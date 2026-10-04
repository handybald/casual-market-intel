"""Macro event -> surprise -> market response research layer, on synthetic events and bars
(plus the committed registry for the FOMC known-gap case)."""
import copy
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.market_exceptions import MarketExceptionRegistry
from src.research import event_response as er
from src.research.baseline import add_baseline, assign_split
from src.research.dataset import WIDE_KEY, WINDOW_KEY, build_event_response, canonical_content_sha256
from src.research.events import (
    AFTER_RESEARCH_END, EXCLUDED_AMBIGUOUS_TIMESTAMP, EXCLUDED_TIMESTAMP_CONFLICT, UNSCHEDULED_RELEASE, USABLE,
    build_event_table, load_research_spec,
)
from src.research.surprise import add_surprises
from tests.test_market_exceptions import entry, registry

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
EMPTY = MarketExceptionRegistry.empty()


@pytest.fixture
def spec():
    s = copy.deepcopy(load_research_spec())
    s["standardization"]["min_history"] = 3
    s["baseline"]["min_history"] = 2
    return s


def et(date, hhmm, sec=0):
    h, m = map(int, hhmm.split(":"))
    return pd.Timestamp(dt.datetime.combine(dt.date.fromisoformat(date), dt.time(h, m, sec), tzinfo=NY)).tz_convert("UTC")


def ff_row(i, fam, ts, actual, forecast, bundle=None, quality="CONFIRMED"):
    return {"event_id": f"ff:{i}", "source_event_id": str(i), "event_family": fam, "release_bundle": bundle or fam,
            "release_timestamp_utc": ts, "timestamp_quality": quality, "actual": actual, "provider_forecast": forecast,
            "previous": None, "revised_previous": None, "actual_unit": "PERCENT", "reference_period": None,
            "raw_artifact_checksum": "x"}


def make_bars(dates, drop=(), start="04:00", end="20:00"):
    rows = []
    for d in dates:
        t, stop = et(d, start), et(d, end)
        k = 0
        while t < stop:
            if t not in drop:
                c = 100.0 + 0.01 * k
                rows.append({"timestamp_utc": t, "open": c, "high": c + 0.02, "low": c - 0.02, "close": c, "volume": 100.0 + k})
            t += pd.Timedelta(minutes=1)
            k += 1
    return pd.DataFrame(rows)


def windows_for(ts, bars_df, reg=EMPTY, spec=None, symbol="SPY", research_end=dt.date(2026, 9, 30)):
    sched = er.calendar_frame(dt.date(2023, 10, 1), dt.date(2024, 12, 31))
    rows = er.compute_release_windows(ts, symbol, er.SymbolBars(bars_df), reg, spec, sched, research_end)
    return {r["window"]: r for r in rows}


# --------------------------------------------------------------------------- surprise
def _surprise_events(spec, values, fam="CPI_MOM"):
    rows = [ff_row(i, fam, et(f"2024-{i + 1:02d}-10", "08:30"), v, 0.0) for i, v in enumerate(values)]
    return add_surprises(build_event_table(pd.DataFrame(rows), spec), spec)


def test_standardization_is_strictly_causal(spec):
    vals = [0.1, -0.2, 0.3, 0.0, 0.5, -0.1]
    ev = _surprise_events(spec, vals)
    assert list(ev["surprise_std_status"][:3]) == ["insufficient_history"] * 3
    for k in range(3, len(vals)):
        prior = np.array(vals[:k])
        assert ev.loc[k, "surprise_std"] == pytest.approx((vals[k] - prior.mean()) / prior.std(ddof=1))
        assert ev.loc[k, "surprise_std_n_history"] == k
    # changing a LATER surprise never changes an earlier standardized value
    later = _surprise_events(spec, vals[:5] + [9.9])
    assert list(later["surprise_std"][:5]) == list(ev["surprise_std"][:5])


