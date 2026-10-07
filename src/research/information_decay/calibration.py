"""Synthetic MI-estimator calibration (methodology §9; config/information_decay_v1.yaml).

SYNTHETIC ONLY. Every surprise and response here is generated from fixed distributions; no real
surprise, response or residual value is read. Workers run inside isolation.real_data_guard().

Structure
  * scenario generators   -> one replicate's synthetic data (`sim_direct`, `sim_residual`)
  * per-replicate analysis -> observed statistics, stratified/unrestricted permutation nulls,
                              Null A / Null B residual nulls, interval coverage, outlier shifts
  * runner                -> one deterministic task per (cell, n), aggregated in the worker
  * aggregation           -> one row per (cell, n, estimator, procedure[, method])

Determinism: every random draw comes from rng_for("calibration", <cell>, <n>, <rep>, <purpose>),
so results are identical for any worker count or execution order; aggregation sorts by
replicate index before reducing.

Estimator keys: gcmi (tie policy A: average ranks of S as given), gcmi_B / gcmi_C (D3 policies,
scenario 9 only), ksg3/ksg5/ksg10 (policy K-A), ksg3_KB/... (K-B, scenario 9), pearson / spearman
(statistic |r|), ilin (Gaussian benchmark, observed only), hist (histogram, observed only).
Procedures: strat (stratified permutation), unres (unrestricted), nullA / nullB (residual),
matched_raw (raw response on the residual cohort), none (no inference).
"""
from __future__ import annotations

import math
import multiprocessing as mp
import warnings
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import inference as inf
from .baseline_sim import expanding_sign_baseline, expanding_sign_baseline_batch
from .benchmarks import gaussian_mi_bits, histogram_mi_bits
from .gcmi import (_corr_rows, average_ranks, average_ranks_2d, copnorm, gcmi_from_scores,
                   gcmi_permutation_matrix, scores_from_ranks)
from .ksg import ksg_batch_bits, zscore
from .seeds import rng_for
from .special import wilson_interval
from .truth import scenario_true_mi_bits, solve_strength

STRENGTH_SCENARIOS = {"s3b_tanh": "monotone_tanh", "s4_u_shape": "u_shape",
                      "s5_hetero": "heteroskedastic", "s6_t3": "heavy_tail_t3"}
CI_METHODS = ("boot_pct", "boot_recentred", "subsample")


# ----------------------------------------------------------------------------------------------
# context
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Context:
    config: Dict[str, Any]
    profile: str
    counts: Dict[str, int]
    strengths: Dict[str, float]

    @property
    def horizons(self) -> Tuple[int, ...]:
        return tuple(self.config["horizons_min"])

    @property
    def ks(self) -> Tuple[int, ...]:
        return tuple(self.config["estimators"]["ksg_k"])


def reference_mi_bits(config: Dict[str, Any]) -> float:
    return float(gaussian_mi_bits(config["reference_rho"]))


def solve_strengths(config: Dict[str, Any]) -> Dict[str, float]:
    """Strength parameter per strength-calibrated scenario: true per-horizon MI == I_ref."""
    target = reference_mi_bits(config)
    hi = float(config["scenario_parameters"]["strength_search_upper"])
    return {name: solve_strength(model, target, hi=hi, tol=1e-6) for name, model in STRENGTH_SCENARIOS.items()}


def make_context(config: Dict[str, Any], profile: str, strengths: Optional[Dict[str, float]] = None) -> Context:
    return Context(config=config, profile=profile, counts=dict(config["profiles"][profile]),
                   strengths=strengths if strengths is not None else solve_strengths(config))


def cell_sizes(config: Dict[str, Any], cell: Dict[str, Any]) -> List[int]:
    grid = list(config["sample_sizes"]["grid"])
    if cell.get("sizes", "grid") == "grid+exact":
        return sorted(set(grid) | set(config["sample_sizes"]["exact_cohort_sizes"]))
    return grid


def contamination_count(config: Dict[str, Any], n: int) -> int:
    return max(1, int(round(config["scenario_parameters"]["s7_outliers"]["fraction"] * n)))


def contaminated_truth_bits(clean_bits: float, p: float) -> float:
    """MI of the population mixture (1-p) * clean + p * {S = 4u, R_h = -10u, u = +-1}: the
    contamination indicator is a.s. a function of S and of R_h, and inside the contaminated
    component R_h determines sign(S) (1 bit). I = H_b(p) + (1-p) I_clean + p (Amendment 02 §3.3)."""
    hb = -p * math.log2(p) - (1 - p) * math.log2(1 - p)
    return hb + (1 - p) * clean_bits + p


def cell_contaminated_truth_bits(ctx: Context, cell: Dict[str, Any], n: int) -> Optional[float]:
    """Contaminated-population reference MI for the dependent-contamination scenario 7 cells."""
    if cell["scenario"] != "s7_outliers":
        return None
    return contaminated_truth_bits(float(gaussian_mi_bits(cell["rho"])), contamination_count(ctx.config, n) / n)


def cell_truth_bits(ctx: Context, cell: Dict[str, Any]) -> Optional[float]:
    """True per-horizon MI(S; analysed response), or None when not available."""
    sc = cell["scenario"]
    if cell.get("is_null"):
        return 0.0
    if sc in ("s2_gaussian", "s7_outliers"):
        return float(gaussian_mi_bits(cell["rho"]))
    if sc == "s3a_copula":
        return float(gaussian_mi_bits(ctx.config["scenario_parameters"]["s3a_copula"]["rho"]))
    if sc in STRENGTH_SCENARIOS:
        return float(scenario_true_mi_bits(STRENGTH_SCENARIOS[sc], ctx.strengths[sc]))
    return None  # s8 alternative (residualized), s9 alternative (trend + discrete standardization)


# ----------------------------------------------------------------------------------------------
# synthetic data
# ----------------------------------------------------------------------------------------------
def cumulative_noise(rng: np.random.Generator, n: int, horizons: Sequence[int]) -> np.ndarray:
    """Z_h = W(h)/sqrt(h) for a standard Brownian motion W: unit-variance margins, cumulative
    cross-horizon correlation sqrt(min/max)."""
    h = np.asarray(horizons, dtype=float)
    dh = np.diff(np.concatenate([[0.0], h]))
    inc = rng.standard_normal((n, len(h))) * np.sqrt(dh)
    return np.cumsum(inc, axis=1) / np.sqrt(h)


