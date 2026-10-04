"""Permanent regressions for the independent methodology review of the event-response layer."""
import copy
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.market_exceptions import MarketExceptionRegistry
from src.research import event_response as er
from src.research.baseline import add_baseline
from src.research.dataset import (
    WIDE_KEY, WINDOW_KEY, build_event_response, canonical_content_sha256, research_input_identity,
)
from src.research.diagnostics import diagnostics_tables, tie_safe_bins
from src.research.events import (
    PIT_OFFICIAL_VERIFIED, PIT_PARTIAL, PIT_UNVERIFIED, build_event_table, load_research_spec,
    official_validation_summary,
)
from src.research.filters import release_level, require_single_family, usable_response
from src.research.surprise import add_surprises
from tests.test_event_research import et, ff_row, make_bars
from tests.test_market_exceptions import entry, registry

EMPTY = MarketExceptionRegistry.empty()


@pytest.fixture
def spec():
    s = copy.deepcopy(load_research_spec())
    s["standardization"]["min_history"] = 3
    s["baseline"]["min_history"] = 2
    return s


# --------------------------------------------------------------------------- point-in-time provenance
def _official(ff_id, value, unit="PERCENT", match_status="EXACT_MATCH", actual_value_source="MQL5"):
    return {"ff_event_id": ff_id, "official_release_vintage_value": value, "official_unit": unit,
            "official_vintage_date": "2024-03-12", "official_source": "ALFRED",
            "release_actual_match_status": match_status, "actual_value_source": actual_value_source}


def test_retrospective_values_are_never_labelled_verified_pre_release(spec):
    ev = build_event_table(pd.DataFrame([ff_row(1, "CPI_MOM", et("2024-03-12", "08:30"), 0.4, 0.3)]), spec)
    r = ev.iloc[0]
    assert r["value_provenance"] == "retrospective_historical_page"
    assert r["forecast_point_in_time_status"] == r["actual_point_in_time_status"] == r["event_point_in_time_status"] == PIT_UNVERIFIED
    assert not any("pre_release" in c or "consensus" in c for c in ev.columns)


def test_official_match_verifies_only_the_actual(spec):
    ev = build_event_table(pd.DataFrame([ff_row(1, "CPI_MOM", et("2024-03-12", "08:30"), 0.4, 0.3)]), spec,
                           official_rows=[_official("ff:1", 0.4, actual_value_source="FF")])
    r = ev.iloc[0]
    assert r["actual_point_in_time_status"] == PIT_OFFICIAL_VERIFIED and r["event_point_in_time_status"] == PIT_PARTIAL
    assert r["forecast_point_in_time_status"] == PIT_UNVERIFIED  # no source can verify a historical forecast


def test_mql5_match_cannot_upgrade_ff_or_official_validation(spec):
    """Reconciliation said EXACT_MATCH, but for an MQL5 actual; the Forex Factory actual differs."""
    ev = build_event_table(pd.DataFrame([ff_row(1, "CPI_MOM", et("2024-03-12", "08:30"), 0.6, 0.3)]), spec,
                           official_rows=[_official("ff:1", 0.4, match_status="EXACT_MATCH", actual_value_source="MQL5")])
    r = ev.iloc[0]
    assert r["ff_actual_vs_official_release_vintage"] == "VALUE_MISMATCH"
    assert r["actual_point_in_time_status"] == "official_release_vintage_mismatch"
    # a reconciliation status without an official number never verifies anything
    ev2 = build_event_table(pd.DataFrame([ff_row(1, "CPI_MOM", et("2024-03-12", "08:30"), 0.4, 0.3)]), spec,
                            official_rows=[_official("ff:1", None, match_status="EXACT_MATCH")])
    assert ev2.iloc[0]["ff_actual_vs_official_release_vintage"] == "no_official_release_vintage"
    assert ev2.iloc[0]["actual_point_in_time_status"] == PIT_UNVERIFIED
    assert "release_actual_match_status" not in ev2.columns