def test_standardization_never_pools_families_or_uses_excluded_events(spec):
    rows = [ff_row(i, "CPI_MOM", et(f"2024-{i + 1:02d}-10", "08:30"), 0.1 * i, 0.0) for i in range(5)]
    rows += [ff_row(100 + i, "NFP", et(f"2024-{i + 1:02d}-05", "08:30"), 1000.0 * i, 0.0) for i in range(5)]
    rows.append(ff_row(99, "CPI_MOM", pd.NaT, 50.0, 0.0, quality="TENTATIVE"))  # excluded: must not enter history
    ev = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec)
    cpi = ev[(ev.event_family == "CPI_MOM") & (ev.event_status == USABLE)].reset_index(drop=True)
    prior = np.array([0.0, 0.1, 0.2])
    assert cpi.loc[3, "surprise_std"] == pytest.approx((0.3 - prior.mean()) / prior.std(ddof=1))


def test_zero_dispersion_and_missing_forecast(spec):
    ev = _surprise_events(spec, [0.0, 0.0, 0.0, 0.25])
    assert ev.loc[3, "surprise_std_status"] == "zero_historical_dispersion" and pd.isna(ev.loc[3, "surprise_std"])
    rows = [ff_row(1, "CPI_MOM", et("2024-01-10", "08:30"), 0.2, None)]
    e = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec)
    assert pd.isna(e.loc[0, "surprise_raw"]) and e.loc[0, "surprise_std_status"] == "missing_forecast"


def test_relative_surprise_only_where_meaningful(spec):
    rows = [ff_row(1, "NFP", et("2024-01-05", "08:30"), 300.0, 200.0), ff_row(2, "NFP", et("2024-02-02", "08:30"), 50.0, -20.0),
            ff_row(3, "CPI_MOM", et("2024-01-10", "08:30"), 0.3, 0.2)]
    e = add_surprises(build_event_table(pd.DataFrame(rows), spec), spec).set_index("event_id")
    assert e.at["ff:1", "surprise_relative"] == pytest.approx(0.5)
    assert e.at["ff:2", "surprise_relative_status"] == "forecast_not_positive"
    assert e.at["ff:3", "surprise_relative_status"] == "not_meaningful_for_family" and pd.isna(e.at["ff:3", "surprise_relative"])


def test_every_family_has_explicit_numeric_direction_semantics(spec):
    for fam, fs in spec["families"].items():
        assert fs["numeric_surprise_direction"].startswith("positive = ")
        assert "bullish" not in fs["numeric_surprise_direction"].lower()


# --------------------------------------------------------------------------- event status
def test_event_statuses(spec):
    rows = [
        ff_row(1, "CPI_YOY", et("2024-03-12", "07:30"), 3.2, 3.1, bundle="CPI"),     # conflicts with its bundle
        ff_row(2, "CPI_MOM", et("2024-03-12", "08:30"), 0.4, 0.4, bundle="CPI"),
        ff_row(3, "NFP", pd.NaT, 100.0, 150.0, quality="TENTATIVE"),
        ff_row(4, "FED_FUNDS_RATE", et("2024-03-15", "17:00"), 0.25, None),         # unscheduled
        ff_row(5, "CPI_MOM", et("2026-10-14", "08:30"), 0.2, 0.2),                    # after research_end
    ]
    ev = build_event_table(pd.DataFrame(rows), spec).set_index("event_id")
    assert ev.at["ff:1", "event_status"] == EXCLUDED_TIMESTAMP_CONFLICT
    assert ev.at["ff:2", "event_status"] == USABLE
    assert ev.at["ff:3", "event_status"] == EXCLUDED_AMBIGUOUS_TIMESTAMP
    assert ev.at["ff:4", "event_status"] == UNSCHEDULED_RELEASE
    assert ev.at["ff:5", "event_status"] == AFTER_RESEARCH_END
    assert all(ev["event_status_reason"].notna() == (ev["event_status"] != USABLE))


# --------------------------------------------------------------------------- alignment and windows
DAY = "2024-03-12"  # Tuesday session; 08:30 ET = 12:30Z (DST)


