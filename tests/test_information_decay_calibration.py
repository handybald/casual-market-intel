"""Tests for the M3 synthetic MI-estimator calibration (src/research/information_decay).

Everything here is synthetic; no test reads real market data, and the isolation guard is tested to
refuse it.
"""
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.research.information_decay import (baseline_sim, benchmarks, calibration as cal, gcmi,
                                            inference as inf, ksg, special, truth)
from src.research.information_decay.isolation import RealDataAccessError, real_data_guard
from src.research.information_decay.seeds import derive_seed, rng_for, seed_key

REPO = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((REPO / "config" / "information_decay_v1.yaml").read_text())
STRENGTHS = {"s3b_tanh": 0.4319569, "s4_u_shape": 0.2677104, "s5_hetero": 0.7775047, "s6_t3": 0.4660782}


def _gauss(n, rho, seed):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    return x, rho * x + math.sqrt(1 - rho * rho) * rng.standard_normal(n)


# --- units, special functions -------------------------------------------------------------------
def test_bits_nats_conversion():
    assert special.nats_to_bits(math.log(2)) == pytest.approx(1.0)
    assert special.bits_to_nats(1.0) == pytest.approx(math.log(2))
    assert float(benchmarks.gaussian_mi_bits(0.5)) == pytest.approx(-0.5 * math.log2(0.75))


def test_ndtri_and_digamma():
    np.testing.assert_allclose(special.ndtri(np.array([0.5, 0.975, 0.025, 1e-12])),
                               [0.0, 1.959963984540054, -1.959963984540054, -7.034483825777], atol=1e-9)
    np.testing.assert_allclose(special.digamma(np.array([1.0, 0.5, 2.5])),
                               [-0.5772156649015329, -1.9635100260214235, 0.7031566406452432], atol=1e-12)
    tab = special.digamma_int_table(6)
    np.testing.assert_allclose(tab[1:], special.digamma(np.arange(1.0, 7.0)), atol=1e-12)


def test_wilson_interval_contains_estimate():
    lo, hi = special.wilson_interval(100, 2000)
    assert lo < 0.05 < hi and hi - lo < 0.025


# --- GCMI ---------------------------------------------------------------------------------------
def test_gcmi_matches_known_gaussian_mi():
    x, y = _gauss(40000, 0.5, 1)
    assert gcmi.gcmi_bits(x, y) == pytest.approx(-0.5 * math.log2(0.75), abs=0.006)


def test_gcmi_invariant_to_monotone_marginal_transforms():
    x, y = _gauss(500, 0.35, 2)
    assert gcmi.gcmi_bits(x ** 3, np.exp(1.5 * y)) == pytest.approx(gcmi.gcmi_bits(x, y), abs=1e-12)


def test_gcmi_tie_handling_is_average_rank_and_order_free():
    r = gcmi.average_ranks(np.array([3.0, 1.0, 2.0, 2.0]))
    np.testing.assert_array_equal(r, [4.0, 1.0, 2.5, 2.5])
    z = gcmi.copnorm(np.array([0.0, 0.1, 0.0, -0.1, 0.0]))
    assert z[0] == z[2] == z[4] == 0.0                      # tied values -> identical scores
    rng = np.random.default_rng(3)
    x = rng.choice([-0.1, 0.0, 0.1], size=80)
    y = rng.standard_normal(80)
    perm = rng.permutation(80)                              # reordering rows never changes the estimate
    assert gcmi.gcmi_bits(x, y) == pytest.approx(gcmi.gcmi_bits(x[perm], y[perm]), abs=1e-12)


def test_average_ranks_2d_matches_1d_with_mask():
    rng = np.random.default_rng(4)
    X = rng.choice([1.0, 2.0, 3.0], size=(5, 12))
    mask = rng.random((5, 12)) > 0.3
    R2 = gcmi.average_ranks_2d(X, mask)
    for b in range(5):
        np.testing.assert_allclose(R2[b, mask[b]], gcmi.average_ranks(X[b, mask[b]]))
        assert np.all(R2[b, ~mask[b]] == 0)


def test_gcmi_permutation_matrix_equals_direct_computation():
    x, y = _gauss(50, 0.3, 5)
    Y = np.column_stack([y, y ** 3 + 0.1])
    zs, zy = gcmi.copnorm(x), np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(2)])
    perms = inf.unrestricted_permutations(50, 7, np.random.default_rng(0))
    M = gcmi.gcmi_permutation_matrix(zs, zy, perms)
    for b in range(7):
        for j in range(2):
            assert M[b, j] == pytest.approx(gcmi.gcmi_bits(x[perms[b]], Y[:, j]), abs=1e-12)


def test_ince_bias_diagnostic_positive_and_shrinking():
    assert gcmi.ince_bias_bits(40) > gcmi.ince_bias_bits(120) > 0


# --- KSG ----------------------------------------------------------------------------------------
def test_ksg_close_to_known_gaussian_mi_and_in_bits():
    x, y = _gauss(3000, 0.5, 6)
    est = ksg.ksg_bits(x, y, (3, 5, 10))
    assert np.all(np.abs(est - (-0.5 * math.log2(0.75))) < 0.03)
    # bits = nats / ln 2, checked against a direct nats implementation for one k
    xz, yz = ksg.zscore(x[:200]), ksg.zscore(y[:200])
    dx, dy = np.abs(xz[:, None] - xz), np.abs(yz[:, None] - yz)
    np.fill_diagonal(dx, np.inf)
    np.fill_diagonal(dy, np.inf)
    eps = np.sort(np.maximum(dx, dy), axis=1)[:, 4][:, None]
    nx, ny = (dx < eps).sum(1), (dy < eps).sum(1)
    nats = special.digamma(np.array([5.0]))[0] + special.digamma(np.array([200.0]))[0] - \
        np.mean(special.digamma(nx + 1.0) + special.digamma(ny + 1.0))
    assert ksg.ksg_bits(x[:200], y[:200], (5,))[0] == pytest.approx(nats / math.log(2), abs=1e-10)


def test_ksg_reproducible_and_batch_equals_single():
    x, y = _gauss(60, 0.3, 7)
    a = ksg.ksg_bits(x, y)
    b = ksg.ksg_bits(x, y)
    np.testing.assert_array_equal(a, b)
    perms = inf.unrestricted_permutations(60, 4, np.random.default_rng(1))
    xz, yz = ksg.zscore(x), ksg.zscore(y)
    batch = ksg.ksg_batch_bits(xz[perms], yz, (3, 5, 10))
    for i in range(4):
        np.testing.assert_allclose(batch[i], ksg.ksg_batch_bits(xz[perms[i]][None], yz[None], (3, 5, 10))[0])
    j1 = xz + 1e-10 * rng_for("t", "jitter").standard_normal(60)
    j2 = xz + 1e-10 * rng_for("t", "jitter").standard_normal(60)
    np.testing.assert_array_equal(j1, j2)


def test_ksg_valid_mask_equals_subsample():
    x, y = _gauss(40, 0.4, 8)
    mask = np.ones(40, dtype=bool)
    mask[::4] = False
    xz, yz = ksg.zscore(x[mask]), ksg.zscore(y[mask])
    full_x = np.zeros(40)
    full_y = np.zeros(40)
    full_x[mask], full_y[mask] = xz, yz
    a = ksg.ksg_batch_bits(full_x[None], full_y[None], (3, 5), valid=mask[None])
    b = ksg.ksg_batch_bits(xz[None], yz[None], (3, 5))
    np.testing.assert_allclose(a, b, atol=1e-12)


# --- benchmarks ---------------------------------------------------------------------------------
def test_histogram_mi_fixed_bins_and_correlations():
    x, y = _gauss(400, 0.0, 9)
    assert 0 <= benchmarks.histogram_mi_bits(x, y, 4) < 0.1
    assert benchmarks.spearman(x, x ** 3) == pytest.approx(1.0)
    assert abs(benchmarks.pearson(x, y)) < 0.15


