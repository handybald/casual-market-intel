#!/usr/bin/env python3
"""M3 information decay v1 -- SECONDARY real-data analysis: SPY residual information profile.

    python scripts/run_information_decay_residual_spy_v1.py

Scope (frozen protocol; nothing is chosen here): SPY x {CPI m/m, Core CPI m/m, NFP} x
h in {1, 5, 15, 30, 60}, residual common-support cohort cs_resid (77 / 77 / 97):
  * residual  I(S; E_h), E_h = R_h - Rhat_h, GCMI with tie policy B, inference by the
    PIPELINE-AWARE Null B (methodology §11.7; Amendment 02 §1.2): every permutation permutes the
    surprise LABEL (surprise_std, surprise_raw, surprise_sign) across the label pool within the
    chronological strata, recomputes the frozen sign-conditioned walk-forward baseline, the
    residuals and the residual cohort, applies tie policy B and computes GCMI. Null A (fixed
    residual) is never used.
  * matched raw  I(S; R_h) on EXACTLY the same cs_resid releases (stratified release permutation).
  * delta_effective = residual_effective - matched_raw_effective (descriptive only; no test).
  * horizon max-T per family and response type; secondary residual family-level Holm.
  * stratified release-level percentile bootstrap (2,000; policy B inside every replicate).

Baseline replica. `masked_sign_baseline` is a vectorized replica of the frozen M2 rule
(src/research/baseline.add_baseline): strictly earlier usable responses of the same raw-surprise
sign group, per horizon, min history 8. Every run proves it on the REAL frames: (1) at the identity
permutation it reproduces the stored v2 expected / residual columns, residual cohorts and residual
ranks; (2) for the first K Null-B permutations of each family it equals add_baseline itself
applied to the permuted frame. The frozen add_baseline is imported read-only, never modified.

Secondary: never replaces or alters the frozen primary result (tag m3-primary-spy-information-profile-v1).
Outputs: data/reports/information_decay/residual_spy_v1/ and
metadata/research/information_decay_residual_spy_v1.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.research import filters  # noqa: E402
from src.research.baseline import add_baseline  # noqa: E402
from src.research.dataset import canonical_content_sha256  # noqa: E402
from src.research.information_decay import calibration as cal  # noqa: E402
from src.research.information_decay import gcmi  # noqa: E402
from src.research.information_decay import inference as inf  # noqa: E402
from src.research.information_decay.provenance import implementation_fingerprint  # noqa: E402
from src.research.information_decay.seeds import derive_seed, rng_for, seed_key  # noqa: E402

_spec = importlib.util.spec_from_file_location("primary_v1", REPO_ROOT / "scripts" / "run_information_decay_primary_v1.py")
prim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prim)

CONFIG = REPO_ROOT / "config" / "information_decay_residual_spy_v1.yaml"
OUT_DIR = REPO_ROOT / "data" / "reports" / "information_decay" / "residual_spy_v1"
METADATA = REPO_ROOT / "metadata" / "research" / "information_decay_residual_spy_v1.json"
NO_SIGN = -9
CHUNK = 500


# ---------------------------------------------------------------------------------------------
# frozen-rule baseline replica (per-horizon usability masks)
# ---------------------------------------------------------------------------------------------
def masked_sign_baseline(R: np.ndarray, usable: np.ndarray, signs: np.ndarray, min_history: int) -> np.ndarray:
    """R: (N, H) responses in chronological order (NaN allowed); usable: (N, H) frozen
    filters.usable_response; signs: (B, N) raw-surprise sign in {-1, 0, 1} or NO_SIGN.
    Returns expected (B, N, H): mean of R over strictly earlier rows of the same sign group that
    are usable at that horizon, defined iff that history has >= min_history rows (NaN otherwise,
    and always NaN for NO_SIGN) -- add_baseline's rule."""
    Rz = np.where(usable, np.nan_to_num(R), 0.0)
    B, N = signs.shape
    expected = np.full((B, N, R.shape[1]), np.nan)
    for g in (-1, 0, 1):
        m = signs == g
        if not m.any():
            continue
        mh = (m[:, :, None] & usable[None, :, :]).astype(float)
        c_excl = np.cumsum(mh, axis=1) - mh
        s_excl = np.cumsum(mh * Rz[None], axis=1) - mh * Rz[None]
        ok = m[:, :, None] & (c_excl >= min_history)
        with np.errstate(invalid="ignore", divide="ignore"):
            expected = np.where(ok, s_excl / np.where(c_excl > 0, c_excl, 1.0), expected)
    return expected