def test_exact_minute_alignment_and_window_geometry(spec):
    bars = make_bars([DAY])
    t = et(DAY, "08:30")
    w = windows_for(t, bars, spec=spec)
    c = bars.set_index("timestamp_utc")["close"]
    ref = c[t - pd.Timedelta(minutes=1)]
    assert w["post1m"]["ret"] == pytest.approx(c[t] / ref - 1)                 # release-minute bar
    assert w["post5m"]["ret"] == pytest.approx(c[t + pd.Timedelta(minutes=4)] / ref - 1)
    assert w["pre5m"]["ret"] == pytest.approx(ref / c[t - pd.Timedelta(minutes=6)] - 1)
    assert w["post5m"]["anchor_bar_utc"] == (t - pd.Timedelta(minutes=1)).isoformat()
    assert w["post5m"]["first_bar_utc"] == t.isoformat() and w["post5m"]["last_bar_utc"] == (t + pd.Timedelta(minutes=4)).isoformat()
    assert w["pre5m"]["last_bar_utc"] == (t - pd.Timedelta(minutes=1)).isoformat()
    assert w["post5m"]["release_timestamp_minute_aligned"] and not w["post5m"]["release_bar_straddles_release"]
    assert w["post5m"]["release_session_state"] == "premarket" and w["post5m"]["status"] == er.OK
    assert w["post5m"]["n_expected_bars"] == w["post5m"]["n_observed_bars"] == 6


def test_seconds_inside_the_minute_are_flagged_not_rounded(spec):
    bars = make_bars([DAY])
    w = windows_for(et(DAY, "08:30", 12), bars, spec=spec)
    assert not w["post1m"]["release_timestamp_minute_aligned"] and w["post1m"]["release_bar_straddles_release"]
    assert w["post1m"]["first_bar_utc"] == et(DAY, "08:30").isoformat()       # release-containing bar
    assert w["post1m"]["anchor_bar_utc"] == et(DAY, "08:29").isoformat()      # last bar completed before t


def test_missing_bar_is_insufficient_never_zero(spec):
    t = et(DAY, "08:30")
    w = windows_for(t, make_bars([DAY], drop={t + pd.Timedelta(minutes=2)}), spec=spec)
    assert w["post5m"]["status"] == er.INSUFFICIENT and w["post5m"]["ret"] is None and w["post5m"]["volume"] is None
    assert w["post5m"]["coverage"] == pytest.approx(5 / 6)
    assert w["post1m"]["status"] == er.OK  # the release-minute window itself is intact


def test_sparse_premarket_preserves_coverage_and_nulls_features(spec):
    t = et(DAY, "08:30")
    sparse = {t - pd.Timedelta(minutes=k) for k in range(2, 61, 2)}   # every other pre-market minute missing
    w = windows_for(t, make_bars([DAY], drop=sparse), spec=spec)
    assert w["pre60m"]["status"] == er.INSUFFICIENT and w["pre60m"]["ret"] is None
    assert 0.4 < w["pre60m"]["coverage"] < 0.6 and w["pre60m"]["n_expected_bars"] == 61
    assert w["post5m"]["status"] == er.OK


def test_registry_is_consulted_for_every_window(spec, monkeypatch):
    calls = []
    orig = MarketExceptionRegistry.query_window

    def spy(self, *a, **k):
        calls.append(a)
        return orig(self, *a, **k)

    monkeypatch.setattr(MarketExceptionRegistry, "query_window", spy)
    w = windows_for(et(DAY, "08:30"), make_bars([DAY]), spec=spec)
    queried = {(pd.Timestamp(a[1]), pd.Timestamp(a[2])) for a in calls}
    for x in w.values():  # each window's own span (anchor .. last bar end) was checked
        span = (pd.Timestamp(x["anchor_bar_utc"]), pd.Timestamp(x["last_bar_utc"]) + pd.Timedelta(minutes=1))
        assert span in queried, x["window"]
    assert len(w) == len(er.window_specs(spec))