def test_official_validation_summary_counts(spec):
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i}-12", "08:30"), 0.4, 0.3) for i in (1, 2, 3)]
    ev = build_event_table(pd.DataFrame(rows), spec, official_rows=[_official("ff:1", 0.4), _official("ff:2", 0.44)])
    s = official_validation_summary(ev)
    assert (s["total_events"], s["officially_cross_validated_events"], s["exact_matches"], s["rounding_matches"],
            s["mismatches"], s["not_validated"], s["forecasts_independently_verified"]) == (3, 2, 1, 1, 0, 1, 0)
    assert "not independently verified" in s["statement"]


# --------------------------------------------------------------------------- exclusion propagation
DATES = ["2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12", "2024-07-11"]


def _dataset(spec, rows, reg=EMPTY):
    bars = {s: er.SymbolBars(make_bars(sorted({d for d in DATES}))) for s in ("QQQ", "SPY")}
    return build_event_response(pd.DataFrame(rows), spec, reg, bars, {"market_dataset_fingerprint": "sha256:t"})


def test_excluded_release_yields_only_event_excluded_windows(spec):
    rows = [ff_row(1, "CPI_MOM", et(DATES[0], "08:30"), 0.4, 0.3, bundle="CPI"),
            ff_row(2, "CPI_YOY", et(DATES[0], "07:30"), 3.2, 3.1, bundle="CPI")]   # conflicts -> excluded alone at 07:30
    wide, windows, events = _dataset(spec, rows)
    bad = windows[windows["release_id"] == et(DATES[0], "07:30").isoformat()]
    assert len(bad) == 2 * len(er.window_specs(spec))
    assert set(bad["status"]) == {"event_excluded"} and bad["ret"].isna().all() and bad["volume"].isna().all()
    assert bad["window_reason"].str.contains("excluded_timestamp_conflict").all()
    ex = wide[wide.event_id == "ff:2"]
    assert (ex[[c for c in wide.columns if c.endswith("_status") and c.startswith(("pre", "post"))]] == "event_excluded").all().all()
    assert ex[[c for c in wide.columns if c.endswith("_ret") and c.startswith(("pre", "post"))]].isna().all().all()
    assert not usable_response(ex, "post5m").any()


def test_excluded_member_of_a_mixed_release_carries_no_response(spec):
    rows = [ff_row(1, "CPI_MOM", et(DATES[0], "08:30"), 0.4, 0.3, bundle="CPI"),
            ff_row(2, "FED_FUNDS_RATE", et(DATES[0], "08:30"), 5.5, None)]          # unscheduled FOMC at 08:30
    wide, windows, _ = _dataset(spec, rows)
    w = windows[windows.release_id == et(DATES[0], "08:30").isoformat()]
    assert (w["status"] == "ok").all() and (w["release_excluded_member_events"] == 1).all()
    assert wide.loc[wide.event_id == "ff:2", "post5m_ret"].isna().all()
    assert wide.loc[wide.event_id == "ff:1", "post5m_ret"].notna().all()


# --------------------------------------------------------------------------- timestamp resolution
@pytest.mark.parametrize("unit", ["ns", "us", "ms", "s"])
def test_history_is_semantic_for_any_datetime_resolution(spec, unit):
    vals = [0.1, -0.2, 0.3, 0.0, 0.5]
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i + 1}-10", "08:30"), v, 0.0) for i, v in enumerate(vals)]
    ev = build_event_table(pd.DataFrame(rows), spec)
    ev["release_timestamp_utc"] = ev["release_timestamp_utc"].astype(f"datetime64[{unit}, UTC]")
    s = add_surprises(ev, spec)
    assert list(s["surprise_std_n_history"].astype(int)) == [0, 1, 2, 3, 4]