def family_arrays(df: pd.DataFrame, family: str, symbol: str, horizons: Sequence[int]) -> Dict[str, Any]:
    """All rows of family x symbol (the baseline's group universe), chronological."""
    x = df[(df["event_family"] == family) & (df["symbol"] == symbol)].copy()
    x["release_timestamp_utc"] = pd.to_datetime(x["release_timestamp_utc"], utc=True)
    x = x.sort_values(["release_timestamp_utc", "release_id"]).reset_index(drop=True)
    if not x["release_timestamp_utc"].is_unique:
        raise RuntimeError(f"{family}: non-unique release timestamps; 'strictly earlier' would be ambiguous")
    usable = np.column_stack([filters.usable_response(x, f"post{h}m").to_numpy() for h in horizons])
    R = np.column_stack([x[f"post{h}m_ret"].astype(float).to_numpy() for h in horizons])
    signs = x["surprise_sign"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    pool = (filters.usable_std_surprise(x) & ~x["provisional_market_data"].fillna(False).astype(bool)).to_numpy()
    return {"frame": x, "R": R, "usable": usable,
            "sign": np.where(np.isnan(signs), NO_SIGN, signs).astype(int),
            "S": x["surprise_std"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
            "raw": x["surprise_raw"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
            "pool": pool}


def residual_cohort(pool: np.ndarray, usable: np.ndarray, expected: np.ndarray) -> np.ndarray:
    """cs_resid: label-pool releases with a usable response AND an ok baseline at every horizon."""
    return pool[None, :] & usable.all(axis=1)[None, :] & ~np.isnan(expected).any(axis=2)


# ---------------------------------------------------------------------------------------------
# Null B (pipeline-aware) -- observed statistic is the same function at the identity permutation
# ---------------------------------------------------------------------------------------------
def null_b_batch(fa: Dict[str, Any], pool_idx: np.ndarray, perms: np.ndarray, min_history: int,
                 policy: str) -> Dict[str, np.ndarray]:
    """perms: (B, P) indices into pool_idx (label sources). Returns per-permutation GCMI, |Pearson|,
    |Spearman| on the recomputed residual cohort, plus cohort sizes."""
    N = len(fa["sign"])
    B = perms.shape[0]
    src = pool_idx[perms]                                   # (B, P) source row of each pool label
    signs = np.broadcast_to(fa["sign"], (B, N)).copy()
    S = np.broadcast_to(np.nan_to_num(fa["S"]), (B, N)).copy()
    raw = np.broadcast_to(np.nan_to_num(fa["raw"]), (B, N)).copy()
    signs[:, pool_idx] = fa["sign"][src]
    S[:, pool_idx] = np.nan_to_num(fa["S"])[src]
    raw[:, pool_idx] = np.nan_to_num(fa["raw"])[src]
    expected = masked_sign_baseline(fa["R"], fa["usable"], signs, min_history)
    cohort = residual_cohort(fa["pool"], fa["usable"], expected)
    E = np.nan_to_num(fa["R"][None, :, :] - expected)
    zs = cal.tie_policy_scores_masked(S, raw, cohort, policy)
    mi = cal._masked_gcmi(S, E, cohort, zs=zs)
    Hn = E.shape[2]
    mk = np.broadcast_to(cohort[:, None, :], (B, Hn, N))
    Et = np.transpose(E, (0, 2, 1))
    pear = np.abs(gcmi._corr_rows(np.broadcast_to(S[:, None, :], (B, Hn, N)), Et, mk))
    nb = cohort.sum(axis=1)
    rS = gcmi.average_ranks_2d(S, cohort)
    rE = gcmi.average_ranks_2d(Et.reshape(B * Hn, N), np.repeat(cohort, Hn, axis=0)).reshape(B, Hn, N)
    spear = np.abs(gcmi._corr_rows(np.broadcast_to(rS[:, None, :], (B, Hn, N)), rE, mk))
    return {"gcmi": mi, "pearson_abs": pear, "spearman_abs": spear, "cohort_n": nb, "expected": expected,
            "cohort": cohort}


def run_null_b(fa, pool_idx, perms, min_history, policy) -> Dict[str, np.ndarray]:
    parts = [null_b_batch(fa, pool_idx, perms[a:a + CHUNK], min_history, policy) for a in range(0, len(perms), CHUNK)]
    return {k: np.concatenate([p[k] for p in parts]) for k in ("gcmi", "pearson_abs", "spearman_abs", "cohort_n")}


def baseline_equivalence(fa: Dict[str, Any], pool_idx: np.ndarray, perms: np.ndarray, spec: Dict[str, Any],
                         horizons: Sequence[int]) -> Dict[str, Any]:
    """Replica == frozen add_baseline on permuted REAL frames (first K permutations)."""
    x = fa["frame"]
    worst, checked = 0.0, 0
    for b in range(perms.shape[0]):
        signs = fa["sign"].copy()
        signs[pool_idx] = fa["sign"][pool_idx[perms[b]]]
        f = x.copy()
        f["surprise_sign"] = pd.array([None if s == NO_SIGN else int(s) for s in signs], dtype="Int64")
        frozen = add_baseline(f, spec)
        rep = masked_sign_baseline(fa["R"], fa["usable"], signs[None, :], int(spec["baseline"]["min_history"]))[0]
        for j, h in enumerate(horizons):
            fr = frozen[f"expected_post{h}m_ret"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
            if not np.array_equal(np.isnan(fr), np.isnan(rep[:, j])):
                return {"equal": False, "permutation": b, "horizon": h, "reason": "definedness differs"}
            d = np.abs(fr - rep[:, j])
            worst = max(worst, float(np.nanmax(d)) if np.isfinite(d).any() else 0.0)
            checked += 1
    return {"equal": worst <= 1e-12, "max_abs_diff": worst, "permutations": int(perms.shape[0]), "horizon_checks": checked}


def stored_identity_check(fa: Dict[str, Any], expected_obs: np.ndarray, cohort_obs: np.ndarray,
                          horizons: Sequence[int]) -> Dict[str, Any]:
    """Identity permutation reproduces the frozen v2 expected / residual columns, residual cohort
    and the within-cohort residual ranks."""
    x = fa["frame"]
    worst_e, worst_r, ranks_equal, pattern_equal = 0.0, 0.0, True, True
    stored_cohort = fa["pool"].copy()
    for j, h in enumerate(horizons):
        se = x[f"expected_post{h}m_ret"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
        sr = x[f"resid_post{h}m_ret"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
        stored_cohort &= fa["usable"][:, j] & (x[f"baseline_post{h}m_status"] == "ok").to_numpy() & ~np.isnan(sr)
        pattern_equal &= bool(np.array_equal(np.isnan(se), np.isnan(expected_obs[:, j])))
        worst_e = max(worst_e, float(np.nanmax(np.abs(se - expected_obs[:, j]))))
        rep_r = np.where(fa["usable"][:, j], fa["R"][:, j] - expected_obs[:, j], np.nan)
        worst_r = max(worst_r, float(np.nanmax(np.abs(sr - rep_r))))
    for j, h in enumerate(horizons):
        sr = x[f"resid_post{h}m_ret"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)[cohort_obs]
        rep_r = (fa["R"][:, j] - expected_obs[:, j])[cohort_obs]
        ranks_equal &= bool(np.array_equal(gcmi.average_ranks(sr), gcmi.average_ranks(rep_r)))
    return {"expected_definedness_equal": pattern_equal, "max_abs_diff_expected": worst_e,
            "max_abs_diff_residual": worst_r, "cohort_equal_to_stored": bool(np.array_equal(stored_cohort, cohort_obs)),
            "residual_ranks_equal": ranks_equal,
            "ok": pattern_equal and worst_e <= 1e-12 and worst_r <= 1e-12 and bool(np.array_equal(stored_cohort, cohort_obs)) and ranks_equal}


# ---------------------------------------------------------------------------------------------
# matched raw and bootstrap (frozen library calls)
# ---------------------------------------------------------------------------------------------
def fixed_response_panel(S, raw, Y, perms, policy) -> Dict[str, np.ndarray]:
    zs = cal.tie_policy_scores(S, raw, policy, None)
    zy = np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(Y.shape[1])])
    obs = gcmi.gcmi_from_scores(zs[None, :], zy.T[None, :, :])[0]
    null = gcmi.gcmi_permutation_matrix(zs, zy, perms)
    out = {"obs": obs, "null": null}
    for name, xs, ys in (("pearson", S, Y),
                         ("spearman", gcmi.average_ranks(S), np.column_stack([gcmi.average_ranks(Y[:, j]) for j in range(Y.shape[1])]))):
        xc, yc = xs - xs.mean(), ys - ys.mean(axis=0)
        den = np.sqrt((xc * xc).sum() * (yc * yc).sum(axis=0))
        r = (xc @ yc) / den
        with np.errstate(all="ignore"):
            rn = np.abs(xc[perms] @ yc / den)
        out[name] = r
        out[f"{name}_null_abs"] = rn
    return out


def bootstrap_panel(S, raw, Y, bidx, policy, tie_rng) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    boot = cal._rowwise_gcmi_batch(S[bidx], Y[bidx], lambda X: cal._random_tiebreak_scores(X, tie_rng),
                                   s_scorer=lambda X: cal.tie_policy_scores_bootstrap(X, raw[bidx], policy, tie_rng))
    return np.quantile(boot, 0.025, axis=0), np.quantile(boot, 0.975, axis=0), boot.mean(axis=0)


def pvals(obs: np.ndarray, null: np.ndarray) -> Dict[str, Any]:
    return inf.permutation_pvalues(obs, null)


# ---------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    cfg_bytes = CONFIG.read_bytes()
    cfg = yaml.safe_load(cfg_bytes)
    spec = yaml.safe_load((REPO_ROOT / cfg["input"]["research_spec"]).read_text())
    H = list(cfg["horizons_min"])
    if not set(H) <= set(spec["baseline"]["horizons"]):
        print("refusing to run: a horizon has no frozen baseline", file=sys.stderr)
        return 1
    min_hist = int(spec["baseline"]["min_history"])
    git = prim.git

    # ---------------- reproducibility gates ----------------
    tag_c = git("rev-parse", f"{cfg['calibration_freeze']['tag']}^{{commit}}").stdout.strip()
    ptag_c = git("rev-parse", f"{cfg['primary_result']['tag']}^{{commit}}").stdout.strip()
    head = git("rev-parse", "HEAD").stdout.strip()
    pm_path = cfg["primary_result"]["metadata"]
    pmeta = json.loads((REPO_ROOT / pm_path).read_text())
    pdir = REPO_ROOT / pmeta["outputs"]["directory"]
    primary_outputs_intact = all(prim.sha((pdir / f).read_bytes()) == h for f, h in pmeta["outputs"]["files_sha256"].items())
    status = git("status", "--porcelain").stdout.splitlines()
    tracked_dirty = [l for l in status if not l.startswith("??")]
    untracked = [l[3:] for l in status if l.startswith("??")]
    sci = {f["path"] for f in implementation_fingerprint(REPO_ROOT)["files"]}
    gates = {"head": head, "calibration_tag_commit": tag_c, "primary_tag_commit": ptag_c,
             "calibration_tag_matches_config": tag_c == cfg["calibration_freeze"]["commit"],
             "primary_tag_matches_config": ptag_c == cfg["primary_result"]["commit"],
             "head_contains_calibration_tag": git("merge-base", "--is-ancestor", tag_c, "HEAD").returncode == 0,
             "head_contains_primary_tag": git("merge-base", "--is-ancestor", ptag_c, "HEAD").returncode == 0,
             "frozen_library_identical_to_calibration_tag": git("diff", "--quiet", tag_c, "--", prim.FROZEN_LIBRARY).returncode == 0
             and not git("status", "--porcelain", "--", prim.FROZEN_LIBRARY).stdout.strip(),
             "primary_metadata_unchanged_since_primary_tag": git("diff", "--quiet", ptag_c, "--", pm_path).returncode == 0,
             "primary_outputs_intact": primary_outputs_intact,
             "tracked_files_modified": tracked_dirty, "untracked_files": untracked,
             "untracked_scientific_files": [u for u in untracked if u in sci or u.startswith(prim.FROZEN_LIBRARY)]}
    protocol = {n: {"path": p, "commit": cfg["protocol_commits"][n],
                    "unchanged_since_commit": git("diff", "--quiet", cfg["protocol_commits"][n], "--", p).returncode == 0}
                for n, p in prim.PROTOCOL_DOCS.items()}
    ok = all(gates[k] for k in ("calibration_tag_matches_config", "primary_tag_matches_config", "head_contains_calibration_tag",
                                "head_contains_primary_tag", "frozen_library_identical_to_calibration_tag",
                                "primary_metadata_unchanged_since_primary_tag", "primary_outputs_intact")) \
        and not tracked_dirty and not gates["untracked_scientific_files"] and all(p["unchanged_since_commit"] for p in protocol.values())
    if not ok:
        print(f"refusing to run: gate failed: {json.dumps(gates, indent=1)} {protocol}", file=sys.stderr)
        return 1

    meta_in = json.loads((REPO_ROOT / cfg["input"]["research_metadata"]).read_text())
    ds = meta_in["outputs"]["event_response"]
    df = pd.read_parquet(REPO_ROOT / ds["path"])
    content = canonical_content_sha256(df, ds["hash_key"])
    if content != ds["content_sha256"] or len(df) != ds["rows"]:
        print("refusing to run: dataset content hash mismatch", file=sys.stderr)
        return 1
    started = dt.datetime.now(dt.timezone.utc)
    impl = implementation_fingerprint(REPO_ROOT)
    est = cfg["estimator"]
    B, Bb = int(cfg["permutations"]), int(cfg["bootstrap"]["replicates"])
    strat = f"stratification={cfg['strata']['policy_label']}"
    pos = {nme: i for i, nme in enumerate(cfg["strata"]["order"])}

    audit, res, checks = {}, {}, {}
    for fam in cfg["families"]:
        fa = family_arrays(df, fam, cfg["symbol"], H)
        x = fa["frame"]
        pool_idx = np.flatnonzero(fa["pool"])
        ident = np.arange(len(pool_idx))[None, :]
        obs_b = null_b_batch(fa, pool_idx, ident, min_hist, est["tie_policy"])
        cohort_obs = obs_b["cohort"][0]
        expected_obs = obs_b["expected"][0]
        ci = np.flatnonzero(cohort_obs)
        rel = list(x["release_id"].iloc[ci])
        resid_id = prim.cohort_id(rel)
        raw_cs = fa["pool"] & fa["usable"].all(axis=1)
        audit[fam] = {"total_candidate_rows": int(len(x)), "usable_events": int(filters.usable_events(x).sum()),
                      "valid_standardized_surprise_label_pool": int(fa["pool"].sum()),
                      "primary_cs_raw_n_context": int(raw_cs.sum()), "matched_raw_n": int(len(ci)), "residual_n": int(len(ci)),
                      "expected_n": int(cfg["expected_n"][fam]), "release_ids_sha256": prim.sha("\n".join(sorted(rel)).encode()),
                      "cohort_id": resid_id, "label_pool_id": prim.cohort_id(list(x["release_id"].iloc[pool_idx])),
                      "split_counts": {k: int(v) for k, v in x["split"].iloc[ci].value_counts().sort_index().items()},
                      "release_ids": rel}
        identity = stored_identity_check(fa, expected_obs, cohort_obs, H)
        problems = []
        if len(ci) != cfg["expected_n"][fam]:
            problems.append(f"residual n {len(ci)} != frozen {cfg['expected_n'][fam]}")
        if raw_cs.sum() != cfg["primary_context_n"][fam]:
            problems.append("primary cs_raw context count differs")
        if not identity["ok"]:
            problems.append(f"replica does not reproduce stored v2 residuals: {identity}")
        if not pd.Series(rel).is_unique:
            problems.append("release_id not unique")
        audit[fam]["problems"] = problems
        checks[fam] = {"stored_identity": identity}
        if problems:
            print(f"STOP: {fam}: {problems}", file=sys.stderr)
            return 1

        # ---- Null B permutations: label pool, within chronological strata ----
        pool_labels = inf.merge_small_strata(x["split"].iloc[pool_idx].map(pos).to_numpy(), cfg["strata"]["min_stratum_size"])
        nb_parts = ("permutation_nullB", f"family={fam}", f"cohort={audit[fam]['label_pool_id']}", strat)
        permsB = inf.stratified_permutations(pool_labels, B, rng_for(*nb_parts))
        checks[fam]["baseline_equivalence"] = baseline_equivalence(
            fa, pool_idx, permsB[: int(cfg["baseline_equivalence_check_permutations"])], spec, H)
        if not checks[fam]["baseline_equivalence"]["equal"]:
            print(f"STOP: {fam}: replica != frozen add_baseline: {checks[fam]['baseline_equivalence']}", file=sys.stderr)
            return 1
        nullB = run_null_b(fa, pool_idx, permsB, min_hist, est["tie_policy"])

        # ---- matched raw on the SAME cohort: stratified release permutation ----
        S_c, raw_c = fa["S"][ci], fa["raw"][ci]
        R_c = fa["R"][ci]
        E_c = (fa["R"] - expected_obs)[ci]
        c_labels = inf.merge_small_strata(x["split"].iloc[ci].map(pos).to_numpy(), cfg["strata"]["min_stratum_size"])
        mr_parts = ("permutation", f"family={fam}", f"cohort={resid_id}", strat)
        permsC = inf.stratified_permutations(c_labels, B, rng_for(*mr_parts))
        mr = fixed_response_panel(S_c, raw_c, R_c, permsC, est["tie_policy"])

        # ---- bootstrap (shared release resamples; policy B inside every replicate) ----
        bt_parts = ("bootstrap", f"family={fam}", f"cohort={resid_id}", strat)
        bidx = inf.stratified_bootstrap(c_labels, Bb, rng_for(*bt_parts))
        boot = {}
        for rtype, Y in (("raw", R_c), ("resid", E_c)):
            tie = rng_for(*bt_parts, "symbol=SPY", f"response={rtype}", f"estimator={est['key']}")
            boot[rtype] = bootstrap_panel(S_c, raw_c, Y, bidx, est["tie_policy"], tie)

        obs_r = obs_b["gcmi"][0]
        res[fam] = {
            "resid": {"obs": obs_r, "pv": pvals(obs_r, nullB["gcmi"]), "boot": boot["resid"],
                      "pearson": None, "spearman": None,
                      "p_pearson": (1 + (nullB["pearson_abs"] >= obs_b["pearson_abs"][0][None, :]).sum(0)) / (B + 1),
                      "p_spearman": (1 + (nullB["spearman_abs"] >= obs_b["spearman_abs"][0][None, :]).sum(0)) / (B + 1),
                      "perm_cohort_n": nullB["cohort_n"]},
            "raw": {"obs": mr["obs"], "pv": pvals(mr["obs"], mr["null"]), "boot": boot["raw"],
                    "pearson": mr["pearson"], "spearman": mr["spearman"],
                    "p_pearson": (1 + (mr["pearson_null_abs"] >= np.abs(mr["pearson"])[None, :]).sum(0)) / (B + 1),
                    "p_spearman": (1 + (mr["spearman_null_abs"] >= np.abs(mr["spearman"])[None, :]).sum(0)) / (B + 1)},
            "seeds": {k: {"key": seed_key(*p), "seed": derive_seed(*p)} for k, p in
                      (("nullB_permutation", nb_parts), ("matched_raw_permutation", mr_parts), ("bootstrap_indices", bt_parts))},
            "strata": {"label_pool": {int(s): int((pool_labels == s).sum()) for s in np.unique(pool_labels)},
                       "cohort": {int(s): int((c_labels == s).sum()) for s in np.unique(c_labels)}}}
        # signed residual correlations on the observed cohort (benchmarks)
        res[fam]["resid"]["pearson"] = np.array([gcmi._corr_rows(S_c, E_c[:, j]) for j in range(len(H))])
        res[fam]["resid"]["spearman"] = np.array([gcmi._corr_rows(gcmi.average_ranks(S_c), gcmi.average_ranks(E_c[:, j]))
                                                  for j in range(len(H))])

    holm = prim.holm_closed_testing({f: res[f]["resid"]["pv"]["p_panel"] for f in cfg["residual_holm_family"]},
                                    {f: {"horizons": H, "p": list(res[f]["resid"]["pv"]["p_horizon_adjusted"])}
                                     for f in cfg["residual_holm_family"]}, float(cfg["alpha"]))

    # ---------------- outputs ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = {"raw": [], "resid": []}
    for fam in cfg["families"]:
        for rtype in ("raw", "resid"):
            r = res[fam][rtype]
            pv = r["pv"]
            lo, hi, bmean = r["boot"]
            for j, h in enumerate(H):
                eff = float(r["obs"][j] - pv["null_mean"][j])
                row = {"family": fam, "symbol": cfg["symbol"], "response_type": "residual" if rtype == "resid" else "matched_raw",
                       "cohort_kind": "cs_resid", "cohort_id": audit[fam]["cohort_id"], "estimator": "gcmi",
                       "tie_policy": est["tie_policy"], "horizon_min": h,
                       "session_label": "session-boundary-adjacent" if h == cfg["session_boundary_horizon_min"] else "premarket",
                       "n": audit[fam]["residual_n"],
                       "gcmi_raw_bits": float(r["obs"][j]), "null_mean_bits": float(pv["null_mean"][j]),
                       "null_sd_bits": float(pv["null_sd"][j]), "effective_bits": eff,
                       "p_raw": float(pv["p_unadjusted"][j]), "p_horizon_adj": float(pv["p_horizon_adjusted"][j]),
                       "panel_p": float(pv["p_panel"]),
                       "bootstrap_low": float(lo[j] - pv["null_mean"][j]), "bootstrap_high": float(hi[j] - pv["null_mean"][j]),
                       "bootstrap_raw_low": float(lo[j]), "bootstrap_raw_high": float(hi[j]), "bootstrap_mean_raw_bits": float(bmean[j]),
                       "pearson": float(r["pearson"][j]), "spearman": float(r["spearman"][j]),
                       "p_pearson": float(r["p_pearson"][j]), "p_spearman": float(r["p_spearman"][j]),
                       "null_procedure": "nullB_pipeline_aware" if rtype == "resid" else "stratified_release_permutation_fixed_response",
                       "ci_label": "nominal 95% percentile bootstrap interval of effective GCMI (synthetic coverage ~0.93)",
                       "B_perm": B, "B_boot": Bb}
                if rtype == "resid":
                    row.update(holm_adjusted_p=float(holm[fam]["holm_adjusted_p"]), panel_rejected_secondary_holm=holm[fam]["panel_rejected"],
                               secondary_detectable=h in holm[fam]["detectable_horizons"],
                               nullB_perm_cohort_n_mean=float(r["perm_cohort_n"].mean()),
                               nullB_perm_cohort_n_min=int(r["perm_cohort_n"].min()), nullB_perm_cohort_n_max=int(r["perm_cohort_n"].max()))
                rows[rtype].append(row)
    prim.write_csv(OUT_DIR / "residual_profile.csv", rows["resid"])
    prim.write_csv(OUT_DIR / "matched_raw_profile.csv", rows["raw"])
    comp = []
    for rr, rm in zip(rows["resid"], rows["raw"]):
        comp.append({"family": rr["family"], "horizon_min": rr["horizon_min"], "n": rr["n"],
                     "matched_raw_eff": rm["effective_bits"], "residual_eff": rr["effective_bits"],
                     "delta_eff": rr["effective_bits"] - rm["effective_bits"],
                     "matched_raw_p_adj": rm["p_horizon_adj"], "residual_p_adj": rr["p_horizon_adj"],
                     "residual_boot_low": rr["bootstrap_low"], "residual_boot_high": rr["bootstrap_high"],
                     "matched_raw_boot_low": rm["bootstrap_low"], "matched_raw_boot_high": rm["bootstrap_high"],
                     "delta_note": "descriptive only; no delta-specific test was frozen"})
    prim.write_csv(OUT_DIR / "matched_vs_residual.csv", comp)
    prim.write_csv(OUT_DIR / "residual_family_holm.csv",
                   [dict(family=f, correction="secondary residual family-level correction (Holm, 3 panels)",
                         **{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in holm[f].items()})
                    for f in cfg["residual_holm_family"]])
    prim.write_csv(OUT_DIR / "cohort_release_ids.csv",
                   [{"family": f, "release_id": rid} for f in cfg["families"] for rid in audit[f]["release_ids"]])
    note = "* 60m: 08:30 ET releases -- window ends at the 09:30 regular-session open (session-boundary-adjacent)."
    LBL = prim.FAMILY_LABEL
    prim.svg_points_plot(OUT_DIR / "plot1_matched_raw_vs_residual.svg",
                         "SPY cs_resid: matched raw (filled) vs residual (open) effective GCMI", "effective GCMI (bits)", H,
                         [s for f in cfg["families"] for s in (
                             {"label": f"{LBL[f]} matched raw", "color": prim.COLORS[f],
                              "y": [x["matched_raw_eff"] for x in comp if x["family"] == f]},
                             {"label": f"{LBL[f]} residual", "color": prim.COLORS[f], "marker": "s",
                              "y": [x["residual_eff"] for x in comp if x["family"] == f]})],
                         cfg["session_boundary_horizon_min"], note + "\nSame releases for both (n = 77 / 77 / 97). Residual inference: pipeline-aware Null B.")
    prim.svg_points_plot(OUT_DIR / "plot2_residual_information_profile.svg",
                         "SPY residual information profile: effective GCMI (bits)", "effective GCMI (bits)", H,
                         [{"label": LBL[f], "color": prim.COLORS[f], "y": [x["effective_bits"] for x in rows["resid"] if x["family"] == f],
                           "lo": [x["bootstrap_low"] for x in rows["resid"] if x["family"] == f],
                           "hi": [x["bootstrap_high"] for x in rows["resid"] if x["family"] == f]} for f in cfg["families"]],
                         cfg["session_boundary_horizon_min"],
                         note + "\nBars: nominal 95% percentile bootstrap interval (synthetic coverage ~0.93, not 0.95). Null B mean subtracted.")
    prim.svg_points_plot(OUT_DIR / "plot3_residual_correlation_benchmarks.svg",
                         "SPY residual: Pearson (filled) / Spearman (open) benchmarks", "correlation", H,
                         [s for f in cfg["families"] for s in (
                             {"label": f"{LBL[f]} Pearson", "color": prim.COLORS[f], "y": [x["pearson"] for x in rows["resid"] if x["family"] == f]},
                             {"label": f"{LBL[f]} Spearman", "color": prim.COLORS[f], "marker": "s",
                              "y": [x["spearman"] for x in rows["resid"] if x["family"] == f]})],
                         cfg["session_boundary_horizon_min"], note)
    summary = {"analysis": "residual_spy_v1", "secondary": True, "real_market_mi_computed": True,
               "residual_null": "nullB_pipeline_aware (Null A never used)",
               "estimator_description": "permutation-null-adjusted Gaussian-copula dependence in bits (primarily monotonic)",
               "caveat": "the baseline conditions on sign(surprise_raw): I(S;E_h) is baseline-adjusted dependence, not I(S;R_h | market information)",
               "cohort_audit": {f: {k: v for k, v in audit[f].items() if k != "release_ids"} for f in cfg["families"]},
               "replica_checks": checks, "residual_holm": holm,
               "seeds": {f: res[f]["seeds"] for f in cfg["families"]}, "strata": {f: res[f]["strata"] for f in cfg["families"]},
               "not_run": ["QQQ", "KSG", "other families", "CMI", "decay fitting"]}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    outputs = {p.name: prim.sha(p.read_bytes()) for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    meta = {"dataset": "information_decay_residual_spy_v1", "secondary": True, "real_market_mi_computed": True,
            "generated_at_utc": started.isoformat(timespec="seconds"),
            "calibration_freeze": cfg["calibration_freeze"], "primary_result": cfg["primary_result"],
            "protocol": protocol, "gates": gates, "implementation_fingerprint": impl,
            "config": {"path": str(CONFIG.relative_to(REPO_ROOT)), "sha256": prim.sha(cfg_bytes)},
            "input": {"event_response_path": ds["path"], "content_sha256": content, "rows": len(df),
                      "file_sha256": prim.sha((REPO_ROOT / ds["path"]).read_bytes()),
                      "market_dataset_fingerprint": meta_in["inputs"]["market_dataset"]["fingerprint"]},
            "cohorts": {f: {k: audit[f][k] for k in ("residual_n", "release_ids_sha256", "cohort_id", "label_pool_id", "split_counts")}
                        for f in cfg["families"]},
            "outputs": {"directory": str(OUT_DIR.relative_to(REPO_ROOT)), "files_sha256": outputs,
                        "combined_sha256": prim.sha(json.dumps(outputs, sort_keys=True).encode())}}
    METADATA.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"cohorts": {f: audit[f]["residual_n"] for f in audit},
                      "replica": {f: [checks[f]["stored_identity"]["ok"], checks[f]["baseline_equivalence"]["equal"]] for f in checks},
                      "outputs_combined_sha256": meta["outputs"]["combined_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