def expanding_standardize(raw: np.ndarray, min_history: int, rel_tol: float) -> np.ndarray:
    """Frozen causal standardization: (raw_t - mean(prior)) / std(prior, ddof=1), >= min_history
    strictly earlier values; null when the history is (numerically) constant."""
    raw = np.asarray(raw, dtype=float)
    out = np.full(len(raw), np.nan)
    for i in range(min_history, len(raw)):
        prior = raw[:i]
        sd = prior.std(ddof=1)
        if not np.isfinite(sd) or sd <= rel_tol * max(1.0, float(np.abs(prior).max())):
            continue
        out[i] = (raw[i] - prior.mean()) / sd
    return out


def make_surprise(rng: np.random.Generator, N: int, mode: str, cfg: Dict[str, Any],
                  burn_in: bool) -> Dict[str, np.ndarray]:
    """raw surprise, analysed S (nan in burn-in), innovation z (unit variance), raw sign."""
    sm = cfg["surprise_modes"]
    b = int(sm["burn_in"])
    if mode == "A":
        raw = rng.standard_normal(N)
        S = raw.copy()
        if burn_in:
            S[:b] = np.nan
        z = raw
    elif mode == "B_cont":
        mu, sd = sm["continuous_raw"]["mean"], sm["continuous_raw"]["sd"]
        raw = mu + sd * rng.standard_normal(N)
        S = expanding_standardize(raw, b, sm["zero_dispersion_rel_tol"])
        z = (raw - mu) / sd
    elif mode == "B_disc":
        vals = np.asarray(sm["discrete_raw"]["values"], dtype=float)
        p = np.asarray(sm["discrete_raw"]["probs"], dtype=float)
        p = p / p.sum()
        raw = vals[rng.choice(len(vals), size=N, p=p)]
        S = expanding_standardize(raw, b, sm["zero_dispersion_rel_tol"])
        mu = float((vals * p).sum())
        z = (raw - mu) / math.sqrt(float(((vals - mu) ** 2 * p).sum()))
    else:
        raise KeyError(mode)
    return {"raw": raw, "S": S, "z": z, "sign": np.sign(raw).astype(int)}


def sim_direct(ctx: Context, cell: Dict[str, Any], n: int, rep: int) -> Dict[str, np.ndarray]:
    cfg = ctx.config
    sp = cfg["scenario_parameters"]
    H = ctx.horizons
    rng = rng_for("calibration", cell["id"], n, rep, "data")
    mode = cell.get("mode", "A")
    sc = cell["scenario"]
    if mode == "A":
        sur = make_surprise(rng, n, "A", cfg, burn_in=False)
        S, z, raw = sur["S"], sur["z"], None
    else:
        b = int(cfg["surprise_modes"]["burn_in"])
        sur = make_surprise(rng, n + b, mode, cfg, burn_in=True)
        S, z, raw = sur["S"][b:], sur["z"][b:], sur["raw"][b:]
    noise = cumulative_noise(rng, n, H)
    strata = inf.strata_by_position(n, cfg["strata"]["proportions"])
    out: Dict[str, np.ndarray] = {"strata": strata}
    beta = ctx.strengths.get(sc)
    if sc in ("s1_null",):
        Y = noise
    elif sc == "s1h_null_hetero":
        Y = noise.copy()
        Y[:, 0] *= np.sqrt(3.0 / rng.chisquare(3, n))
        Y[:, 1] *= np.sqrt(5.0 / rng.chisquare(5, n))
    elif sc == "s2_gaussian":
        rho = cell["rho"]
        Y = rho * z[:, None] + math.sqrt(1 - rho * rho) * noise
    elif sc == "s3a_copula":
        p = sp["s3a_copula"]
        rho = p["rho"]
        Yg = rho * S[:, None] + math.sqrt(1 - rho * rho) * noise
        S = S ** int(p["s_power"])
        Y = np.exp(float(p["r_exp_scale"]) * Yg)
    elif sc == "s3b_tanh":
        Y = np.tanh(beta * S)[:, None] + noise
    elif sc == "s4_u_shape":
        Y = beta * (S * S - 1.0)[:, None] + noise
    elif sc == "s5_hetero":
        Y = (1.0 + beta * np.abs(S))[:, None] * noise
    elif sc == "s6_t3":
        b6 = float(cell["beta"]) if "beta" in cell else beta
        Y = b6 * S[:, None] + noise * np.sqrt(3.0 / rng.chisquare(3, n))[:, None]
    elif sc == "s7_outliers":
        p = sp["s7_outliers"]
        rho = cell["rho"]
        Y = rho * S[:, None] + math.sqrt(1 - rho * rho) * noise
        out["S_clean"], out["Y_clean"] = S.copy(), Y.copy()
        c = max(1, int(round(p["fraction"] * n)))
        pos = rng.choice(n, size=c, replace=False)
        u = rng.choice(np.array([-1.0, 1.0]), size=c)
        S = S.copy()
        Y = Y.copy()
        S[pos] = p["s_value"] * u
        Y[pos, :] = p["r_value"] * u[:, None]
    elif sc == "s7v4a_indep_contam":
        # Amendment 02 §3.2: independence-preserving contamination. S-side and R-side contamination
        # index sets and signs are drawn independently, so the S vector is independent of R.
        p = sp["s7_outliers"]
        Y = noise.copy()
        c = max(1, int(round(p["fraction"] * n)))
        m_s = rng.choice(n, size=c, replace=False)
        u_s = rng.choice(np.array([-1.0, 1.0]), size=c)
        m_r = rng.choice(n, size=c, replace=False)
        u_r = rng.choice(np.array([-1.0, 1.0]), size=c)
        S = S.copy()
        S[m_s] = p["s_value"] * u_s
        Y[m_r, :] = p["r_value"] * u_r[:, None]
        out["contam_S_idx"], out["contam_R_idx"] = m_s, m_r
    elif sc == "s9_discrete":
        p = sp["s9_trend"]
        rho = cell.get("rho", 0.0)
        t = (np.arange(n) + 0.5) / n
        mu_t = p["mean_slope"] * (t - 0.5)
        sd_t = 1.0 + p["sd_slope"] * t
        Y = mu_t[:, None] + sd_t[:, None] * (rho * z[:, None] + math.sqrt(1 - rho * rho) * noise)
        out["raw"] = raw
    elif sc == "s10_regime":
        p = sp["s10_regime"]
        m = np.asarray(p["means"], dtype=float)[strata]
        s = np.asarray(p["sds"], dtype=float)[strata]
        S = m + s * z
        Y = m[:, None] + s[:, None] * noise
    else:
        raise KeyError(sc)
    out["S"], out["Y"] = np.asarray(S, dtype=float), np.asarray(Y, dtype=float)
    return out