def test_python_datetime_storage_is_safe(spec):
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i + 1}-10", "08:30"), 0.1 * i, 0.0) for i in range(5)]
    ev = build_event_table(pd.DataFrame(rows), spec)
    ev["release_timestamp_utc"] = [t.to_pydatetime() for t in ev["release_timestamp_utc"]]
    ev["release_timestamp_utc"] = ev["release_timestamp_utc"].astype(object)
    assert list(add_surprises(ev, spec)["surprise_std_n_history"].astype(int)) == [0, 1, 2, 3, 4]


def test_same_timestamp_never_enters_history_and_future_never_does(spec):
    t0 = et("2024-01-10", "08:30")
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i + 1}-10", "08:30"), 0.1 * i, 0.0) for i in range(4)]
    rows.append(ff_row(90, "CPI_MOM", t0, 9.0, 0.0))                                         # same instant as row 0
    rows.append(ff_row(91, "CPI_MOM", t0 + pd.Timedelta(microseconds=1), 7.0, 0.0))          # 1 microsecond later
    ev = build_event_table(pd.DataFrame(rows), spec)
    ev["release_timestamp_utc"] = ev["release_timestamp_utc"].astype("datetime64[us, UTC]")
    s = add_surprises(ev, spec).set_index("event_id")
    assert s.at["ff:0", "surprise_std_n_history"] == 0 and s.at["ff:90", "surprise_std_n_history"] == 0
    assert s.at["ff:91", "surprise_std_n_history"] == 2  # strictly earlier only: ff:0 and ff:90


def test_baseline_history_is_semantic_for_microsecond_storage(spec):
    rows = [ff_row(i, "CPI_MOM", et(d, "08:30"), 0.1 * (i % 2), 0.0) for i, d in enumerate(DATES)]
    wide, _, _ = _dataset(spec, rows)
    w_us = wide.copy()
    w_us["release_timestamp_utc"] = w_us["release_timestamp_utc"].astype("datetime64[us, UTC]")
    base = add_baseline(w_us.drop(columns=[c for c in w_us.columns if c.startswith(("expected_", "resid_", "baseline_"))]), spec)
    spy = base[base.symbol == "SPY"].sort_values("release_timestamp_utc")
    for _, r in spy.iterrows():
        if pd.notna(r["baseline_post5m_training_cutoff_utc"]):
            assert pd.Timestamp(r["baseline_post5m_training_cutoff_utc"]) < r["release_timestamp_utc"]
    assert (base["baseline_evaluation_paradigm"] == "walk_forward_online_expanding").all()
    assert spy["baseline_post5m_n_history"].astype(float).tolist() == [0, 0, 1, 1, 2]  # per surprise-sign group


# --------------------------------------------------------------------------- surprise status
def test_missing_forecast_is_never_ok(spec):
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i + 1}-10", "08:30"), 0.1 * i, 0.0) for i in range(5)]
    rows.append(ff_row(99, "CPI_MOM", et("2024-07-10", "08:30"), 0.3, None))
    s = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec).set_index("event_id")
    assert s.at["ff:99", "surprise_std_status"] == "missing_forecast"
    assert pd.isna(s.at["ff:99", "surprise_std"]) and pd.isna(s.at["ff:99", "surprise_raw"])


def test_near_constant_history_is_zero_dispersion_not_a_giant_z(spec):
    """Three identical 0.1 surprises: numpy's std is 1.7e-17 (float noise in the mean), which an
    exact `std == 0` test would turn into a z-score of ~6e15."""
    assert np.array([0.1, 0.1, 0.1]).std(ddof=1) > 0  # the hazard is real
    rows = [ff_row(i, "CPI_MOM", et(f"2024-0{i + 1}-10", "08:30"), v, 0.0) for i, v in enumerate([0.1, 0.1, 0.1, 0.2])]
    s = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec)
    assert s.loc[3, "surprise_std_status"] == "zero_historical_dispersion" and pd.isna(s.loc[3, "surprise_std"])