def test_provider_gap_in_window_nulls_features_with_ids(spec):
    reg = registry(entry("gap-x", "provider_gap", DAY, "08:32", "08:40", symbols=("SPY",)))
    t = et(DAY, "08:30")
    w = windows_for(t, make_bars([DAY]), reg=reg, spec=spec)
    assert w["post5m"]["status"] == er.PROVIDER_GAP and w["post5m"]["ret"] is None and w["post5m"]["exception_ids"] == "gap-x"
    assert w["post1m"]["status"] == er.OK and w["pre5m"]["status"] == er.OK   # windows not touching the gap stay usable


def test_market_halt_is_tagged_and_computed_on_remaining_bars(spec):
    t = et(DAY, "10:00")
    halt = {t + pd.Timedelta(minutes=k) for k in range(5, 20)}   # no bars 10:05-10:19
    bars = make_bars([DAY], drop=halt)
    reg = registry(entry("halt-x", "market_wide_halt", DAY, "10:05", "10:20", symbols=("SPY",), provider="*", feed="*",
                         invalidates=False))
    w = windows_for(t, bars, reg=reg, spec=spec)
    assert w["post30m"]["status"] == er.OK_MARKET_HALT and w["post30m"]["ret"] is not None
    assert w["post30m"]["has_market_halt"] and "market_halt_in_window" in w["post30m"]["exception_tags"]
    assert w["post30m"]["n_expected_bars"] == 31 - 15
    assert w["post30m"]["volume_rel_status"] == "not_comparable_excluded_minutes"
    assert windows_for(t, bars, spec=spec)["post30m"]["status"] == er.INSUFFICIENT  # without the registry: just missing


def test_exchange_closed_day(spec):
    w = windows_for(et("2024-03-29", "08:30"), make_bars(["2024-03-28"]), spec=spec)   # Good Friday
    assert {x["status"] for x in w.values()} == {er.EXCHANGE_CLOSED}
    assert w["post5m"]["release_session_state"] == "closed_day"


def test_window_reaching_past_research_end_is_provisional(spec):
    t = et(DAY, "15:30")
    w = windows_for(t, make_bars([DAY]), spec=spec, research_end=dt.date(2024, 3, 11))
    assert w["post5m"]["status"] == er.PROVISIONAL and w["post5m"]["ret"] is None