def sim_residual(ctx: Context, cell: Dict[str, Any], n: int, rep: int) -> Dict[str, np.ndarray]:
    """A time-ordered family history: burn-in releases (raw sign, no S) followed by S-eligible
    releases; truncated right after the n-th release of the observed residual cohort."""
    cfg = ctx.config
    rng = rng_for("calibration", cell["id"], n, rep, "data")
    N0 = 2 * n + 80
    sur = make_surprise(rng, N0, cell.get("mode", "A"), cfg, burn_in=True)
    noise = cumulative_noise(rng, N0, ctx.horizons)
    if cell.get("t3"):
        noise = noise * np.sqrt(3.0 / rng.chisquare(3, N0))[:, None]
    rho = float(cell.get("rho", 0.0))
    R = rho * sur["z"][:, None] + math.sqrt(1 - rho * rho) * noise
    mh = int(cfg["baseline_replica"]["min_history"])
    _, cnt = expanding_sign_baseline(R, sur["sign"], mh)
    cohort = ~np.isnan(sur["S"]) & (cnt >= mh)
    idx = np.flatnonzero(cohort)
    if len(idx) < n:
        raise RuntimeError(f"synthetic history too short for n={n} ({cell['id']}, rep {rep})")
    N = int(idx[n - 1]) + 1
    return {"R": R[:N], "S": sur["S"][:N], "sign": sur["sign"][:N], "raw": sur["raw"][:N],
            "strata": inf.strata_by_position(N, cfg["strata"]["proportions"])}


# ----------------------------------------------------------------------------------------------
# analysis helpers
# ----------------------------------------------------------------------------------------------
def _record(out: Dict[str, np.ndarray], est: str, proc: str, obs: np.ndarray, null: np.ndarray) -> None:
    pv = inf.permutation_pvalues(obs, null)
    out[f"{est}|{proc}|obs"] = np.asarray(obs, dtype=float)
    out[f"{est}|{proc}|null_mean"] = pv["null_mean"]
    out[f"{est}|{proc}|null_sd"] = pv["null_sd"]
    out[f"{est}|{proc}|p_unadj"] = pv["p_unadjusted"]
    out[f"{est}|{proc}|p_adj"] = pv["p_horizon_adjusted"]
    out[f"{est}|{proc}|p_panel"] = np.array([pv["p_panel"]])
    st = inf.studentized_pvalues(obs, null)
    out[f"{est}|{proc}|p_panel_student"] = np.array([st["p_panel"]])


def _scores_cols(Y: np.ndarray) -> np.ndarray:
    return np.column_stack([copnorm(Y[:, j]) for j in range(Y.shape[1])])


GCMI_POLICY_KEYS = {"A": "gcmi", "B": "gcmi_B", "C": "gcmi_C"}


def cell_tie_policies(cell: Dict[str, Any], has_raw: bool) -> List[str]:
    """Tie policies evaluated in a cell: an explicit `tie_policies` list, else A/B/C when a raw
    (discretised) surprise exists in a direct scenario, else A only."""
    if cell.get("tie_policies"):
        return list(cell["tie_policies"])
    return ["A", "B", "C"] if has_raw and cell["scenario"] != "s8_residual" else ["A"]