# --- truth engine -------------------------------------------------------------------------------
def test_truth_engine_reproduces_gaussian_mi():
    for rho in (0.15, 0.35):
        assert truth.scenario_true_mi_bits("gaussian", rho) == pytest.approx(-0.5 * math.log2(1 - rho * rho), abs=1e-6)


def test_truth_engine_t_entropy_closed_form_matches_numeric():
    # MI with zero signal is zero for heavy-tailed noise as well
    assert truth.scenario_true_mi_bits("heavy_tail_t3", 0.0) == pytest.approx(0.0, abs=1e-6)


# --- seeds --------------------------------------------------------------------------------------
def test_seed_derivation_is_stable_and_key_specific():
    assert seed_key("calibration", "s1", 60, 3, "data") == "m3_information_decay|v1|calibration|s1|60|3|data"
    assert derive_seed("calibration", "s1", 60, 3, "data") == derive_seed("calibration", "s1", 60, 3, "data")
    assert derive_seed("calibration", "s1", 60, 3, "data") != derive_seed("calibration", "s1", 60, 4, "data")
    np.testing.assert_array_equal(rng_for("a", 1).random(5), rng_for("a", 1).random(5))


# --- permutation / resampling -------------------------------------------------------------------
def test_stratified_permutations_stay_within_strata():
    labels = np.repeat([0, 1, 2], [10, 8, 12])
    P = inf.stratified_permutations(labels, 50, np.random.default_rng(0))
    assert np.all(labels[P] == labels[None, :])
    assert all(sorted(row) == list(range(30)) for row in P)
    assert not np.all(P == np.arange(30))


def test_merge_small_strata_rule():
    np.testing.assert_array_equal(inf.merge_small_strata(np.repeat([0, 1, 2], [0, 14, 42]), 8),
                                  np.repeat([0, 1], [14, 42]))
    np.testing.assert_array_equal(inf.merge_small_strata(np.repeat([0, 1, 2], [5, 20, 30]), 8),
                                  np.repeat([0, 1], [25, 30]))            # forward merge
    np.testing.assert_array_equal(inf.merge_small_strata(np.repeat([0, 1, 2], [20, 30, 3]), 8),
                                  np.repeat([0, 1], [20, 33]))            # last merges backward
    np.testing.assert_array_equal(inf.merge_small_strata(np.repeat([0, 1, 2], [11, 23, 43]), 8),
                                  np.repeat([0, 1, 2], [11, 23, 43]))


def test_permutation_preserves_release_grouping():
    row_release = np.array([0, 0, 1, 1, 2, 2, 3, 3])     # two linked rows per release (e.g. SPY, QQQ)
    rel_perm = inf.unrestricted_permutations(4, 20, np.random.default_rng(2))
    rows = inf.release_row_permutations(row_release, rel_perm)
    for b in range(20):
        src_release = row_release[rows[b]]
        for r in range(4):
            assert len(set(src_release[row_release == r])) == 1              # linked rows move together
            assert src_release[row_release == r][0] == rel_perm[b, r]
        assert np.all(rows[b, ::2] % 2 == 0) and np.all(rows[b, 1::2] % 2 == 1)   # position kept


def test_max_statistic_pvalues():
    obs = np.array([0.5, 0.1])
    null = np.array([[0.6, 0.0], [0.2, 0.3], [0.1, 0.05], [0.0, 0.2]])
    pv = inf.permutation_pvalues(obs, null)
    np.testing.assert_allclose(pv["p_unadjusted"], [(1 + 1) / 5, (1 + 2) / 5])
    # max-null per permutation = [0.6, 0.3, 0.1, 0.2]
    np.testing.assert_allclose(pv["p_horizon_adjusted"], [(1 + 1) / 5, (1 + 4) / 5])
    assert pv["p_panel"] == pytest.approx(2 / 5)
    assert np.all(pv["p_horizon_adjusted"] >= pv["p_unadjusted"])
    assert pv["p_unadjusted"].min() > 0                                     # never zero


def test_null_independence_type_one_rate_reasonable():
    rejections = 0
    for rep in range(150):
        rng = rng_for("test", "null", rep)
        x, Y = rng.standard_normal(60), rng.standard_normal((60, 2))
        zs = gcmi.copnorm(x)
        zy = np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(2)])
        obs = gcmi.gcmi_from_scores(zs[None, :], zy.T[None])[0]
        null = gcmi.gcmi_permutation_matrix(zs, zy, inf.unrestricted_permutations(60, 99, rng))
        rejections += inf.permutation_pvalues(obs, null)["p_panel"] <= 0.05
    assert 0.0 <= rejections / 150 <= 0.12


def test_bootstrap_and_subsample_are_release_level_within_strata():
    labels = np.repeat([0, 1, 2], [10, 8, 12])
    Bt = inf.stratified_bootstrap(labels, 40, np.random.default_rng(3))
    assert np.all(labels[Bt] == labels[None, :])
    assert any(len(set(r)) < 30 for r in Bt)                     # with replacement
    S = inf.stratified_subsample(labels, 40, 0.7, np.random.default_rng(3))
    assert S.shape[1] == 7 + 5 + 8
    assert all(len(set(r)) == len(r) for r in S)                 # without replacement
    x = np.arange(30.0)
    Y = np.column_stack([x * 10, x * 100])
    assert np.all(Y[Bt][:, :, 0] == x[Bt] * 10)                  # a release's horizons move together


# --- sign-conditioned baseline replica ------------------------------------------------------------
def test_baseline_replica_reproduces_frozen_m2_add_baseline():
    from src.research.baseline import add_baseline
    rng = np.random.default_rng(11)
    N, H = 70, [1, 5, 15, 30, 60]
    sign = rng.choice([-1, 0, 1], size=N, p=[0.4, 0.25, 0.35])
    R = rng.standard_normal((N, 5))
    df = pd.DataFrame({"symbol": "SPY", "event_family": "SYN", "surprise_sign": pd.array(sign, dtype="Int64"),
                       "release_timestamp_utc": pd.date_range("2020-01-01", periods=N, freq="30D", tz="UTC"),
                       "event_status": "usable"})
    for j, h in enumerate(H):
        df[f"post{h}m_status"] = "ok"
        df[f"post{h}m_ret"] = R[:, j]
    spec = {"baseline": {"name": "expanding_family_sign_mean", "version": 1, "min_history": 8,
                         "horizons": H, "evaluation_paradigm": "walk_forward_online_expanding"}}
    frozen = add_baseline(df, spec)
    exp, cnt = baseline_sim.expanding_sign_baseline(R, sign, 8)
    for j, h in enumerate(H):
        f = frozen[f"expected_post{h}m_ret"].astype(float).to_numpy()
        np.testing.assert_array_equal(np.isnan(f), np.isnan(exp[:, j]))
        np.testing.assert_allclose(f[~np.isnan(f)], exp[~np.isnan(exp[:, j]), j], atol=1e-12)
    np.testing.assert_array_equal(frozen["baseline_post1m_n_history"].astype(int).to_numpy(), cnt)


def test_baseline_batch_equals_single_and_depends_on_sign():
    rng = np.random.default_rng(12)
    R = rng.standard_normal((50, 3))
    signs = np.stack([rng.choice([-1, 1], size=50) for _ in range(4)])
    eb, cb = baseline_sim.expanding_sign_baseline_batch(R, signs, 8)
    for b in range(4):
        e, c = baseline_sim.expanding_sign_baseline(R, signs[b], 8)
        np.testing.assert_array_equal(np.isnan(e), np.isnan(eb[b]))
        np.testing.assert_allclose(np.nan_to_num(e), np.nan_to_num(eb[b]))
    assert not np.allclose(np.nan_to_num(eb[0]), np.nan_to_num(eb[1]))   # residual depends on sign(S)