def test_volume_baseline_uses_only_prior_sessions(spec):
    days = [d.date().isoformat() for d in pd.bdate_range("2024-02-01", "2024-03-12")]
    bars = make_bars(days)
    t = et(DAY, "10:00")
    w = windows_for(t, bars, spec=spec)
    assert w["post5m"]["volume_rel_status"] == "ok"
    # identical clock-time volumes every day -> ratio exactly 1, and a later day cannot change it
    assert w["post5m"]["volume_rel"] == pytest.approx(1.0)
    later = make_bars(days + ["2024-03-13"])
    later.loc[later.timestamp_utc >= et("2024-03-13", "04:00"), "volume"] *= 50
    assert windows_for(t, later, spec=spec)["post5m"]["volume_rel"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- FOMC known gap (committed registry)
def test_fomc_2018_05_02_qqq_gap_excluded_spy_usable(spec):
    reg = MarketExceptionRegistry.load(REPO_ROOT / "config" / "market_exceptions.yaml")
    sched = er.calendar_frame(dt.date(2018, 4, 1), dt.date(2018, 5, 31))
    t = et("2018-05-02", "14:00")
    bars = er.SymbolBars(make_bars(["2018-05-02"]))
    q = {r["window"]: r for r in er.compute_release_windows(t, "QQQ", bars, reg, spec, sched, dt.date(2026, 9, 30))}
    s = {r["window"]: r for r in er.compute_release_windows(t, "SPY", bars, reg, spec, sched, dt.date(2026, 9, 30))}
    assert all(r["status"] == er.PROVIDER_GAP for r in q.values())
    assert q["post5m"]["exception_ids"] == "alpaca-sip-QQQ-2018-05-02-gap1"
    assert all(r["status"] == er.OK for r in s.values())


# --------------------------------------------------------------------------- dataset, baseline, split, provenance
def _mini_dataset(spec, reg=EMPTY):
    dates = ["2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12", "2024-07-11", "2024-08-14"]
    rows = [ff_row(i, "CPI_MOM", et(d, "08:30"), 0.1 * (i % 3), 0.1, bundle="CPI") for i, d in enumerate(dates)]
    rows += [ff_row(50 + i, "CORE_CPI_MOM", et(d, "08:30"), 0.2, 0.2, bundle="CPI") for i, d in enumerate(dates)]
    bars = {s: er.SymbolBars(make_bars(dates)) for s in ("QQQ", "SPY")}
    ident = {"market_provider": "alpaca", "market_feed": "sip", "market_dataset_fingerprint": "sha256:test"}
    return build_event_response(pd.DataFrame(rows), spec, reg, bars, ident)


def test_dataset_grain_identity_and_fingerprint_propagation(spec):
    wide, windows, events = _mini_dataset(spec)
    assert len(wide) == len(events) * 2 and set(wide.symbol) == {"QQQ", "SPY"}
    assert (wide["market_dataset_fingerprint"] == "sha256:test").all()
    # members of one simultaneous release share one response
    g = wide[wide.symbol == "SPY"].groupby("release_id")
    assert all(x["post5m_ret"].nunique(dropna=False) == 1 for _, x in g)
    assert set(wide["concurrent_families"].dropna()) == {"CORE_CPI_MOM,CPI_MOM"}
    assert len(windows) == wide["release_id"].nunique() * 2 * len(er.window_specs(spec))


def test_generation_is_deterministic(spec):
    a, aw, _ = _mini_dataset(spec)
    b, bw, _ = _mini_dataset(spec)
    assert canonical_content_sha256(a, WIDE_KEY) == canonical_content_sha256(b, WIDE_KEY)
    assert canonical_content_sha256(aw, WINDOW_KEY) == canonical_content_sha256(bw, WINDOW_KEY)


def test_baseline_uses_only_strictly_earlier_releases(spec):
    wide, _, _ = _mini_dataset(spec)
    base = wide.copy()
    sub = base[(base.symbol == "SPY") & (base.event_family == "CORE_CPI_MOM")].sort_values("release_timestamp_utc")
    i3 = sub.index[3]
    prior = sub.loc[sub.index[:3], "post5m_ret"].astype(float)
    assert base.at[i3, "expected_post5m_ret"] == pytest.approx(prior.mean())
    assert base.at[i3, "resid_post5m_ret"] == pytest.approx(base.at[i3, "post5m_ret"] - prior.mean())
    assert pd.Timestamp(base.at[i3, "baseline_post5m_training_cutoff_utc"]) < base.at[i3, "release_timestamp_utc"]
    # perturbing a FUTURE response leaves the expectation unchanged
    future = base.copy()
    future.loc[sub.index[4:], "post5m_ret"] = 99.0
    redone = add_baseline(future.drop(columns=[c for c in future.columns if c.startswith(("expected_", "resid_", "baseline_"))]), spec)
    assert redone.at[i3, "expected_post5m_ret"] == pytest.approx(base.at[i3, "expected_post5m_ret"])
    assert base.at[sub.index[0], "baseline_post5m_status"] == "insufficient_history"


def test_chronological_split(spec):
    assert assign_split("2016-01-08", spec) == "development"
    assert assign_split("2020-12-31", spec) == "development"
    assert assign_split("2021-01-01", spec) == "validation"
    assert assign_split("2023-01-01", spec) == "test"
    assert assign_split("2026-10-01", spec) == "outside_research_range"
    assert assign_split(None, spec) == "unassigned"
    s = spec["split"]
    assert s["development"][1] < s["validation"][0] <= s["validation"][1] < s["test"][0] <= s["test"][1] == s["research_end"]