def policy_ranks(S: np.ndarray, raw: Optional[np.ndarray], policy: str,
                 rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """1-based ranks of S under a D3 tie policy -- the SINGLE implementation used by the raw,
    residual (observed / Null A / Null B) and bootstrap paths.
    A: average ranks of S as given (exact ties share their average rank).
    B: every member of an exact raw-value tie group gets the group's average rank; groups are
       ordered by their median S.
    C: rank by group (ordered as in B), ties within a group broken by a seeded random order.
    Without a raw surprise (continuous S), B and C reduce to A."""
    S = np.asarray(S, dtype=float)
    n = len(S)
    if policy == "A" or raw is None:
        return average_ranks(S)
    raw = np.asarray(raw, dtype=float)
    vals, inv = np.unique(raw, return_inverse=True)
    med = np.array([np.median(S[inv == g]) for g in range(len(vals))])
    grp_order = np.argsort(np.argsort(med, kind="mergesort"), kind="mergesort")
    key = grp_order[inv]
    if policy == "B":
        return average_ranks(key.astype(float))
    if policy == "C":
        order = np.lexsort((rng.random(n), key))
        ranks = np.empty(n)
        ranks[order] = np.arange(1, n + 1)
        return ranks
    raise KeyError(policy)


def tie_policy_scores(S: np.ndarray, raw: Optional[np.ndarray], policy: str,
                      rng: Optional[np.random.Generator]) -> np.ndarray:
    """Normal scores of S under a D3 tie policy (see policy_ranks)."""
    return scores_from_ranks(policy_ranks(S, raw, policy, rng), len(S))


def tie_policy_scores_masked(S_b: np.ndarray, raw_b: Optional[np.ndarray], mask: np.ndarray, policy: str,
                             rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """Row-wise policy scores on per-row cohorts (Null B). Entries outside the mask are 0."""
    nb = mask.sum(axis=1)
    if policy == "A" or raw_b is None:
        return np.where(mask, scores_from_ranks(average_ranks_2d(S_b, mask), nb), 0.0)
    if policy == "B":
        return np.where(mask, scores_from_ranks(_policy_b_ranks_masked(S_b, raw_b, mask), nb), 0.0)
    out = np.zeros(S_b.shape)
    for b in range(S_b.shape[0]):
        m = mask[b]
        out[b, m] = tie_policy_scores(S_b[b, m], raw_b[b, m], policy, rng)
    return out


def _policy_b_ranks_masked(S_b: np.ndarray, raw_b: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Vectorized policy B over per-row cohorts; numerically identical to applying policy_ranks
    row by row: group medians per row, groups ordered by median with ties broken by raw value
    (stable), members get their group's average rank among valid entries."""
    vals = np.unique(raw_b[mask])
    G = len(vals)
    med = np.full((S_b.shape[0], G), np.inf)
    for g, v in enumerate(vals):
        m = mask & (raw_b == v)
        present = m.any(axis=1)
        if present.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows: group absent there
                med_g = np.nanmedian(np.where(m, S_b, np.nan), axis=1)
            med[:, g] = np.where(present, med_g, np.inf)
    order = np.argsort(med, axis=1, kind="mergesort")
    grank = np.empty_like(order)
    np.put_along_axis(grank, order, np.broadcast_to(np.arange(G), order.shape), axis=1)
    gidx = np.clip(np.searchsorted(vals, raw_b), 0, G - 1)
    key = np.take_along_axis(grank, gidx, axis=1).astype(float)
    return average_ranks_2d(key, mask)


def tie_policy_scores_bootstrap(S_b: np.ndarray, raw_b: Optional[np.ndarray], policy: str,
                                rng: np.random.Generator) -> np.ndarray:
    """Row-wise policy scores for bootstrap resamples (duplicated releases). Policy A breaks the
    duplicates' exact ties in seeded random order (D5 option a); B gives each raw tie group --
    duplicates included -- its average rank; C as in policy_ranks."""
    if policy == "A" or raw_b is None:
        return _random_tiebreak_scores(S_b, rng)
    n = S_b.shape[1]
    return np.vstack([tie_policy_scores(S_b[b], raw_b[b], policy, rng) for b in range(S_b.shape[0])])


def _random_tiebreak_scores(X: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Row-wise ordinal ranks with exact ties (bootstrap duplicates) broken in seeded random order."""
    B, n = X.shape
    order = np.lexsort((rng.random(X.shape), X), axis=1)
    ranks = np.empty((B, n))
    np.put_along_axis(ranks, order, np.broadcast_to(np.arange(1.0, n + 1), (B, n)), axis=1)
    return scores_from_ranks(ranks, n)


def _rowwise_gcmi_batch(Sb: np.ndarray, Yb: np.ndarray, scorer, s_scorer=None) -> np.ndarray:
    """Sb: (B, m), Yb: (B, m, H) -> (B, H) GCMI using `scorer(rows)` for response normal scores and
    `s_scorer` (default `scorer`) for the surprise. The surprise is scored first."""
    B, m, H = Yb.shape
    zs = (s_scorer or scorer)(Sb)
    zy = scorer(np.transpose(Yb, (0, 2, 1)).reshape(B * H, m)).reshape(B, H, m)
    return gcmi_from_scores(zs[:, None, :], zy)


def _interval_rows(est: str, obs: np.ndarray, boot: np.ndarray, sub: np.ndarray, m_over_n: float,
                   null_mean: np.ndarray, truth: float, out: Dict[str, np.ndarray]) -> None:
    lo_b, hi_b = np.quantile(boot, 0.025, axis=0), np.quantile(boot, 0.975, axis=0)
    shift = boot.mean(axis=0) - obs
    tau = math.sqrt(m_over_n)
    lo_s = obs - tau * (np.quantile(sub, 0.975, axis=0) - obs)
    hi_s = obs - tau * (np.quantile(sub, 0.025, axis=0) - obs)
    cis = {"boot_pct": (lo_b, hi_b), "boot_recentred": (lo_b - shift, hi_b - shift), "subsample": (lo_s, hi_s)}
    for method, (lo, hi) in cis.items():
        if (method == "subsample" and sub is None) or (method != "subsample" and boot is None):
            continue
        out[f"{est}|cov_eff|{method}"] = ((lo - null_mean <= truth) & (truth <= hi - null_mean)).astype(float)
        out[f"{est}|cov_obs|{method}"] = ((lo <= truth) & (truth <= hi)).astype(float)
        out[f"{est}|width|{method}"] = hi - lo


def _masked_gcmi(Sb: np.ndarray, Eb: np.ndarray, mask: np.ndarray, zs: Optional[np.ndarray] = None) -> np.ndarray:
    """GCMI on a per-row cohort. Sb: (B, N), Eb: (B, N, H), mask: (B, N) -> (B, H). `zs`: optional
    precomputed surprise scores (tie policy applied); default policy A."""
    B, N, H = Eb.shape
    nb = mask.sum(axis=1)
    if zs is None:
        zs = scores_from_ranks(average_ranks_2d(Sb, mask), nb)
    Et = np.transpose(Eb, (0, 2, 1)).reshape(B * H, N)
    mk = np.repeat(mask, H, axis=0)
    ze = scores_from_ranks(average_ranks_2d(Et, mk), np.repeat(nb, H)).reshape(B, H, N)
    return gcmi_from_scores(np.where(mask, zs, 0.0)[:, None, :], np.where(mask[:, None, :], ze, 0.0),
                            np.broadcast_to(mask[:, None, :], (B, H, N)))


def _ksg_perm(x: np.ndarray, Y: np.ndarray, perms: np.ndarray, ks: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
    """KSG observed (K, H) and permutation null (B, K, H); x (n,), Y (n, H), already scaled."""
    H = Y.shape[1]
    obs = np.empty((len(ks), H))
    null = np.empty((perms.shape[0], len(ks), H))
    for h in range(H):
        obs[:, h] = ksg_batch_bits(x[None, :], Y[None, :, h], ks)[0]
        null[:, :, h] = ksg_batch_bits(x[perms], Y[:, h], ks)
    return obs, null


def _jitter(x: np.ndarray, sd: float, rng: np.random.Generator) -> np.ndarray:
    return x + sd * rng.standard_normal(x.shape)


# ----------------------------------------------------------------------------------------------
# per-replicate analyses
# ----------------------------------------------------------------------------------------------
def analyse_direct(ctx: Context, cell: Dict[str, Any], n: int, rep: int, sim: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    c = ctx.counts
    cfg = ctx.config
    ks = ctx.ks
    jsd = float(cfg["estimators"]["ksg_jitter_sd"])
    S, Y = sim["S"], sim["Y"]
    strata = inf.merge_small_strata(sim["strata"], cfg["strata"]["min_stratum_size"])
    perms = inf.stratified_permutations(strata, c["perms_gcmi"], rng_for("calibration", cell["id"], n, rep, "perm_strat"))
    perms_u = (inf.unrestricted_permutations(n, c["perms_gcmi"], rng_for("calibration", cell["id"], n, rep, "perm_unres"))
               if cell.get("unrestricted") else None)
    out: Dict[str, np.ndarray] = {}
    zy = _scores_cols(Y)

    # GCMI under each tie policy of the cell (central tie-policy layer)
    raw = sim.get("raw")
    policies = {GCMI_POLICY_KEYS[p]: p for p in cell_tie_policies(cell, raw is not None)}
    zs_by = {}
    for est, pol in policies.items():
        zs = tie_policy_scores(S, raw, pol, rng_for("calibration", cell["id"], n, rep, "tiebreak_C"))
        zs_by[est] = zs
        _record(out, est, "strat", gcmi_from_scores(zs[None, :], zy.T[None, :, :])[0], gcmi_permutation_matrix(zs, zy, perms))
        if perms_u is not None and est == "gcmi":
            _record(out, est, "unres", out[f"{est}|strat|obs"], gcmi_permutation_matrix(zs, zy, perms_u))

    # correlation benchmarks (statistic |r|) and observed-only benchmarks
    for est, xs, ys in (("pearson", zscore(S), zscore(Y, axis=0)),
                        ("spearman", zscore(average_ranks(S)), zscore(np.column_stack([average_ranks(Y[:, j]) for j in range(Y.shape[1])]), axis=0))):
        xc = xs - xs.mean()
        yc = ys - ys.mean(axis=0)
        den = np.sqrt((xc * xc).sum() * (yc * yc).sum(axis=0))
        with np.errstate(all="ignore"):
            null = np.abs(xc[perms] @ yc / den)
        _record(out, est, "strat", np.abs((xc @ yc) / den), null)
    r_p = np.array([_corr_rows(S, Y[:, j]) for j in range(Y.shape[1])])
    out["ilin|none|obs"] = gaussian_mi_bits(r_p)
    out["hist|none|obs"] = np.array([histogram_mi_bits(S, Y[:, j], cfg["estimators"]["histogram_bins"]) for j in range(Y.shape[1])])

    # outlier shift (scenario 7): observed estimates on the clean version of the same replicate
    if "S_clean" in sim:
        Sc, Yc = sim["S_clean"], sim["Y_clean"]
        out["gcmi|clean|obs"] = gcmi_from_scores(copnorm(Sc)[None, :], _scores_cols(Yc).T[None, :, :])[0]
        out["ilin|clean|obs"] = gaussian_mi_bits(np.array([_corr_rows(Sc, Yc[:, j]) for j in range(Yc.shape[1])]))

    truth = cell.get("_truth")
    # GCMI interval coverage
    if cell.get("coverage") and rep < cell.get("reps_coverage", c["reps_coverage"]) and truth is not None:
        frac = float(cfg["estimators"]["subsample_fraction"])
        bidx = inf.stratified_bootstrap(strata, c["boot_gcmi"], rng_for("calibration", cell["id"], n, rep, "boot_gcmi"))
        sidx = inf.stratified_subsample(strata, c["subsample_gcmi"], frac, rng_for("calibration", cell["id"], n, rep, "sub_gcmi"))
        m = sidx.shape[1]
        for est, pol in policies.items():
            if pol == "C":
                continue
            tb = rng_for("calibration", cell["id"], n, rep, "boot_gcmi_tiebreak")
            rb = raw[bidx] if raw is not None else None
            boot = _rowwise_gcmi_batch(S[bidx], Y[bidx], lambda X: _random_tiebreak_scores(X, tb),
                                       s_scorer=lambda X: tie_policy_scores_bootstrap(X, rb, pol, tb))
            rs = raw[sidx] if raw is not None else None
            sub_s = (None if (pol == "A" or rs is None) else
                     (lambda X: np.vstack([tie_policy_scores(X[b], rs[b], pol, None) for b in range(X.shape[0])])))
            sub = _rowwise_gcmi_batch(S[sidx], Y[sidx], lambda X: scores_from_ranks(average_ranks_2d(X), m), s_scorer=sub_s)
            _interval_rows(est, out[f"{est}|strat|obs"], boot, sub, m / n, out[f"{est}|strat|null_mean"], truth, out)

    # KSG family
    if rep < cell.get("reps_ksg", c["reps_ksg"]):
        Bk = c["perms_ksg"]
        jrng = rng_for("calibration", cell["id"], n, rep, "jitter_ksg")
        xA = _jitter(zscore(S), jsd, jrng)
        Yj = _jitter(zscore(Y, axis=0), jsd, jrng)
        xs_by = {"": xA}
        if "gcmi_B" in zs_by:
            xs_by["_KB"] = _jitter(zscore(zs_by["gcmi_B"]), jsd, jrng)
        for suffix, x in xs_by.items():
            obs, null = _ksg_perm(x, Yj, perms[:Bk], ks)
            for j, k in enumerate(ks):
                _record(out, f"ksg{k}{suffix}", "strat", obs[j], null[:, j, :])
            if perms_u is not None and suffix == "":
                _, null_u = _ksg_perm(x, Yj, perms_u[:Bk], ks)
                for j, k in enumerate(ks):
                    _record(out, f"ksg{k}", "unres", obs[j], null_u[:, j, :])
        if "S_clean" in sim:
            xc = _jitter(zscore(sim["S_clean"]), jsd, jrng)
            Ycj = _jitter(zscore(sim["Y_clean"], axis=0), jsd, jrng)
            for h in range(Y.shape[1]):
                v = ksg_batch_bits(xc[None, :], Ycj[None, :, h], ks)[0]
                for j, k in enumerate(ks):
                    out.setdefault(f"ksg{k}|clean|obs", np.empty(Y.shape[1]))[h] = v[j]
        # KSG interval coverage
        if cell.get("coverage") and rep < cell.get("reps_ksg_coverage", c["reps_ksg_coverage"]) and truth is not None:
            frac = float(cfg["estimators"]["subsample_fraction"])
            bidx = inf.stratified_bootstrap(strata, c["boot_ksg"], rng_for("calibration", cell["id"], n, rep, "boot_ksg"))
            sidx = inf.stratified_subsample(strata, c["subsample_ksg"], frac, rng_for("calibration", cell["id"], n, rep, "sub_ksg"))
            m = sidx.shape[1]
            H = Y.shape[1]
            boot = np.empty((bidx.shape[0], len(ks), H))
            sub = np.empty((sidx.shape[0], len(ks), H))
            for h in range(H):
                boot[:, :, h] = ksg_batch_bits(zscore(xA[bidx], axis=1), zscore(Yj[bidx, h], axis=1), ks)
                sub[:, :, h] = ksg_batch_bits(zscore(xA[sidx], axis=1), zscore(Yj[sidx, h], axis=1), ks)
            for j, k in enumerate(ks):
                e = f"ksg{k}"
                _interval_rows(e, out[f"{e}|strat|obs"], boot[:, j, :], sub[:, j, :], m / n,
                               out[f"{e}|strat|null_mean"], truth, out)
    return out


def analyse_residual(ctx: Context, cell: Dict[str, Any], n: int, rep: int, sim: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """Scenario 8: residual MI under fixed-residual (Null A) and pipeline-aware (Null B) nulls,
    plus the matched raw comparator on the same cohort."""
    c = ctx.counts
    cfg = ctx.config
    ks = ctx.ks
    mh = int(cfg["baseline_replica"]["min_history"])
    mss = cfg["strata"]["min_stratum_size"]
    R, S, sign, strata = sim["R"], sim["S"], sim["sign"], sim["strata"]
    N, H = R.shape
    eligible = ~np.isnan(S)
    exp, cnt = expanding_sign_baseline(R, sign, mh)
    cohort = eligible & (cnt >= mh)
    E = R - exp
    ci = np.flatnonzero(cohort)
    out: Dict[str, np.ndarray] = {}

    raw = sim.get("raw")
    policies = {GCMI_POLICY_KEYS[p]: p for p in cell_tie_policies(cell, raw is not None)}
    tie_rng = lambda: rng_for("calibration", cell["id"], n, rep, "tiebreak_C")  # noqa: E731

    # Null A (diagnostic only per Amendment 02): residual fixed, S permuted within strata over the
    # observed cohort; plus the matched raw comparator on the same cohort and permutations.
    st_c = inf.merge_small_strata(strata[ci], mss)
    permsA = inf.stratified_permutations(st_c, c["perms_gcmi"], rng_for("calibration", cell["id"], n, rep, "perm_nullA"))
    zE = _scores_cols(E[ci])
    zR = _scores_cols(R[ci])
    for est, pol in policies.items():
        zs = tie_policy_scores(S[ci], None if raw is None else raw[ci], pol, tie_rng())
        _record(out, est, "nullA", gcmi_from_scores(zs[None, :], zE.T[None, :, :])[0], gcmi_permutation_matrix(zs, zE, permsA))
        _record(out, est, "matched_raw", gcmi_from_scores(zs[None, :], zR.T[None, :, :])[0],
                gcmi_permutation_matrix(zs, zR, permsA))

    # Null B (pipeline-aware): permute the surprise LABEL (surprise_std, surprise_raw, raw sign)
    # over the S-eligible pool within strata, recompute the frozen sign-conditioned baseline, the
    # residual and the cohort, then apply the SAME tie policy to the permuted cohort.
    pool = np.flatnonzero(eligible)
    st_p = inf.merge_small_strata(strata[pool], mss)
    permsB = inf.stratified_permutations(st_p, c["perms_gcmi"], rng_for("calibration", cell["id"], n, rep, "perm_nullB"))
    B = permsB.shape[0]
    signs_b = np.broadcast_to(sign, (B, N)).copy()
    signs_b[:, pool] = sign[pool][permsB]
    S_b = np.broadcast_to(S, (B, N)).copy()
    S_b[:, pool] = S[pool][permsB]
    raw_b = None
    if raw is not None:
        raw_b = np.broadcast_to(raw, (B, N)).copy()
        raw_b[:, pool] = raw[pool][permsB]
    exp_b, cnt_b = expanding_sign_baseline_batch(R, signs_b, mh)
    cohort_b = eligible[None, :] & (cnt_b >= mh)
    E_b = np.nan_to_num(R[None, :, :] - exp_b)
    for est, pol in policies.items():
        zs_b = tie_policy_scores_masked(np.nan_to_num(S_b), raw_b, cohort_b, pol, tie_rng())
        null_B = _masked_gcmi(S_b, E_b, cohort_b, zs=zs_b)
        zs_o = tie_policy_scores_masked(np.nan_to_num(S)[None, :], None if raw is None else raw[None, :],
                                        cohort[None, :], pol, tie_rng())
        obsB = _masked_gcmi(S[None, :], np.nan_to_num(E)[None], cohort[None, :], zs=zs_o)[0]
        _record(out, est, "nullB", obsB, null_B)
        out[f"{est}|nullB|perm_cohort_n_mean"] = np.array([cohort_b.sum(axis=1).mean()])

    if rep < cell.get("reps_ksg", c["reps_ksg"]):
        Bk = c["perms_ksg"]
        jsd = float(cfg["estimators"]["ksg_jitter_sd"])
        jrng = rng_for("calibration", cell["id"], n, rep, "jitter_ksg")
        Sj = S.copy()
        Sj[eligible] = _jitter(S[eligible], jsd * float(np.nanstd(S)), jrng)
        x = zscore(Sj[ci])
        obs, null = _ksg_perm(x, zscore(E[ci], axis=0), permsA[:Bk], ks)
        obs_r, null_r = _ksg_perm(x, zscore(R[ci], axis=0), permsA[:Bk], ks)
        for j, k in enumerate(ks):
            _record(out, f"ksg{k}", "nullA", obs[j], null[:, j, :])
            _record(out, f"ksg{k}", "matched_raw", obs_r[j], null_r[:, j, :])
        Sj_b = np.broadcast_to(Sj, (Bk, N)).copy()
        Sj_b[:, pool] = Sj[pool][permsB[:Bk]]
        mask = cohort_b[:Bk]
        xb = zscore(np.nan_to_num(Sj_b), axis=1, mask=mask)
        obsB_k = np.empty((len(ks), H))
        nullB_k = np.empty((Bk, len(ks), H))
        x_obs = zscore(np.nan_to_num(Sj)[None, :], axis=1, mask=cohort[None, :])
        for h in range(H):
            yb = zscore(np.nan_to_num(E_b[:Bk, :, h]), axis=1, mask=mask)
            nullB_k[:, :, h] = ksg_batch_bits(xb, yb, ks, valid=mask)
            y_obs = zscore(np.nan_to_num(E[:, h])[None, :], axis=1, mask=cohort[None, :])
            obsB_k[:, h] = ksg_batch_bits(x_obs, y_obs, ks, valid=cohort[None, :])[0]
        for j, k in enumerate(ks):
            _record(out, f"ksg{k}", "nullB", obsB_k[j], nullB_k[:, j, :])
    return out


def run_replicate(ctx: Context, cell: Dict[str, Any], n: int, rep: int) -> Dict[str, np.ndarray]:
    if cell["scenario"] == "s8_residual":
        return analyse_residual(ctx, cell, n, rep, sim_residual(ctx, cell, n, rep))
    return analyse_direct(ctx, cell, n, rep, sim_direct(ctx, cell, n, rep))


# ----------------------------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------------------------
_WORKER: Dict[str, Any] = {}


def _worker_init(ctx: Context, repo_root: str) -> None:
    from .isolation import real_data_guard
    _WORKER["ctx"] = ctx
    _WORKER["guard"] = real_data_guard(repo_root)
    _WORKER["guard"].__enter__()
    _WORKER["cells"] = {c["id"]: c for c in ctx.config["cells"]}


def _prepared_cells(ctx: Context) -> Dict[str, Dict[str, Any]]:
    cells = {}
    for c in ctx.config["cells"]:
        c = dict(c)
        c["_truth"] = cell_truth_bits(ctx, c)
        cells[c["id"]] = c
    return cells


def _run_task(task: Tuple[str, int, int, int]) -> Tuple[str, int, List[Dict[str, Any]], float]:
    """One (cell, n) over replicate indices [start, end), aggregated inside the worker: only
    summary rows travel back to the parent (per-replicate arrays never accumulate in one process)."""
    cell_id, n, start, end = task
    ctx = _WORKER["ctx"]
    cell = _WORKER["prepared"][cell_id]
    t0 = time.perf_counter()
    res = {rep: run_replicate(ctx, cell, n, rep) for rep in range(start, end)}
    rows = aggregate(ctx, cell, n, res)
    return cell_id, n, rows, time.perf_counter() - t0


def _worker_init_full(ctx: Context, repo_root: str) -> None:
    _worker_init(ctx, repo_root)
    _WORKER["prepared"] = _prepared_cells(ctx)


def build_tasks(ctx: Context, cell_ids: Optional[Iterable[str]] = None, sizes: Optional[Iterable[int]] = None,
                reps: Optional[int] = None, rep_start: int = 0,
                cell_sizes_override: Optional[Dict[str, Sequence[int]]] = None) -> List[Tuple[str, int, int, int]]:
    tasks = []
    wanted = set(cell_ids) if cell_ids is not None else None
    size_filter = set(sizes) if sizes is not None else None
    R = reps if reps is not None else ctx.counts["reps"]
    for cell in ctx.config["cells"]:
        if wanted is not None and cell["id"] not in wanted:
            continue
        ns = (cell_sizes_override or {}).get(cell["id"], cell_sizes(ctx.config, cell))
        for n in ns:
            if size_filter is None or n in size_filter:
                tasks.append((cell["id"], int(n), rep_start, rep_start + R))
    # expensive tasks first (larger n, residual cells) for load balance; ties broken by id
    tasks.sort(key=lambda t: (-(t[1] ** 2) * (3 if t[0].startswith("s8") else 1), t[0]))
    return tasks


def run_calibration(ctx: Context, repo_root: str, workers: int = 1, cell_ids=None, sizes=None, reps=None,
                    progress=None, rep_start: int = 0, cell_sizes_override=None, tasks=None) -> Dict[str, Any]:
    """Run every (cell, n) task; returns aggregated rows plus timing. Results are identical for
    any number of workers (each replicate is seeded by its own identifiers; aggregation sorts
    replicates). `progress(done, total, cell_id, n, seconds)` is called after each task."""
    if tasks is None:
        tasks = build_tasks(ctx, cell_ids, sizes, reps, rep_start, cell_sizes_override)
    rows: List[Dict[str, Any]] = []
    timing: Dict[str, float] = {}
    t_start = time.perf_counter()

    def consume(cid, n, task_rows, secs):
        timing[cid] = timing.get(cid, 0.0) + secs
        rows.extend(task_rows)
        if progress:
            progress(len(timing_done) + 1, len(tasks), cid, n, secs)
        timing_done.append((cid, n))

    timing_done: List[Tuple[str, int]] = []
    if workers <= 1:
        _worker_init_full(ctx, repo_root)
        try:
            for t in tasks:
                consume(*_run_task(t))
        finally:
            _WORKER["guard"].__exit__(None, None, None)
    else:
        with mp.get_context("spawn").Pool(workers, initializer=_worker_init_full, initargs=(ctx, repo_root),
                                          maxtasksperchild=4) as pool:
            for result in pool.imap_unordered(_run_task, tasks):
                consume(*result)
    rows.sort(key=lambda r: (r["cell"], r["n"], r["estimator"], r["procedure"], r.get("method", "")))
    return {"rows": rows, "cpu_seconds_by_cell": timing, "wall_seconds": time.perf_counter() - t_start,
            "tasks": len(tasks)}


# ----------------------------------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------------------------------
def _f(x) -> float:
    return float(x) if x is not None and np.isfinite(x) else float("nan")


def aggregate(ctx: Context, cell: Dict[str, Any], n: int, by_rep: Dict[int, Dict[str, np.ndarray]]) -> List[Dict[str, Any]]:
    alpha = float(ctx.config["alpha"])
    kmult = float(ctx.config["decision_rules"]["acceptance_mcse_multiplier"])
    truth = cell.get("_truth")
    reps_sorted = sorted(by_rep)
    groups: Dict[Tuple[str, str], Dict[str, List[np.ndarray]]] = {}
    for rep in reps_sorted:
        for key, val in by_rep[rep].items():
            est, proc, field = key.split("|")
            groups.setdefault((est, proc), {}).setdefault(field, []).append(np.asarray(val, dtype=float))
    rows: List[Dict[str, Any]] = []
    base = {"cell": cell["id"], "scenario": cell["scenario"], "mode": cell.get("mode", "A"),
            "is_null": bool(cell.get("is_null", False)), "n": n, "truth_bits": truth}
    tc = cell_contaminated_truth_bits(ctx, cell, n)
    if tc is not None:
        base["truth_bits_role"] = "clean_target"
        base["truth_contaminated_bits"] = tc
    H = ctx.horizons
    mi_estimators = ("gcmi", "ksg", "ilin", "hist")
    for (est, proc), fields in sorted(groups.items()):
        is_mi = est.startswith(mi_estimators)
        if proc in ("cov_eff", "cov_obs", "width") or "obs" not in fields:
            continue
        obs = np.vstack(fields["obs"])  # (R, H)
        R = obs.shape[0]
        row = dict(base, estimator=est, procedure=proc, reps=R)
        if is_mi:
            row.update(mean_obs_bits=float(obs.mean()), sd_obs_bits=float(obs.std(axis=0, ddof=1).mean()) if R > 1 else float("nan"))
        if "null_mean" in fields:
            nm = np.vstack(fields["null_mean"])
            eff = obs - nm
            per_rep = eff.mean(axis=1)
            if is_mi:
                row.update(mean_null_mean_bits=float(nm.mean()), mean_eff_bits=float(eff.mean()),
                           sd_eff_bits=float(eff.std(axis=0, ddof=1).mean()) if R > 1 else float("nan"),
                           se_eff_bits=float(per_rep.std(ddof=1) / math.sqrt(R)) if R > 1 else float("nan"),
                           p_eff_negative=float((eff < 0).mean()))
                nsd = np.vstack(fields["null_sd"])
                for j, h in enumerate(H):
                    row[f"null_mean_h{h}"] = float(nm[:, j].mean())
                    row[f"null_sd_h{h}"] = float(nsd[:, j].mean())
            pu = np.vstack(fields["p_unadj"])
            pa = np.vstack(fields["p_adj"])
            pp = np.concatenate(fields["p_panel"])
            pps = np.concatenate(fields["p_panel_student"])
            k_adj = int((pp <= alpha).sum())
            lo, hi = wilson_interval(k_adj, R)
            row.update(pointwise_rate=float((pu <= alpha).mean()), adjusted_rate=k_adj / R, adjusted_rejections=k_adj,
                       rep_first=reps_sorted[0], rep_last=reps_sorted[-1],
                       adjusted_rate_wilson_lo=lo, adjusted_rate_wilson_hi=hi,
                       adjusted_rate_studentized=float((pps <= alpha).mean()),
                       acceptance_max=alpha + kmult * math.sqrt(alpha * (1 - alpha) / R))
            for j, h in enumerate(H):
                row[f"pointwise_rate_h{h}"] = float((pu[:, j] <= alpha).mean())
                row[f"adjusted_rate_h{h}"] = float((pa[:, j] <= alpha).mean())
        if is_mi and truth is not None and "null_mean" in fields:
            row.update(bias_obs_bits=float(obs.mean() - truth), bias_eff_bits=float(eff.mean() - truth),
                       rmse_obs_bits=float(np.sqrt(((obs - truth) ** 2).mean())),
                       rmse_eff_bits=float(np.sqrt(((eff - truth) ** 2).mean())))
        elif is_mi and truth is not None:
            row.update(bias_obs_bits=float(obs.mean() - truth), rmse_obs_bits=float(np.sqrt(((obs - truth) ** 2).mean())))
        if "perm_cohort_n_mean" in fields:
            row["perm_cohort_n_mean"] = float(np.concatenate(fields["perm_cohort_n_mean"]).mean())
        rows.append(row)
    # interval coverage rows
    cov: Dict[Tuple[str, str], Dict[str, np.ndarray]] = {}
    for rep in reps_sorted:
        for key, val in by_rep[rep].items():
            est, kind, method = key.split("|")
            if kind in ("cov_eff", "cov_obs", "width"):
                cov.setdefault((est, method), {}).setdefault(kind, []).append(np.asarray(val, dtype=float))
    for (est, method), f in sorted(cov.items()):
        # Coverage of one replicate's horizons are NOT independent trials: the replicate is the
        # cluster. Point coverage = mean over replicates of the replicate's horizon-mean coverage;
        # MC uncertainty = cluster SE sd(c_r)/sqrt(R). Per-horizon coverage uses Wilson intervals
        # (its trials are independent replicates).
        ce = np.vstack(f["cov_eff"])            # (R, H)
        R = ce.shape[0]
        c_r = ce.mean(axis=1)
        se = float(c_r.std(ddof=1) / math.sqrt(R)) if R > 1 else float("nan")
        point = float(c_r.mean())
        row = dict(base, estimator=est, procedure="interval", method=method, reps=R,
                   coverage_eff=point, coverage_eff_cluster_se=se,
                   coverage_eff_cluster_ci_lo=point - 1.959963984540054 * se,
                   coverage_eff_cluster_ci_hi=point + 1.959963984540054 * se,
                   coverage_obs=float(np.vstack(f["cov_obs"]).mean(axis=1).mean()),
                   mean_width_bits=float(np.vstack(f["width"]).mean()),
                   coverage_uncertainty="replicate_cluster")
        for j, h in enumerate(H):
            kh = int(ce[:, j].sum())
            lo, hi = wilson_interval(kh, R)
            row[f"coverage_eff_h{h}"] = kh / R
            row[f"coverage_eff_h{h}_wilson_lo"] = lo
            row[f"coverage_eff_h{h}_wilson_hi"] = hi
        rows.append(row)
    # outlier shift rows (scenario 7)
    for est in sorted({e for (e, p) in groups if p == "clean"}):
        clean = np.vstack(groups[(est, "clean")]["obs"])
        proc = "strat" if (est, "strat") in groups else "none"
        cont = np.vstack(groups[(est, proc)]["obs"])[: clean.shape[0]]
        d = cont - clean
        rows.append(dict(base, estimator=est, procedure="outlier_shift", reps=clean.shape[0],
                         median_abs_shift_bits=float(np.median(np.abs(d))), mean_shift_bits=float(d.mean())))
    return rows