def test_sign_conditioned_residual_depends_on_surprise_by_construction():
    """Under S independent of R, E = R - Rhat(sign(S)) carries a group-specific offset shared by
    all releases with the same sign: the mechanical artefact of methodology §2.1."""
    rng = np.random.default_rng(13)
    N = 400
    S = rng.standard_normal(N)
    R = rng.standard_normal((N, 1)) + 0.0
    exp, _ = baseline_sim.expanding_sign_baseline(R, np.sign(S).astype(int), 8)
    ok = ~np.isnan(exp[:, 0])
    offset = exp[ok, 0]
    pos, neg = np.sign(S[ok]) > 0, np.sign(S[ok]) < 0
    # the expected value (and hence E) is a function of sign(S) and time, not of R alone
    assert offset[pos].std() > 0 and offset[neg].std() > 0
    assert not np.allclose(offset[pos][:20].mean(), offset[neg][:20].mean())


# --- residual analyses: Null A vs pipeline-aware Null B ------------------------------------------
@pytest.fixture(scope="module")
def ctx():
    return cal.make_context(CFG, "test", STRENGTHS)


def test_pipeline_aware_identity_equals_fixed_residual_statistic(ctx):
    cell = dict(next(c for c in CFG["cells"] if c["id"] == "s8_null_modeB_disc"), _truth=0.0)
    out = cal.run_replicate(ctx, cell, 60, 0)
    np.testing.assert_allclose(out["gcmi|nullA|obs"], out["gcmi|nullB|obs"], atol=1e-12)
    np.testing.assert_allclose(out["ksg5|nullA|obs"], out["ksg5|nullB|obs"], atol=1e-9)
    assert out["gcmi|nullB|p_panel"][0] > 0


def test_pipeline_aware_permutation_recomputes_baseline_and_cohort(ctx):
    cell = next(c for c in CFG["cells"] if c["id"] == "s8_null_modeA")
    sim = cal.sim_residual(ctx, cell, 60, 1)
    exp, cnt = baseline_sim.expanding_sign_baseline(sim["R"], sim["sign"], 8)
    cohort = ~np.isnan(sim["S"]) & (cnt >= 8)
    assert cohort.sum() == 60 and cohort[-1]                      # truncated after the 60th cohort release
    pool = np.flatnonzero(~np.isnan(sim["S"]))
    perm = np.random.default_rng(0).permutation(len(pool))
    signs = sim["sign"].copy()
    signs[pool] = sim["sign"][pool][perm]
    exp_b, _ = baseline_sim.expanding_sign_baseline(sim["R"], signs, 8)
    assert not np.allclose(np.nan_to_num(exp), np.nan_to_num(exp_b))   # E is rebuilt under the permutation
    assert np.all(signs[:12] == sim["sign"][:12])                       # burn-in labels never permuted


def test_tie_policies(ctx):
    S = np.array([0.1, -0.3, 0.12, 0.5, 0.11, -0.29])
    raw = np.array([0.0, -0.1, 0.0, 0.1, 0.0, -0.1])
    zA = cal.tie_policy_scores(S, raw, "A", None)
    zB = cal.tie_policy_scores(S, raw, "B", None)
    zC = cal.tie_policy_scores(S, raw, "C", rng_for("t", "C"))
    assert len(set(np.round(zA, 12))) == 6
    assert zB[0] == zB[2] == zB[4] and zB[1] == zB[5]                     # raw tie groups collapse
    assert sorted(np.round(zC, 12)) == sorted(np.round(zA, 12))           # same score set, group-ordered
    assert max(zC[[1, 5]]) < min(zC[[0, 2, 4]]) < max(zC[[0, 2, 4]]) < zC[3]
    np.testing.assert_array_equal(zC, cal.tie_policy_scores(S, raw, "C", rng_for("t", "C")))


def test_expanding_standardization_is_causal():
    raw = np.array([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 100, 14])
    z = cal.expanding_standardize(raw, 12, 1e-9)
    assert np.all(np.isnan(z[:12]))
    assert z[12] == pytest.approx((13 - raw[:12].mean()) / raw[:12].std(ddof=1))
    z2 = cal.expanding_standardize(np.r_[raw[:13], 0.0, 0.0], 12, 1e-9)
    assert z2[12] == z[12]                                                  # future values never matter
    assert np.isnan(cal.expanding_standardize(np.r_[np.ones(12), 2.0], 12, 1e-9)[12])  # zero dispersion


def test_cumulative_noise_correlation_structure():
    Z = cal.cumulative_noise(np.random.default_rng(0), 200000, (1, 5, 15, 30, 60))
    C = np.corrcoef(Z.T)
    assert C[0, 1] == pytest.approx(math.sqrt(1 / 5), abs=0.01)
    assert C[3, 4] == pytest.approx(math.sqrt(30 / 60), abs=0.01)
    np.testing.assert_allclose(Z.std(axis=0), 1.0, atol=0.01)


# --- calibration runner determinism ---------------------------------------------------------------
def test_calibration_output_deterministic_across_workers(ctx):
    cells = ["s1_null_modeA", "s8_null_modeB_disc", "s9_null_discrete", "s2_rho035_modeA"]
    a = cal.run_calibration(ctx, str(REPO), workers=1, cell_ids=cells, sizes=[40])["rows"]
    b = cal.run_calibration(ctx, str(REPO), workers=2, cell_ids=cells, sizes=[40])["rows"]
    assert len(a) == len(b) > 0
    for ra, rb in zip(a, b):
        assert ra.keys() == rb.keys()
        for k in ra:
            if isinstance(ra[k], float) and math.isnan(ra[k]):
                assert math.isnan(rb[k])
            else:
                assert ra[k] == rb[k], k


# --- real-data isolation ------------------------------------------------------------------------
def test_guard_refuses_real_market_response_files(tmp_path):
    target = REPO / "data" / "processed" / "event_response" / "event_response_v2.parquet"
    with real_data_guard(REPO):
        with pytest.raises(RealDataAccessError):
            open(target, "rb")
        with pytest.raises(RealDataAccessError):
            open(REPO / "data" / "interim" / "anything.csv")
        with pytest.raises(RealDataAccessError):
            open(tmp_path / "elsewhere.parquet", "wb")
        with pytest.raises(RealDataAccessError):
            pd.read_parquet(target)
        with pytest.raises(RealDataAccessError):
            __import__("os").listdir(REPO / "data" / "processed")
        (tmp_path / "ok.csv").write_text("fine")                  # unrelated files stay accessible
    # the guard is released afterwards
    assert pd.read_parquet is not None and not __import__(
        "src.research.information_decay.isolation", fromlist=["guard_active"]).guard_active()