# --------------------------------------------------------------------------- canonical hashing and input identity
def test_hash_survives_parquet_roundtrip(spec, tmp_path):
    rows = [ff_row(i, "CPI_MOM", et(d, "08:30"), 0.1 * (i % 3), 0.1, bundle="CPI") for i, d in enumerate(DATES)]
    rows.append(ff_row(50, "NFP", pd.NaT, 100.0, None, quality="TENTATIVE"))
    wide, windows, _ = _dataset(spec, rows)
    for df, key, name in ((wide, WIDE_KEY, "w"), (windows, WINDOW_KEY, "x")):
        p = tmp_path / f"{name}.parquet"
        df.to_parquet(p, index=False)
        assert canonical_content_sha256(df, key) == canonical_content_sha256(pd.read_parquet(p), key)
        shuffled = df.sample(frac=1.0, random_state=1)[list(reversed(df.columns))]
        assert canonical_content_sha256(shuffled, key) == canonical_content_sha256(df, key)


def test_research_input_identity_covers_grouping_and_official_inputs(spec):
    ff = pd.DataFrame([ff_row(1, "CPI_MOM", et(DATES[0], "08:30"), 0.4, 0.3, bundle="CPI")])
    base = research_input_identity(ff, [], {"m": 1}, spec)
    ff2 = ff.assign(release_bundle="OTHER")
    assert research_input_identity(ff2, [], {"m": 1}, spec)["macro_events_sha256"] != base["macro_events_sha256"]
    off = research_input_identity(ff, [_official("ff:1", 0.4)], {"m": 1}, spec)
    assert off["official_reconciliation_sha256"] != base["official_reconciliation_sha256"]
    assert research_input_identity(ff, [], {"m": 2}, spec)["combined_sha256"] != base["combined_sha256"]
    ff3 = ff.assign(normalized_at_utc=pd.Timestamp("2030-01-01", tz="UTC"))  # wall clock: irrelevant
    assert research_input_identity(ff3, [], {"m": 1}, spec)["combined_sha256"] == base["combined_sha256"]