def test_calibration_package_never_imports_pandas_or_pyarrow():
    code = ("import sys; import src.research.information_decay.calibration, "
            "src.research.information_decay.decisions; "
            "print(int('pandas' in sys.modules), int('pyarrow' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["0", "0"]


def test_calibration_sources_reference_no_real_data_paths():
    sources = list((REPO / "src" / "research" / "information_decay").glob("*.py"))
    sources.append(REPO / "scripts" / "calibrate_information_decay_estimators.py")
    for p in sources:
        text = p.read_text()
        for forbidden in ("event_response_v", "read_parquet(", "data/processed/event_response", "macro_market_response"):
            assert forbidden not in text or p.name == "isolation.py", (p, forbidden)


def test_config_cells_are_synthetic_and_complete():
    ids = [c["id"] for c in CFG["cells"]]
    assert len(ids) == len(set(ids))
    scen = {c["scenario"] for c in CFG["cells"]}
    for s in ("s1_null", "s2_gaussian", "s3a_copula", "s3b_tanh", "s4_u_shape", "s5_hetero", "s6_t3",
              "s7_outliers", "s8_residual", "s9_discrete", "s10_regime"):
        assert s in scen
    assert CFG["horizons_min"] == [1, 5, 15, 30, 60]
    assert CFG["sample_sizes"]["grid"] == [40, 60, 80, 100, 120]
    assert CFG["profiles"]["final"]["reps"] >= 2000


# =================================================================================================
# Amendment 02 fixes
# =================================================================================================
from src.research.information_decay import amendment02 as a2  # noqa: E402

A02 = yaml.safe_load((REPO / "config" / "information_decay_v1_amendment02.yaml").read_text())


def _a02_ctx(cells):
    cfg = dict(CFG)
    cfg["cells"] = cells
    cfg["profiles"] = dict(CFG["profiles"], a02=dict(A02["counts"], perms_gcmi=49, perms_ksg=19,
                                                       boot_gcmi=20, subsample_gcmi=20, reps_coverage=2))
    return cal.make_context(cfg, "a02", STRENGTHS)


def _cell(cid, **extra):
    base = {c["id"]: c for c in CFG["cells"] + A02["new_cells"]}
    c = dict(base[cid])
    c.update(extra)
    c["_truth"] = 0.0 if c.get("is_null") else c.get("_truth")
    return c


# --- cluster-aware coverage ---------------------------------------------------------------------
def test_coverage_uncertainty_is_replicate_clustered(ctx):
    cell = dict(next(c for c in CFG["cells"] if c["id"] == "s2_rho035_modeA"), _truth=0.0943)
    H = len(CFG["horizons_min"])
    by_rep = {}
    for r in range(40):            # horizons perfectly correlated within a replicate
        v = np.full(H, float(r % 2))
        by_rep[r] = {"gcmi|cov_eff|boot_pct": v, "gcmi|cov_obs|boot_pct": v, "gcmi|width|boot_pct": np.ones(H)}
    rows = [x for x in cal.aggregate(ctx, cell, 60, by_rep) if x["procedure"] == "interval"]
    assert len(rows) == 1
    row = rows[0]
    assert row["coverage_eff"] == pytest.approx(0.5)
    naive = math.sqrt(0.25 / (40 * H))                      # treating 200 horizon-trials as independent
    assert row["coverage_eff_cluster_se"] == pytest.approx(np.std([r % 2 for r in range(40)], ddof=1) / math.sqrt(40))
    assert row["coverage_eff_cluster_se"] > 2 * naive
    assert row["coverage_uncertainty"] == "replicate_cluster"
    for h in CFG["horizons_min"]:
        assert row[f"coverage_eff_h{h}"] == pytest.approx(0.5)
        assert row[f"coverage_eff_h{h}_wilson_lo"] < 0.5 < row[f"coverage_eff_h{h}_wilson_hi"]
    assert "coverage_eff_wilson_lo" not in row                  # pooled-trials interval withdrawn


# --- isolation: symlink and late-import bypasses ---------------------------------------------------
def _fake_repo(tmp_path):
    prot = tmp_path / "repo" / "data" / "processed"
    prot.mkdir(parents=True)
    (prot / "secret.csv").write_text("not real data")
    (tmp_path / "link.csv").symlink_to(prot / "secret.csv")
    (tmp_path / "linkdir").symlink_to(prot, target_is_directory=True)
    return tmp_path / "repo"


def test_guard_blocks_symlink_into_protected_tree(tmp_path):
    repo = _fake_repo(tmp_path)
    with real_data_guard(repo):
        with pytest.raises(RealDataAccessError):
            open(tmp_path / "link.csv").read()
        with pytest.raises(RealDataAccessError):
            open(tmp_path / "linkdir" / "secret.csv").read()
        with pytest.raises(RealDataAccessError):
            __import__("os").listdir(tmp_path / "linkdir")
        (tmp_path / "unrelated.txt").write_text("ok")
    assert (tmp_path / "link.csv").read_text() == "not real data"   # guard released


def _subprocess_guard(code: str, tmp_path) -> subprocess.CompletedProcess:
    repo = _fake_repo(tmp_path)
    (tmp_path / "fake.parquet").write_bytes(b"PAR1 not a real parquet file")
    prog = ("import sys\n"
            "from src.research.information_decay.isolation import real_data_guard, RealDataAccessError\n"
            f"REPO, T = {str(repo)!r}, {str(tmp_path)!r}\n" + code)
    return subprocess.run([sys.executable, "-c", prog], cwd=REPO, capture_output=True, text=True)


def test_guard_blocks_data_reader_imported_after_guard(tmp_path):
    code = ("assert 'pyarrow' not in sys.modules\n"
            "with real_data_guard(REPO):\n"
            "    try:\n"
            "        import pyarrow.parquet as pq\n"
            "        pq.read_table(T + '/fake.parquet'); print('BYPASS')\n"
            "    except RealDataAccessError:\n"
            "        print('BLOCKED')\n")
    out = _subprocess_guard(code, tmp_path)
    assert out.stdout.strip() == "BLOCKED", out.stderr


def test_guard_blocks_preimported_pyarrow_readers(tmp_path):
    code = ("import pyarrow, pyarrow.parquet as pq, pyarrow.csv as pc\n"
            "res = []\n"
            "with real_data_guard(REPO):\n"
            "    for f in (lambda: pq.read_table(T + '/fake.parquet'), lambda: pc.read_csv(T + '/link.csv'),\n"
            "              lambda: pyarrow.memory_map(T + '/link.csv')):\n"
            "        try:\n"
            "            f(); res.append('BYPASS')\n"
            "        except RealDataAccessError:\n"
            "            res.append('BLOCKED')\n"
            "print(','.join(res))\n")
    out = _subprocess_guard(code, tmp_path)
    assert out.stdout.strip() == "BLOCKED,BLOCKED,BLOCKED", out.stderr


# --- tie-policy propagation ---------------------------------------------------------------------
def test_tie_policy_flows_through_raw_path():
    cell = _cell("s9_null_discrete", tie_policies=["A", "B"])
    c = _a02_ctx([cell])
    sim = cal.sim_direct(c, cell, 60, 3)
    out = cal.analyse_direct(c, cell, 60, 3, sim)
    assert "gcmi|strat|obs" in out and "gcmi_B|strat|obs" in out and "gcmi_C|strat|obs" not in out
    zB = cal.tie_policy_scores(sim["S"], sim["raw"], "B", None)
    zy = np.column_stack([gcmi.copnorm(sim["Y"][:, j]) for j in range(5)])
    np.testing.assert_allclose(out["gcmi_B|strat|obs"], gcmi.gcmi_from_scores(zB[None], zy.T[None])[0], atol=1e-12)
    assert not np.allclose(out["gcmi|strat|obs"], out["gcmi_B|strat|obs"])


def test_same_tie_policy_in_residual_null_a_and_null_b():
    cell = _cell("s8_null_modeB_disc", tie_policies=["B"])
    c = _a02_ctx([cell])
    sim = cal.sim_residual(c, cell, 60, 2)
    out = cal.analyse_residual(c, cell, 60, 2, sim)
    assert {"gcmi_B|nullA|obs", "gcmi_B|nullB|obs", "gcmi_B|matched_raw|obs"} <= set(out)
    assert not any(k.startswith("gcmi|") for k in out)          # no hidden policy-A statistic
    np.testing.assert_allclose(out["gcmi_B|nullA|obs"], out["gcmi_B|nullB|obs"], atol=1e-12)
    cellA = _cell("s8_null_modeB_disc", tie_policies=["A"])
    outA = cal.analyse_residual(_a02_ctx([cellA]), cellA, 60, 2, sim)
    assert not np.allclose(outA["gcmi|nullB|obs"], out["gcmi_B|nullB|obs"])   # B really applied


def test_null_b_masked_policy_scores_match_subset_and_permuted_raw():
    rng = np.random.default_rng(5)
    S_b = rng.standard_normal((4, 30))
    raw_b = rng.choice([-0.1, 0.0, 0.1], size=(4, 30))
    mask = rng.random((4, 30)) > 0.2
    Z = cal.tie_policy_scores_masked(S_b, raw_b, mask, "B")
    for b in range(4):
        np.testing.assert_allclose(Z[b, mask[b]], cal.tie_policy_scores(S_b[b, mask[b]], raw_b[b, mask[b]], "B", None))
        assert np.all(Z[b, ~mask[b]] == 0)
    ZA = cal.tie_policy_scores_masked(S_b, raw_b, mask, "A")
    for b in range(4):
        np.testing.assert_allclose(ZA[b, mask[b]], gcmi.copnorm(S_b[b, mask[b]]))


def test_bootstrap_policy_b_keeps_raw_groups_tied():
    S = np.array([0.1, 0.12, -0.3, 0.5, 0.11])
    raw = np.array([0.0, 0.0, -0.1, 0.1, 0.0])
    idx = np.array([[0, 0, 1, 2, 3], [4, 4, 4, 3, 2]])
    Z = cal.tie_policy_scores_bootstrap(S[idx], raw[idx], "B", rng_for("t", "boot"))
    assert Z[0, 0] == Z[0, 1] == Z[0, 2] and Z[1, 0] == Z[1, 1] == Z[1, 2]


# --- scenario 7 targets and V4a contamination -----------------------------------------------------
def test_scenario7_contaminated_truth():
    i_clean = -0.5 * math.log2(1 - 0.35 ** 2)
    assert cal.contaminated_truth_bits(i_clean, 0.02) == pytest.approx(0.253820, abs=5e-7)
    hb = -0.02 * math.log2(0.02) - 0.98 * math.log2(0.98)
    assert cal.contaminated_truth_bits(0.0, 0.02) == pytest.approx(hb + 0.02)
    cell = next(c for c in CFG["cells"] if c["id"] == "s7_alt_contaminated")
    ctx_ = cal.make_context(CFG, "test", STRENGTHS)
    assert cal.cell_contaminated_truth_bits(ctx_, cell, 100) == pytest.approx(0.253820, abs=5e-7)
    assert cal.cell_contaminated_truth_bits(ctx_, cell, 60) == pytest.approx(
        cal.contaminated_truth_bits(i_clean, 1 / 60))
    assert cal.cell_truth_bits(ctx_, cell) == pytest.approx(i_clean)       # clean target kept separately


def test_v4a_contamination_preserves_independence_by_construction():
    cell = _cell("s7v4a_null_independent_contamination")
    c = _a02_ctx([cell])
    for n, expect_c in ((60, 1), (77, 2), (120, 2)):
        sim = cal.sim_direct(c, cell, n, 0)
        ms, mr = sim["contam_S_idx"], sim["contam_R_idx"]
        assert len(ms) == len(mr) == expect_c
        assert set(np.round(np.abs(sim["S"][ms]), 12)) == {4.0}
        for i in mr:
            assert np.all(sim["Y"][i] == sim["Y"][i][0]) and abs(sim["Y"][i][0]) == 10.0   # consistent across horizons
    # S-side and R-side contamination signs are independent across replicates
    sgn = []
    for rep in range(400):
        sim = cal.sim_direct(c, cell, 100, rep)
        sgn.append((np.sign(sim["S"][sim["contam_S_idx"][0]]), np.sign(sim["Y"][sim["contam_R_idx"][0], 0])))
    sgn = np.array(sgn)
    assert abs(np.corrcoef(sgn.T)[0, 1]) < 0.15
    # the clean draws (S before contamination, R noise) do not depend on the contamination draws
    s_clean = cal.make_surprise(rng_for("calibration", cell["id"], 100, 7, "data"), 100, "A", CFG, burn_in=False)["S"]
    sim = cal.sim_direct(c, cell, 100, 7)
    keep = np.setdiff1d(np.arange(100), sim["contam_S_idx"])
    np.testing.assert_array_equal(sim["S"][keep], s_clean[keep])


# --- PASS / INCONCLUSIVE / FAIL and the additional-replicate rule ------------------------------------
def _item(k, R, cell="c", n=60):
    return a2.HardItem(gate="V1", cell=cell, estimator="gcmi", procedure="strat", n=n, policy="A", k=k, R=R)


def test_pass_inconclusive_fail_logic():
    c = a2.ceiling(2000)
    assert c == pytest.approx(0.0597468, abs=1e-7)
    items = [_item(400, 10000, "low"), _item(1000, 10000, "high"), _item(590, 10000, "edge")]
    a2.classify_look(items, 1, c, 0.01)
    st = {it.cell: it.status for it in items}
    assert st == {"low": a2.PASS, "high": a2.FAIL, "edge": a2.INCONCLUSIVE}
    # the point estimate alone never decides: 0.050 at R = 2000 overlaps the ceiling
    one = [_item(100, 2000)]
    a2.classify_look(one, 1, c, 0.01)
    assert one[0].status == a2.INCONCLUSIVE
    assert a2.wilson_score_z(100, 2000, c) == pytest.approx((0.05 - c) / math.sqrt(c * (1 - c) / 2000))


def test_holm_reject_step_down():
    assert a2.holm_reject({"a": 0.001, "b": 0.02, "c": 0.04}, 0.05) == {"a", "b", "c"}
    assert a2.holm_reject({"a": 0.001, "b": 0.03, "c": 0.04}, 0.05) == {"a"}
    assert a2.holm_reject({"a": 0.2}, 0.05) == set()


def test_additional_replicate_stopping_rule():
    calls = []
    rates = {"good": 0.040, "bad": 0.080, "edge": 0.0597}

    def run_batch(und, a, b):
        calls.append((sorted(it.cell for it in und), a, b))
        return {it.key: (int(round(rates[it.cell] * (b - a))), b - a) for it in und}
    items = [_item(0, 0, "good"), _item(0, 0, "bad"), _item(0, 0, "edge")]
    a2.run_sequential(items, run_batch, a2.ceiling(2000), 0.01, 2000, 10000)
    st = {it.cell: it for it in items}
    assert st["edge"].status == a2.INCONCLUSIVE and st["edge"].R == 10000      # max reps, still undecided
    assert st["good"].status == a2.PASS and st["bad"].status == a2.FAIL
    assert [c[1:] for c in calls] == [(0, 2000), (2000, 4000), (4000, 6000), (6000, 8000), (8000, 10000)]
    for cells, a, b in calls:                       # decided items never receive more replicates
        if a >= st["good"].R:
            assert "good" not in cells
        if a >= st["bad"].R:
            assert "bad" not in cells
    assert all(h["R"] <= 10000 for it in items for h in it.history)


def test_d3_and_qualification_rules():
    def pol_items(a_status, b_status):
        out = []
        for pol, s in (("A", a_status), ("B", b_status)):
            for n in (60, 77):
                it = a2.HardItem("V2_D3", "s9", cal.GCMI_POLICY_KEYS[pol], "strat", n, pol)
                it.status = s
                out.append(it)
        return out
    assert a2.decide_d3(pol_items(a2.PASS, a2.PASS))["selected"] == "A"
    assert a2.decide_d3(pol_items(a2.INCONCLUSIVE, a2.PASS))["selected"] == "B"
    assert a2.decide_d3(pol_items(a2.PASS, a2.FAIL))["selected"] == "A"
    assert a2.decide_d3(pol_items(a2.FAIL, a2.INCONCLUSIVE))["selected"] is None
    items = pol_items(a2.PASS, a2.FAIL)
    for g in ("V1", "V4a"):
        it = a2.HardItem(g, "x", "gcmi", "strat", 60, "A")
        it.status = a2.PASS
        items.append(it)
    v3 = [{"classification": a2.PASS}]
    assert a2.qualification(items, a2.decide_d3(items), v3, True)["gcmi_qualifies"]
    assert not a2.qualification(items, a2.decide_d3(items), v3, False)["gcmi_qualifies"]
    items[-1].status = a2.INCONCLUSIVE
    q = a2.qualification(items, a2.decide_d3(items), v3, True)
    assert not q["gcmi_qualifies"] and q["V4a"] == a2.INCONCLUSIVE


def test_amendment02_hard_family_is_complete():
    keys = set()
    for spec in A02["hard_family"]:
        for pol in spec["policies"]:
            for n in A02["hard_sizes"]:
                keys.add((spec["gate"], spec["cell"], pol, n))
    assert len(keys) == 42
    assert {k[0] for k in keys} == {"V1", "V2_D3", "V4a"}
    assert A02["hard_sizes"] == [60, 77, 80, 97, 100, 120]
    assert A02["mc_rule"]["alpha_per_look"] == pytest.approx(A02["mc_rule"]["family_alpha"] / A02["mc_rule"]["max_looks"])
    assert A02["mc_rule"]["max_reps"] == A02["mc_rule"]["batch_reps"] * A02["mc_rule"]["max_looks"] == 10000
    it = a2.HardItem("V1", "s1_null_modeA", "gcmi", "strat", 60, "A", k=90, R=2000)
    a2.classify_look([it], 1, a2.ceiling(2000), 0.01)
    row = a2.item_row(it)
    for f in ("gate", "cell", "estimator", "policy", "n", "reps", "rejections", "rate", "ceiling",
              "wilson_score_z", "p_pass_one_sided", "classification"):
        assert row.get(f) is not None, f


def test_v4b_reports_both_targets():
    rows = [{"cell": "s7_alt_contaminated", "n": 100, "estimator": "ksg5", "procedure": "strat", "reps": 10,
             "truth_bits": 0.0943, "mean_obs_bits": 0.11, "mean_eff_bits": 0.11, "adjusted_rate": 0.5},
            {"cell": "s7_alt_contaminated", "n": 100, "estimator": "ksg5", "procedure": "outlier_shift",
             "median_abs_shift_bits": 0.03}]
    out = a2.v4b_report(rows, ["s7_alt_contaminated"], lambda c, n: 0.2538)
    assert len(out) == 1
    r = out[0]
    assert r["error_eff_vs_clean_bits"] == pytest.approx(0.11 - 0.0943)
    assert r["error_eff_vs_contaminated_bits"] == pytest.approx(0.11 - 0.2538) and r["error_eff_vs_contaminated_bits"] < 0
    assert r["classification"].startswith("SENSITIVITY")


def test_vectorized_policy_b_equals_row_loop_including_median_ties():
    rng = np.random.default_rng(9)
    S_b = rng.standard_normal((50, 40))
    raw_b = rng.choice([-0.2, -0.1, 0.0, 0.1, 0.2], size=(50, 40))
    S_b[0, :4], raw_b[0, :4] = [0.5, 0.5, 0.5, 0.5], [-0.1, -0.1, 0.1, 0.1]   # two groups, equal medians
    mask = rng.random((50, 40)) > 0.25
    mask[1, raw_b[1] == 0.2] = False                                           # a group absent in one row
    Z = cal.tie_policy_scores_masked(S_b, raw_b, mask, "B")
    for b in range(50):
        np.testing.assert_array_equal(Z[b, mask[b]], cal.tie_policy_scores(S_b[b, mask[b]], raw_b[b, mask[b]], "B", None))


# =================================================================================================
# Amendment 03: fixed-sample intersection-union qualification
# =================================================================================================
import importlib.util as _ilu  # noqa: E402
import json  # noqa: E402

from src.research.information_decay import amendment03 as a3  # noqa: E402

A03 = yaml.safe_load((REPO / "config" / "information_decay_v1_amendment03.yaml").read_text())
C_CEIL = 0.05 + 2 * math.sqrt(0.05 * 0.95 / 2000)


def _qual_script():
    spec = _ilu.spec_from_file_location("qual03", REPO / "scripts" / "qualify_information_decay_amendment03.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_one_sided_wilson_upper_bound():
    z = 1.6448536269514722
    k, n = 531, 10000
    p = k / n
    expect = (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)
    assert a3.wilson_upper_one_sided(k, n) == pytest.approx(expect, rel=1e-12)
    assert a3.wilson_upper_one_sided(k, n) > p
    assert a3.wilson_upper_one_sided(0, n) > 0
    assert a3.max_passing_count(10000, C_CEIL) == 558
    assert a3.wilson_upper_one_sided(558, 10000) < C_CEIL <= a3.wilson_upper_one_sided(559, 10000)


def test_fixed_n_requirement_and_no_early_qualification():
    with pytest.raises(ValueError):
        a3.classify_cell(100, 2000, C_CEIL)            # an early look cannot be classified
    with pytest.raises(ValueError):
        a3.classify_cell(400, 8000, C_CEIL)
    d = a3.classify_cell(558, 10000, C_CEIL)
    assert d["classification"] == a3.PASS and d["reps"] == 10000
    assert a3.classify_cell(559, 10000, C_CEIL)["classification"] == a3.FAIL
    assert set(d) >= {"p_hat", "wilson_upper_95_one_sided", "ceiling", "false_positives"}
    # the classification is never taken from the point estimate alone
    assert a3.classify_cell(570, 10000, C_CEIL)["p_hat"] < C_CEIL
    assert a3.classify_cell(570, 10000, C_CEIL)["classification"] == a3.FAIL


def test_intersection_union_global_logic_without_cross_cell_adjustment():
    cells = [dict(a3.classify_cell(558, 10000, C_CEIL), required=True) for _ in range(36)]
    g = a3.global_qualification(cells)
    assert g["qualifies"] and g["passed"] == 36          # Holm/Bonferroni over 36 cells would not pass these
    cells[7] = dict(a3.classify_cell(559, 10000, C_CEIL), required=True)
    g = a3.global_qualification(cells)
    assert not g["qualifies"] and len(g["failed_cells"]) == 1
    cells[7]["required"] = False                          # report-only cells never block qualification
    assert a3.global_qualification(cells)["qualifies"]
    assert not a3.global_qualification([])["qualifies"]


def test_operating_characteristics_deterministic_and_match_exact():
    a = a3.simulate_new_rule([0.05] * 36, 10000, C_CEIL, 20000, rng_for("t", "oc"))
    b = a3.simulate_new_rule([0.05] * 36, 10000, C_CEIL, 20000, rng_for("t", "oc"))
    assert a == b
    assert a == pytest.approx(a3.exact_qualification_probability([0.05] * 36, 10000, C_CEIL), abs=0.01)
    assert a3.exact_qualification_probability([C_CEIL], 10000, C_CEIL) <= 0.05     # level at the boundary
    assert a3.exact_qualification_probability([C_CEIL + 0.001], 10000, C_CEIL) < \
        a3.exact_qualification_probability([C_CEIL], 10000, C_CEIL)               # monotone in p
    oc = json.loads((REPO / "docs" / "research" / "information_decay_v1_amendment_03_oc.json").read_text())
    assert oc["sims_new_rule"] >= 1_000_000 and oc["real_market_mi_computed"] is False
    row = next(r for r in oc["rows"] if r["configuration"].startswith("A: all"))
    assert row["new_rule_P_qualify_required36_exact"] == pytest.approx(
        a3.exact_qualification_probability([0.05] * 36, 10000, C_CEIL), rel=1e-9)


def test_topup_to_exactly_n_final_without_double_counting():
    q = _qual_script()
    stored = [{"gate": "V2_D3", "cell": "s9", "estimator": "gcmi", "procedure": "strat", "n": 60, "policy": "A",
               "reps": 4000, "rejections": 200},
              {"gate": "V2_D3", "cell": "s9", "estimator": "gcmi_B", "procedure": "strat", "n": 60, "policy": "B",
               "reps": 8000, "rejections": 410},
              {"gate": "V1", "cell": "s1", "estimator": "gcmi", "procedure": "strat", "n": 60, "policy": "A",
               "reps": 10000, "rejections": 500}]
    plan = q.topup_plan(stored, 10000)
    assert [(e["cell"], e["start"], e["end"]) for e in plan] == [("s9", 4000, 10000), ("s9", 8000, 10000)]
    rows = []
    for e in plan:          # one task computes both policies; rows are identified by their start
        for est in ("gcmi", "gcmi_B"):
            rows.append({"cell": "s9", "estimator": est, "procedure": "strat", "n": 60, "rep_first": e["start"],
                         "rep_last": 9999, "reps": 10000 - e["start"], "adjusted_rejections": 1})
    merged = {(m["cell"], m["estimator"]): m for m in q.merge_counts(stored, rows, 10000)}
    assert all(m["reps"] == 10000 for m in merged.values())
    assert merged[("s9", "gcmi")]["rejections"] == 201 and merged[("s9", "gcmi_B")]["rejections"] == 411
    assert merged[("s1", "gcmi")]["rejections"] == 500        # already-complete cell is never topped up
    with pytest.raises(RuntimeError):            # missing top-up refused
        q.merge_counts(stored, rows[:1], 10000)
    bad = [dict(r, rep_last=8999) for r in rows]
    with pytest.raises(RuntimeError):            # a top-up that does not end at N_final is refused
        q.merge_counts(stored, bad, 10000)


def test_amendment03_required_family_is_complete():
    req = {(x["gate"], x["policy"]) for x in A03["required"]}
    cells = []
    for spec in A02["hard_family"]:
        for pol in spec["policies"]:
            for n in A02["hard_sizes"]:
                cells.append((spec["gate"], spec["cell"], pol, n, (spec["gate"], pol) in req))
    assert len(cells) == 42
    assert sum(c[4] for c in cells) == 36
    assert {(c[0], c[2]) for c in cells if not c[4]} == {("V2_D3", "A")}
    assert A03["n_final"] == 10000 and A03["selected_policy"] == "B"


# =================================================================================================
# Amendment 04: dir_fd isolation, scientific fingerprint, exact primary-cohort-size inventory
# =================================================================================================
import os  # noqa: E402

from src.research.information_decay import amendment04 as a4  # noqa: E402
from src.research.information_decay import provenance  # noqa: E402

A04_INV = json.loads((REPO / "docs" / "research" / "information_decay_v1_amendment_04_inventory.json").read_text())


@pytest.fixture()
def protected_tree(tmp_path):
    prot = tmp_path / "repo" / "data" / "processed"
    (prot / "sub").mkdir(parents=True)
    (prot / "synthetic.txt").write_text("synthetic, not real data")
    (prot / "sub" / "nested.txt").write_text("synthetic")
    dfd = os.open(str(prot), os.O_RDONLY)            # opened BEFORE the guard is activated
    ffd = os.open(str(prot / "synthetic.txt"), os.O_RDONLY)
    yield tmp_path, tmp_path / "repo", dfd, ffd
    os.close(dfd)
    os.close(ffd)


def test_dir_fd_relative_opens_denied(protected_tree):
    tmp, repo, dfd, ffd = protected_tree
    captured_open = os.open                          # a reference taken before the guard
    with real_data_guard(repo):
        for rel in ("synthetic.txt", "sub/nested.txt", "../processed/synthetic.txt"):
            with pytest.raises(RealDataAccessError):
                os.open(rel, os.O_RDONLY, dir_fd=dfd)
            with pytest.raises(RealDataAccessError):
                captured_open(rel, os.O_RDONLY, dir_fd=dfd)
        with pytest.raises(RealDataAccessError):
            os.stat("synthetic.txt", dir_fd=dfd)
        with pytest.raises(RealDataAccessError):
            os.listdir(dfd)
        with pytest.raises(RealDataAccessError):
            open(ffd).read()                        # re-opening a pre-opened regular-file descriptor
        with pytest.raises(RealDataAccessError):
            open("relative_name.txt", "w")          # any relative path fails closed
    assert os.open is captured_open                  # os functions restored after the guard


def test_allowed_calibration_files_and_pipes_work_under_guard(protected_tree):
    tmp, repo, _, _ = protected_tree
    with real_data_guard(repo):
        target = tmp / "calibration_output.csv"
        with open(target, "w") as fh:               # absolute path outside protected trees
            fh.write("ok")
        assert target.read_text() == "ok"
        r, w = os.pipe()
        with open(w, "wb", closefd=False) as fh:   # pipes stay usable (multiprocessing)
            fh.write(b"x")
        os.close(w)
        assert os.read(r, 1) == b"x"
        os.close(r)


def test_process_pool_runs_inside_guard(tmp_path):
    cells = [_cell("s1_null_modeA")]
    c = _a02_ctx(cells)
    with real_data_guard(REPO):
        rows = cal.run_calibration(c, str(REPO), workers=2, tasks=[("s1_null_modeA", 40, 0, 3)])["rows"]
    assert any(r["estimator"] == "gcmi" and r["reps"] == 3 for r in rows)


def _fake_scientific_repo(root: Path, order):
    files = {"src/research/information_decay/a.py": "A = 1\n",
             "src/research/information_decay/sub/b.py": "B = 2\n",
             "scripts/qualify_information_decay_amendment04.py": "print('q')\n",
             "scripts/unrelated.py": "x = 1\n",
             "config/information_decay_v1.yaml": "k: 1\n",
             "README.md": "prose\n",
             "src/research/information_decay/__pycache__/a.cpython-39.pyc": "bytecode"}
    for rel in order(list(files)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(files[rel])


def test_fingerprint_scope_ordering_and_sensitivity(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _fake_scientific_repo(r1, lambda xs: xs)
    _fake_scientific_repo(r2, lambda xs: list(reversed(xs)))
    i1, i2 = provenance.scientific_inventory(r1), provenance.scientific_inventory(r2)
    assert i1 == i2                                                     # file creation order is irrelevant
    paths = [f["path"] for f in i1["files"]]
    assert paths == sorted(paths)
    assert "scripts/qualify_information_decay_amendment04.py" in paths
    assert "README.md" not in paths and "scripts/unrelated.py" not in paths
    assert not any("__pycache__" in p for p in paths)
    (r1 / "README.md").write_text("changed prose\n")
    assert provenance.scientific_inventory(r1)["aggregate_sha256"] == i1["aggregate_sha256"]
    (r1 / "scripts/qualify_information_decay_amendment04.py").write_text("print('changed qualifier')\n")
    assert provenance.scientific_inventory(r1)["aggregate_sha256"] != i1["aggregate_sha256"]


def test_real_fingerprint_includes_every_qualification_runner():
    paths = [f["path"] for f in provenance.scientific_inventory(REPO)["files"]]
    for must in ("scripts/qualify_information_decay_amendment03.py", "scripts/qualify_information_decay_amendment04.py",
                 "scripts/revalidate_information_decay_amendment02.py", "scripts/calibrate_information_decay_estimators.py",
                 "src/research/information_decay/amendment03.py", "src/research/information_decay/amendment04.py",
                 "config/information_decay_v1_amendment03.yaml"):
        assert must in paths, must


def test_exact_size_inventory_derived_from_frozen_definitions():
    derived = a4.derive_exact_inventory(CFG, A02, A03)
    assert a4.inventories_equal(derived, A04_INV["cells"])
    assert A04_INV["contains_simulation_results"] is False
    for n in (93, 103):
        cells = [c for c in derived if c["n"] == n]
        assert sum(c["gate"] == "V1" for c in cells) == 4
        assert [c["tie_policy"] for c in cells if c["gate"] == "V2_D3"] == ["B"]
        assert [c["estimator"] for c in cells if c["gate"] == "V2_D3"] == ["gcmi_B"]
        assert sum(c["gate"] == "V4a" for c in cells) == 1
        assert all(c["replicates"] == 10000 for c in cells)
    assert sorted({c["n"] for c in derived}) == [93, 103]
    altered = [dict(c, n=100) if i == 0 else c for i, c in enumerate(derived)]
    assert not a4.inventories_equal(altered, A04_INV["cells"])          # no substitution of n = 100


def test_exact_size_rule_and_wilson_boundary():
    assert A04_INV["rule"]["ceiling"] == a2.ceiling(2000) == 0.059746794344808965
    assert A04_INV["rule"]["n_final"] == 10000
    assert a3.wilson_upper_one_sided(558, 10000) == pytest.approx(0.059697, abs=5e-7)
    assert a3.wilson_upper_one_sided(559, 10000) == pytest.approx(0.059800, abs=5e-7)
    assert a3.classify_cell(558, 10000, A04_INV["rule"]["ceiling"])["classification"] == a3.PASS
    assert a3.classify_cell(559, 10000, A04_INV["rule"]["ceiling"])["classification"] == a3.FAIL
    with pytest.raises(ValueError):
        a3.classify_cell(450, 9999, A04_INV["rule"]["ceiling"])
    cells = [dict(a3.classify_cell(500, 10000, A04_INV["rule"]["ceiling"]), required=True) for _ in range(11)]
    cells.append(dict(a3.classify_cell(559, 10000, A04_INV["rule"]["ceiling"]), required=True))
    assert not a3.global_qualification(cells)["qualifies"]              # one exact-size FAIL blocks


REAL_DATA_RUNNERS = {"run_information_decay_primary_v1.py"}   # the only scripts allowed to read real data


def test_all_information_decay_runners_reference_no_real_data_paths():
    for p in sorted((REPO / "scripts").glob("*information_decay*.py")):
        if p.name in REAL_DATA_RUNNERS:
            continue
        text = p.read_text()
        for forbidden in ("event_response_v", "read_parquet(", "data/processed/event_response", "macro_market_response"):
            assert forbidden not in text, (p, forbidden)


# =================================================================================================
# Primary real-data runner: logic tested on SYNTHETIC frames only (no real data is read here)
# =================================================================================================
def _primary_runner():
    spec = _ilu.spec_from_file_location("primary_v1", REPO / "scripts" / "run_information_decay_primary_v1.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synthetic_event_frame(n=40, seed=0):
    rng = np.random.default_rng(seed)
    H = [1, 5, 15, 30, 60]
    rows = []
    for fam in ("CPI_MOM", "NFP"):
        for i in range(n):
            r = {"event_family": fam, "symbol": "SPY", "event_status": "usable", "surprise_std_status": "ok",
                 "surprise_std": float(rng.standard_normal()), "surprise_raw": float(rng.choice([-0.1, 0.0, 0.1])),
                 "provisional_market_data": False, "release_id": f"{fam}-{i:03d}",
                 "release_timestamp_utc": pd.Timestamp("2017-01-01", tz="UTC") + pd.Timedelta(days=30 * i),
                 "split": ["development", "validation", "test"][min(2, i * 3 // n)]}
            for h in H:
                r[f"post{h}m_status"] = "ok"
                r[f"post{h}m_ret"] = float(rng.standard_normal())
            rows.append(r)
    df = pd.DataFrame(rows)
    df.loc[3, "post60m_status"] = "insufficient_market_window"      # excluded at ALL horizons
    df.loc[3, "post60m_ret"] = np.nan
    df.loc[5, "surprise_std_status"] = "insufficient_history"
    return df


def test_primary_cohort_is_common_support_and_release_unique():
    m = _primary_runner()
    df = _synthetic_event_frame()
    c = m.build_cohort(df, "CPI_MOM", "SPY", [1, 5, 15, 30, 60])
    assert len(c) == 38 and "CPI_MOM-003" not in set(c["release_id"]) and "CPI_MOM-005" not in set(c["release_id"])
    assert c["release_timestamp_utc"].is_monotonic_increasing
    chk = m.verify_cohort(c, "CPI_MOM", 38, [1, 5, 15, 30, 60])
    assert chk["problems"] == [] and len(chk["cohort_id"]) == 16
    assert m.verify_cohort(c, "CPI_MOM", 39, [1, 5, 15, 30, 60])["problems"]          # count mismatch -> STOP
    dup = pd.concat([c, c.iloc[:1]], ignore_index=True)
    assert any("unique" in p for p in m.verify_cohort(dup, "CPI_MOM", 39, [1, 5, 15, 30, 60])["problems"])
    drift = c.copy()
    drift.loc[0, "post15m_ret"] = np.nan
    assert any("drift" in p for p in m.verify_cohort(drift, "CPI_MOM", 38, [1, 5, 15, 30, 60])["problems"])
    assert m.cohort_id(list(c["release_id"])) == m.cohort_id(list(reversed(c["release_id"])))


def test_primary_panel_deterministic_and_shared_permutation():
    m = _primary_runner()
    df = _synthetic_event_frame()
    c = m.build_cohort(df, "NFP", "SPY", [1, 5, 15, 30, 60])
    labels = m.strata_labels(c, ["development", "validation", "test"], 8)
    S, raw = c["surprise_std"].to_numpy(), c["surprise_raw"].to_numpy()
    Y = np.column_stack([c[f"post{h}m_ret"].to_numpy() for h in [1, 5, 15, 30, 60]])
    cid = m.cohort_id(c["release_id"])
    a = m.analyse_panel(S, raw, Y, labels, "NFP", cid, "B", "chronological_split_v1_min8", "gcmi_B", 199, 50)
    b = m.analyse_panel(S, raw, Y, labels, "NFP", cid, "B", "chronological_split_v1_min8", "gcmi_B", 199, 50)
    for k in ("obs", "null_mean", "p_unadj", "p_adj", "boot_raw_lo", "boot_raw_hi"):
        np.testing.assert_array_equal(a[k], b[k])
    zs = cal.tie_policy_scores(S, raw, "B", None)
    zy = np.column_stack([gcmi.copnorm(Y[:, j]) for j in range(5)])
    np.testing.assert_allclose(a["obs"], gcmi.gcmi_from_scores(zs[None], zy.T[None])[0])
    assert "horizon" not in a["seeds"]["permutation"]["key"]                    # one draw for all horizons
    assert a["seeds"]["permutation"]["key"].startswith("m3_information_decay|v1|permutation|family=NFP|cohort=")
    assert np.all(a["p_adj"] >= a["p_unadj"]) and a["p_unadj"].min() >= 1 / 200


def test_holm_closed_testing_rule():
    m = _primary_runner()
    H = [1, 5, 15, 30, 60]
    out = m.holm_closed_testing({"A": 0.001, "B": 0.02, "C": 0.30},
                                {"A": {"horizons": H, "p": [0.001, 0.01, 0.02, 0.2, 0.5]},
                                 "B": {"horizons": H, "p": [0.02, 0.03, 0.2, 0.3, 0.4]},
                                 "C": {"horizons": H, "p": [0.3] * 5}}, 0.05)
    assert out["A"]["panel_rejected"] and out["A"]["holm_threshold"] == pytest.approx(0.05 / 3)
    assert out["A"]["detectable_horizons"] == [1, 5]
    assert out["B"]["panel_rejected"] and out["B"]["detectable_horizons"] == [1]
    assert not out["C"]["panel_rejected"] and out["C"]["detectable_horizons"] == []
    assert out["B"]["holm_adjusted_p"] == pytest.approx(0.04)
    out2 = m.holm_closed_testing({"A": 0.03, "B": 0.001}, {"A": {"horizons": H, "p": [0.03] * 5},
                                                           "B": {"horizons": H, "p": [0.001] * 5}}, 0.05)
    assert out2["A"]["panel_rejected"]          # step-down: second panel tested at 0.05/1