# --------------------------------------------------------------------------- tie-safe bins
def test_ties_never_split_across_bins():
    v = pd.Series([0.0] * 15 + [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    b = tie_safe_bins(v)
    assert b["labels"][:15].nunique() == 1 and b["actual_bins"] == 5 and b["unique_values"] == 7
    for x in v.unique():
        assert b["labels"][v == x].nunique() == 1


def test_insufficient_unique_values_reduce_or_skip_bins():
    b = tie_safe_bins(pd.Series([0.0] * 10 + [0.25, -0.25]))
    assert (b["requested_bins"], b["actual_bins"], b["unique_values"]) == (5, 3, 3)
    assert tie_safe_bins(pd.Series([0.0] * 8))["labels"] is None


# --------------------------------------------------------------------------- volume baseline
def _days(n_before, target="2024-03-12"):
    d = [x.date().isoformat() for x in pd.bdate_range(end=target, periods=n_before + 1)]
    return [x for x in d if x not in ("2024-02-19",)]  # Presidents' Day


def _vol_rel(bars, t, spec, reg=EMPTY):
    sched = er.calendar_frame(dt.date(2023, 9, 1), dt.date(2024, 12, 31))
    rows = er.compute_release_windows(t, "SPY", er.SymbolBars(bars), reg, spec, sched, dt.date(2026, 9, 30))
    return {r["window"]: r for r in rows}["post5m"]


def test_search_backward_skips_invalid_candidates_to_find_20(spec):
    t = et("2024-03-12", "10:00")
    days = _days(30)
    prior = [d for d in days if d < "2024-03-12"]
    drop = {et(prior[-1], "10:02")}                 # most recent prior day: incomplete -> skipped
    bars = make_bars(days, drop=drop)
    gap_day = prior[-2]
    bars.loc[(bars.timestamp_utc >= et(gap_day, "10:00")) & (bars.timestamp_utc < et(gap_day, "10:05")), "volume"] *= 1000
    reg = registry(entry("gap", "provider_gap", gap_day, "10:01", "10:03", symbols=("SPY",)))  # 2nd most recent: gap -> skipped
    r = _vol_rel(bars, t, spec, reg)
    assert r["volume_rel_status"] == "ok" and r["volume_rel_n_windows"] == 20
    assert r["volume_rel"] == pytest.approx(1.0)  # the inflated gap day never entered the baseline


def test_halt_day_is_not_a_comparable_window(spec):
    t = et("2024-03-12", "10:00")
    days = _days(25)
    halt_day = [d for d in days if d < "2024-03-12"][-1]
    bars = make_bars(days)
    bars.loc[(bars.timestamp_utc >= et(halt_day, "10:00")) & (bars.timestamp_utc < et(halt_day, "10:05")), "volume"] *= 1000
    reg = registry(entry("h", "market_wide_halt", halt_day, "10:03", "10:04", symbols=("SPY",), provider="*", feed="*",
                         invalidates=False))
    assert _vol_rel(bars, t, spec, reg)["volume_rel"] == pytest.approx(1.0)


def test_early_close_after_hours_never_stands_in_for_regular_minutes(spec):
    t = et("2023-12-13", "14:00")                       # FOMC-style regular-session window
    days = [x.date().isoformat() for x in pd.bdate_range("2023-11-01", "2023-12-13") if x.date().isoformat() != "2023-11-23"]
    bars = make_bars(days)
    early = "2023-11-24"                                # 13:00 close: 14:00 is after-hours that day
    bars.loc[(bars.timestamp_utc >= et(early, "14:00")) & (bars.timestamp_utc < et(early, "14:05")), "volume"] *= 1000
    r = _vol_rel(bars, t, spec)
    assert r["volume_rel"] == pytest.approx(1.0) and r["volume_rel_n_windows"] == 20


def test_sparse_history_gives_insufficient_comparable_history(spec):
    t = et("2024-03-12", "10:00")
    days = _days(25)
    prior = [d for d in days if d < "2024-03-12"]
    drop = {et(d, "10:01") for d in prior[:-5]}         # only the 5 most recent prior days are complete
    r = _vol_rel(make_bars(days, drop=drop), t, spec)
    assert r["volume_rel"] is None and r["volume_rel_status"] == "insufficient_comparable_history"
    assert r["volume_rel_n_windows"] == 5


# --------------------------------------------------------------------------- diagnostics honour exclusions / families
def test_excluded_events_never_enter_diagnostics(spec):
    rows = [ff_row(i, "CPI_MOM", et(d, "08:30"), 0.1 * i, 0.0) for i, d in enumerate(DATES)]
    wide, _, _ = _dataset(spec, rows)
    tampered = wide.copy()
    # an excluded event carrying (illegitimate) values must still be ignored by every statistic
    i = tampered.index[(tampered.symbol == "SPY")][-1]
    tampered.loc[i, ["event_status", "post5m_ret", "post5m_status", "surprise_std", "surprise_std_status"]] = [
        "excluded_timestamp_conflict", 99.0, "ok", 50.0, "ok"]
    d = diagnostics_tables(tampered, spec)
    spy = d["distributions"][(d["distributions"].symbol == "SPY") & (d["distributions"].quantity == "post5m_ret")]
    assert spy["max"].iloc[0] < 1.0


def test_per_family_statistics_refuse_pooled_families():
    df = pd.DataFrame({"event_family": ["CPI_MOM", "NFP"]})
    with pytest.raises(ValueError, match="several families"):
        require_single_family(df)


def test_release_level_is_the_statistical_unit(spec):
    rows = [ff_row(1, "CPI_MOM", et(DATES[0], "08:30"), 0.4, 0.3, bundle="CPI"),
            ff_row(2, "CORE_CPI_MOM", et(DATES[0], "08:30"), 0.3, 0.3, bundle="CPI")]
    wide, _, _ = _dataset(spec, rows)
    rel = release_level(wide)
    assert len(rel) == 2 and set(rel["n_member_events"]) == {2}  # one row per (release, symbol)
    assert set(rel["member_families"]) == {"CORE_CPI_MOM,CPI_MOM"}


# --------------------------------------------------------------------------- FOMC weak proxy and family isolation
def test_fomc_surprise_is_labelled_a_weak_proxy(spec):
    assert spec["families"]["FED_FUNDS_RATE"]["surprise_measure_quality"] == "weak_surprise_proxy"
    ev = build_event_table(pd.DataFrame([ff_row(1, "FED_FUNDS_RATE", et("2024-03-20", "14:00"), 5.5, 5.5),
                                         ff_row(2, "CPI_MOM", et("2024-03-12", "08:30"), 0.4, 0.3)]), spec).set_index("event_id")
    assert ev.at["ff:1", "surprise_measure_quality"] == "weak_surprise_proxy"
    assert ev.at["ff:2", "surprise_measure_quality"] == "calendar_forecast_surprise"


def test_gdp_estimates_and_core_pce_keep_separate_histories(spec):
    rows = []
    for i in range(4):
        rows.append(ff_row(10 + i, "GDP_ADVANCE_QOQ", et(f"2024-0{i + 1}-25", "08:30"), 100.0 * i, 0.0))
        rows.append(ff_row(20 + i, "GDP_PRELIM_QOQ", et(f"2024-0{i + 1}-27", "08:30"), 0.1 * i, 0.0))
        rows.append(ff_row(30 + i, "CORE_PCE_MOM", et(f"2024-0{i + 1}-28", "08:30"), 0.01 * i, 0.0))
    s = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec).set_index("event_id")
    for fam_first in (10, 20, 30):
        assert [s.at[f"ff:{fam_first + i}", "surprise_std_n_history"] for i in range(4)] == [0, 1, 2, 3]
    prior = np.array([0.0, 0.1, 0.2])
    assert s.at["ff:23", "surprise_std"] == pytest.approx((0.3 - prior.mean()) / prior.std(ddof=1))


def test_quantile_diagnostic_requires_distinct_raw_surprises_and_skips_weak_proxies(spec):
    """Identical raw surprises drift to distinct standardized values as the history evolves; they
    must not be spread across quantile bins. Weak proxies (FOMC) get no quantile diagnostic."""
    raws = [0.1, -0.1, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0]          # after the history, only raw 0.0 values
    dates = [x.date().isoformat() for x in pd.bdate_range("2024-01-02", periods=len(raws), freq="7D")]
    rows = [ff_row(i, "CPI_MOM", et(d, "08:30"), r, 0.0) for i, (d, r) in enumerate(zip(dates, raws))]
    rows += [ff_row(100 + i, "FED_FUNDS_RATE", et(d, "14:00"), 5.0 + r, 5.0) for i, (d, r) in enumerate(zip(dates, raws))]
    bars = {s: er.SymbolBars(make_bars(dates)) for s in ("QQQ", "SPY")}
    wide, _, _ = build_event_response(pd.DataFrame(rows), spec, EMPTY, bars, {})
    q = diagnostics_tables(wide, spec)["by_quantile_bin"]
    cpi = q[(q.event_family == "CPI_MOM") & (q.horizon_min == 5) & (q.symbol == "SPY")]
    assert len(cpi) == 1 and cpi["skipped_reason"].iloc[0] == "insufficient_distinct_raw_surprises"
    assert cpi["unique_std_surprises"].iloc[0] > cpi["unique_raw_surprises"].iloc[0]  # the drift hazard is present
    fomc = q[(q.event_family == "FED_FUNDS_RATE") & (q.horizon_min == 5)]
    assert set(fomc["skipped_reason"]) == {"weak_surprise_proxy"} and (fomc["actual_bins"] == 0).all()


# --------------------------------------------------------------------------- SymbolBars storage resolution
RESOLUTIONS = ["ns", "us", "ms", "s"]
WINDOW_FIELDS = ["status", "n_expected_bars", "n_observed_bars", "coverage", "ret", "logret", "high_exc",
                 "low_exc", "range", "rv_abs", "rv_sq", "volume"]


def _windows_with_bars(bars_df, spec, t):
    sched = er.calendar_frame(dt.date(2024, 2, 1), dt.date(2024, 4, 30))
    rows = er.compute_release_windows(t, "SPY", er.SymbolBars(bars_df), EMPTY, spec, sched, dt.date(2026, 9, 30))
    return {r["window"]: r for r in rows}


def _as_resolution(df, unit):
    out = df.copy()
    out["timestamp_utc"] = out["timestamp_utc"].astype(f"datetime64[{unit}, UTC]")
    assert str(out["timestamp_utc"].dtype) == f"datetime64[{unit}, UTC]"
    return out


def test_complete_window_identical_for_every_storage_resolution(spec):
    t = et("2024-03-12", "08:30")
    bars = make_bars(["2024-03-12"])
    ref = _windows_with_bars(bars, spec, t)["post5m"]
    assert ref["status"] == er.OK and ref["n_observed_bars"] == 6
    for unit in RESOLUTIONS:
        w = _windows_with_bars(_as_resolution(bars, unit), spec, t)["post5m"]
        assert {k: w[k] for k in WINDOW_FIELDS} == {k: ref[k] for k in WINDOW_FIELDS}, unit


def test_missing_bar_detected_identically_for_every_storage_resolution(spec):
    t = et("2024-03-12", "08:30")
    bars = make_bars(["2024-03-12"], drop={t + pd.Timedelta(minutes=2)})
    for unit in RESOLUTIONS:
        w = _windows_with_bars(_as_resolution(bars, unit), spec, t)
        assert w["post5m"]["status"] == er.INSUFFICIENT and w["post5m"]["ret"] is None, unit
        assert w["post5m"]["coverage"] == pytest.approx(5 / 6) and w["post1m"]["status"] == er.OK, unit


def test_aware_non_utc_bars_normalize_to_utc(spec):
    t = et("2024-03-12", "08:30")
    bars = make_bars(["2024-03-12"])
    ny = bars.assign(timestamp_utc=bars["timestamp_utc"].dt.tz_convert("America/New_York").astype("datetime64[us, America/New_York]"))
    a, b = _windows_with_bars(bars, spec, t)["post5m"], _windows_with_bars(ny, spec, t)["post5m"]
    assert {k: a[k] for k in WINDOW_FIELDS} == {k: b[k] for k in WINDOW_FIELDS}
    py = bars.assign(timestamp_utc=[x.to_pydatetime() for x in bars["timestamp_utc"]]).astype({"timestamp_utc": object})
    c = _windows_with_bars(py, spec, t)["post5m"]
    assert {k: a[k] for k in WINDOW_FIELDS} == {k: c[k] for k in WINDOW_FIELDS}


@pytest.mark.parametrize("unit", RESOLUTIONS)
def test_naive_bar_timestamps_still_fail(unit):
    bars = make_bars(["2024-03-12"])
    naive = bars.assign(timestamp_utc=bars["timestamp_utc"].dt.tz_localize(None).astype(f"datetime64[{unit}]"))
    with pytest.raises(ValueError, match="naive"):
        er.SymbolBars(naive)
    obj = bars.assign(timestamp_utc=[x.tz_localize(None).to_pydatetime() for x in bars["timestamp_utc"]]).astype({"timestamp_utc": object})
    with pytest.raises(ValueError, match="naive"):
        er.SymbolBars(obj)
