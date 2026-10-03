"""Tests for src/features/market_response.py and scripts/build_macro_market_response.py (synthetic bars only)."""
import datetime as dt
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.data.normalize.io import events_to_dataframe
from src.data.schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit
from src.features import market_response as mr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_macro_market_response as cli  # noqa: E402

UTC = dt.timezone.utc
M = dt.timedelta(minutes=1)
T = dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)         # 08:30 America/New_York (EDT)
CFG = mr.ResponseConfig()


def make_bars(t=T, close=lambda i: 100 + 0.5 * i, high=None, low=None, vol=lambda i: 1000.0 + i, drop=(), start=-45, end=70):
    """One bar per minute from t+start to t+end; `i` is the bar's minute offset from t (bar START), i=-1 is the
    baseline bar. `drop` = offsets with no bar."""
    idx, rows = [], []
    for i in range(start, end + 1):
        if i in drop:
            continue
        c = close(i)
        idx.append(t + i * M)
        rows.append({"open": c, "high": high(i) if high else c + 0.1, "low": low(i) if low else c - 0.1, "close": c, "volume": vol(i)})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="timestamp_utc"))


def window(bars, t=T):
    return mr.compute_window(mr.BarStore(bars), t, CFG)


def approx(x):
    return pytest.approx(x, rel=1e-12, abs=1e-15)


# ---- exact 08:30 event, baseline, horizon semantics ----
def test_exact_0830_event_baseline_is_the_last_completed_bar_before_t():
    w = window(make_bars())
    assert w["baseline_found"] and w["baseline_timestamp_utc"] == T - M       # the 08:29 ET bar
    assert w["baseline_price"] == 99.5                                        # close of bar i=-1, not the 08:30 bar
    assert w["market_window_status"] == mr.OK and w["bars_expected"] == 90 and w["bars_found"] == 90
    assert w["missing_bar_count"] == 0 and w["timestamp_minute_aligned"] is True


@pytest.mark.parametrize("k", [1, 5, 15, 30, 60])
def test_post_horizon_uses_the_close_of_the_kth_post_release_bar(k):
    w = window(make_bars())
    end_close = 100 + 0.5 * (k - 1)                # bar STARTING at T+(k-1)m: +1m -> the 08:30 bar, +5m -> the 08:34 bar
    assert w[f"ret_post_{k}m"] == approx(end_close / 99.5 - 1)
    assert w[f"direction_{k}m"] == 1


def test_bar_at_t_is_post_release_not_baseline():
    # a huge move inside the 08:30 bar must show up in +1m, never in the baseline
    bars = make_bars(close=lambda i: 200.0 if i >= 0 else 100.0)
    w = window(bars)
    assert w["baseline_price"] == 100.0 and w["ret_post_1m"] == approx(1.0)


def test_pre_returns_end_at_the_baseline_bar():
    w = window(make_bars(close=lambda i: 100 + i))
    assert w["ret_pre_5m"] == approx(99 / 94 - 1)            # close(-1) / close(-6) - 1
    assert w["ret_pre_30m"] == approx(99 / 69 - 1)           # close(-1) / close(-31) - 1 (a pre-release bar, no leakage)


def test_simple_returns_and_directions_including_unchanged_and_negative():
    flat = window(make_bars(close=lambda i: 50.0))
    assert flat["ret_post_5m"] == 0 and flat["direction_5m"] == 0
    down = window(make_bars(close=lambda i: 50.0 if i < 0 else 49.0))
    assert down["ret_post_1m"] == approx(49 / 50 - 1) and down["direction_1m"] == -1
    tiny = window(make_bars(close=lambda i: 50.0 if i < 0 else 50.0 * (1 + 1e-14)))
    assert tiny["direction_1m"] == 0                                  # within the numerical tolerance
    assert mr.direction(None, 1e-12) is None


# ---- excursions, volatility, volume ----
def test_max_excursions_use_intrabar_high_and_low_within_the_horizon_only():
    bars = make_bars(close=lambda i: 100.0, high=lambda i: 130.0 if i == 3 else (150.0 if i == 20 else 100.5),
                     low=lambda i: 90.0 if i == 4 else (70.0 if i == 40 else 99.5))
    w = window(bars)
    assert w["max_up_5m"] == approx(130 / 100 - 1) and w["max_down_5m"] == approx(90 / 100 - 1)
    assert w["max_up_30m"] == approx(150 / 100 - 1) and w["max_down_30m"] == approx(90 / 100 - 1)      # 20m spike now in
    assert w["max_up_60m"] == approx(1.5 - 1) and w["max_down_60m"] == approx(70 / 100 - 1)            # 40m low now in


def test_realized_vol_is_sqrt_of_summed_squared_log_returns_not_annualized():
    closes = {i: 100.0 * (1.001 ** i) for i in range(-45, 71)}
    w = window(make_bars(close=lambda i: closes[i]))
    per_min = math.log(1.001)
    assert w["realized_vol_pre_30m"] == approx(math.sqrt(30 * per_min ** 2))
    assert w["realized_vol_post_30m"] == approx(math.sqrt(30 * per_min ** 2))
    assert w["realized_vol_post_60m"] == approx(math.sqrt(60 * per_min ** 2))


def test_realized_vol_first_post_return_is_measured_against_the_baseline_close():
    w = window(make_bars(close=lambda i: 100.0 if i < 0 else 101.0))          # one jump at T, then flat
    assert w["realized_vol_post_30m"] == approx(abs(math.log(1.01)))
    assert w["realized_vol_pre_30m"] == 0.0


def test_volume_sums_and_ratio():
    w = window(make_bars(vol=lambda i: 10.0 if i < 0 else 30.0))
    assert w["volume_pre_30m"] == 300 and w["volume_post_30m"] == 900 and w["volume_post_60m"] == 1800
    assert w["volume_ratio_30m"] == 3.0 and w["volume_ratio_30m_status"] == "OK"


def test_zero_or_missing_pre_volume_is_explicit_not_infinite():
    zero = window(make_bars(vol=lambda i: 0.0 if i < 0 else 5.0))
    assert zero["volume_ratio_30m"] is None and zero["volume_ratio_30m_status"] == "PRE_VOLUME_ZERO"
    missing = window(make_bars(drop={-10}))
    assert missing["volume_pre_30m"] is None and missing["volume_ratio_30m"] is None
    assert missing["volume_ratio_30m_status"] == "PRE_VOLUME_MISSING"
    assert window(make_bars(drop={7}))["volume_ratio_30m_status"] == "POST_VOLUME_MISSING"


# ---- missing data: never interpolated ----
def test_missing_baseline_bar():
    w = window(make_bars(drop=set(range(-8, 0))))             # nothing within the 5-minute baseline lookback
    assert w["market_window_status"] == mr.NO_BASELINE_BAR and w["baseline_found"] is False
    assert w["baseline_price"] is None and w["ret_post_1m"] is None and w["max_up_5m"] is None


def test_stale_baseline_within_lookback_uses_last_traded_bar_and_leaves_unsafe_features_null():
    w = window(make_bars(drop={-1, -2}))
    assert w["baseline_timestamp_utc"] == T - 3 * M and w["baseline_price"] == 100 + 0.5 * -3
    assert w["ret_post_1m"] == approx(100 / w["baseline_price"] - 1)
    assert w["realized_vol_post_30m"] is None                 # needs a consecutive chain starting at close(T-1m)
    assert w["market_window_status"] == mr.INSUFFICIENT_PRE_WINDOW and w["missing_pre_bars"] == 2


def test_missing_post_release_bar_nulls_only_the_features_that_need_it():
    w = window(make_bars(drop={3}))                            # the +4m bar (starts T+3m) is absent
    assert w["market_window_status"] == mr.INSUFFICIENT_POST_WINDOW and w["missing_post_bars"] == 1
    assert w["max_up_5m"] is None and w["max_down_5m"] is None and w["volume_post_30m"] is None
    assert w["ret_post_1m"] is not None and w["ret_post_15m"] is not None       # their own bars exist
    assert w["ret_post_5m"] == approx((100 + 0.5 * 4) / 99.5 - 1)               # bar T+4m exists
    assert window(make_bars(drop={4}))["ret_post_5m"] is None                   # the 5m endpoint bar itself missing


def test_missing_internal_pre_bar_is_insufficient_pre_window():
    w = window(make_bars(drop={-15}))
    assert w["market_window_status"] == mr.INSUFFICIENT_PRE_WINDOW and w["missing_pre_bars"] == 1
    assert w["realized_vol_pre_30m"] is None and w["volume_pre_30m"] is None
    assert w["ret_post_5m"] is not None and w["bars_found"] == 89 and w["missing_bar_count"] == 1


def test_no_market_data():
    assert mr.compute_window(None, T, CFG)["market_window_status"] == mr.MISSING_MARKET_DATA
    empty = mr.compute_window(mr.BarStore(make_bars(t=T + dt.timedelta(days=3))), T, CFG)
    assert empty["market_window_status"] == mr.MISSING_MARKET_DATA and empty["bars_found"] is None


def test_no_trusted_timestamp():
    w = mr.compute_window(mr.BarStore(make_bars()), None, CFG)
    assert w["market_window_status"] == mr.NO_TRUSTED_EVENT_TIMESTAMP and w["ret_post_1m"] is None and w["bars_found"] is None


# ---- non-aligned timestamps ----
def test_non_aligned_release_excludes_the_straddling_bar_from_both_sides():
    t = T + dt.timedelta(seconds=30)                            # 12:30:30 -> bar 12:30 straddles the release
    w = window(make_bars(), t)
    assert w["timestamp_minute_aligned"] is False
    assert w["baseline_timestamp_utc"] == T - M                 # 12:29 (ends 12:30:00, wholly pre-release)
    assert w["ret_post_1m"] == approx(100.5 / 99.5 - 1)         # first wholly post-release bar starts 12:31 (i=1)
    assert w["realized_vol_post_30m"] is None                   # its reference bar would straddle t


# ---- sessions and DST ----
def test_session_classification_premarket_regular_after_hours_closed():
    et = lambda y, mo, d, h, mi: dt.datetime(y, mo, d, h, mi, tzinfo=mr.NY).astimezone(UTC)
    assert mr.classify_session(et(2025, 9, 5, 8, 30)) == "PREMARKET"
    assert mr.classify_session(et(2025, 9, 5, 9, 29)) == "PREMARKET" and mr.classify_session(et(2025, 9, 5, 9, 30)) == "REGULAR"
    assert mr.classify_session(et(2025, 9, 5, 10, 0)) == "REGULAR" and mr.classify_session(et(2025, 9, 5, 16, 0)) == "AFTER_HOURS"
    assert mr.classify_session(et(2025, 9, 5, 19, 59)) == "AFTER_HOURS" and mr.classify_session(et(2025, 9, 5, 20, 0)) == "CLOSED"
    assert mr.classify_session(et(2025, 9, 5, 3, 59)) == "CLOSED"
    assert mr.classify_session(et(2025, 9, 6, 10, 0)) == "CLOSED"            # Saturday


def test_session_uses_new_york_time_across_dst():
    same_utc = dt.datetime(2025, 3, 7, 13, 30, tzinfo=UTC), dt.datetime(2025, 3, 14, 13, 30, tzinfo=UTC)
    assert mr.classify_session(same_utc[0]) == "PREMARKET"                   # EST (UTC-5): 08:30
    assert mr.classify_session(same_utc[1]) == "REGULAR"                     # EDT (UTC-4) since Mar 9: 09:30
    winter, summer = dt.datetime(2025, 12, 5, 13, 30, tzinfo=UTC), dt.datetime(2025, 9, 5, 12, 30, tzinfo=UTC)
    assert winter.astimezone(mr.NY).strftime("%H:%M") == summer.astimezone(mr.NY).strftime("%H:%M") == "08:30"
    assert mr.classify_session(winter) == mr.classify_session(summer) == "PREMARKET"


def test_dst_winter_release_window_is_anchored_on_utc_not_on_the_summer_offset():
    tw = dt.datetime(2025, 12, 5, 13, 30, tzinfo=UTC)                        # 08:30 EST
    w = mr.compute_window(mr.BarStore(make_bars(t=tw)), tw, CFG)
    assert w["market_window_status"] == mr.OK and w["baseline_timestamp_utc"] == tw - M
    assert mr.compute_window(mr.BarStore(make_bars(t=T)), tw, CFG)["market_window_status"] == mr.MISSING_MARKET_DATA


# ---- leakage protection ----
FEATURE_CALLS = {
    "ret_post_1m": (lambda s: mr.ret_post(s, T, 1, 99.5), 0), "ret_post_5m": (lambda s: mr.ret_post(s, T, 5, 99.5), 4),
    "ret_post_15m": (lambda s: mr.ret_post(s, T, 15, 99.5), 14), "ret_post_30m": (lambda s: mr.ret_post(s, T, 30, 99.5), 29),
    "ret_post_60m": (lambda s: mr.ret_post(s, T, 60, 99.5), 59),
    "max_5m": (lambda s: mr.excursions(s, T, 5, 99.5), 4), "max_30m": (lambda s: mr.excursions(s, T, 30, 99.5), 29),
    "max_60m": (lambda s: mr.excursions(s, T, 60, 99.5), 59),
    "vol_post_30m": (lambda s: mr.realized_vol(s, T, 30), 29), "vol_post_60m": (lambda s: mr.realized_vol(s, T, 60), 59),
    "volume_post_30m": (lambda s: mr.volume_sum(s, T, 30), 29), "volume_post_60m": (lambda s: mr.volume_sum(s, T, 60), 59),
    "vol_pre_30m": (lambda s: mr.realized_vol(s, T - 30 * M, 30), -1), "volume_pre_30m": (lambda s: mr.volume_sum(s, T - 30 * M, 30), -1),
    "ret_pre_5m": (lambda s: mr.ret_pre(s, T - M, 99.5, 5), -1), "ret_pre_30m": (lambda s: mr.ret_pre(s, T - M, 99.5, 30), -1),
    "baseline": (lambda s: mr.find_baseline(s, T, CFG), -1),
}


@pytest.mark.parametrize("name", sorted(FEATURE_CALLS))
def test_no_feature_reads_a_bar_beyond_its_horizon(name):
    call, last_offset = FEATURE_CALLS[name]
    store = mr.BarStore(make_bars())
    call(store)
    latest = max(store.accessed)
    assert latest <= T + last_offset * M, f"{name} read the bar at {latest} (allowed up to offset {last_offset}m)"


def test_poisoning_bars_after_each_horizon_never_changes_that_horizons_features():
    base = window(make_bars())
    horizons = {1: ["ret_post_1m", "direction_1m"], 5: ["ret_post_5m", "max_up_5m", "max_down_5m"],
                15: ["ret_post_15m"], 30: ["ret_post_30m", "max_up_30m", "max_down_30m", "realized_vol_post_30m",
                                             "volume_post_30m"], 60: ["ret_post_60m", "max_up_60m", "max_down_60m",
                                                                     "realized_vol_post_60m", "volume_post_60m"]}
    for h, feats in horizons.items():
        poisoned = make_bars(close=lambda i, h=h: 100 + 0.5 * i if i < h else 1e6, high=lambda i, h=h: 100.1 + 0.5 * i if i < h else 1e7,
                             low=lambda i, h=h: 99.9 + 0.5 * i if i < h else -1e7, vol=lambda i, h=h: 1000.0 + i if i < h else 1e9)
        w = window(poisoned)
        for f in feats:
            assert w[f] == base[f], f"{f} changed when bars from +{h}m onward were poisoned"


def test_poisoning_post_release_bars_never_changes_baseline_or_pre_features():
    base = window(make_bars())
    poisoned = window(make_bars(close=lambda i: 100 + 0.5 * i if i < 0 else 1e6, high=lambda i: 100.1 + 0.5 * i if i < 0 else 1e7,
                                low=lambda i: 99.9 + 0.5 * i if i < 0 else -1e7, vol=lambda i: 1000.0 + i if i < 0 else 1e9))
    for f in ["baseline_price", "baseline_timestamp_utc", "ret_pre_5m", "ret_pre_30m", "realized_vol_pre_30m", "volume_pre_30m"]:
        assert poisoned[f] == base[f], f


# ---- events x symbols ----
def event(family="NFP", ts="2025-09-05T12:30:00+00:00", basis="FF_CONFIRMED", eid=None):
    return {"event_id": eid or f"macro:{family}:2025-08", "canonical_event_id": f"{family}:2025-08", "event_family": family,
            "trusted_release_timestamp_utc": ts, "trusted_release_timestamp_basis": basis if ts else None,
            "release_date": "2025-09-05", "reference_period": "2025-08-01", "ff_provider_forecast": 75.0, "actual_value": 22.0,
            "actual_value_source": "MQL5", "actual_minus_forecast": -53.0}


def test_multiple_releases_at_the_same_timestamp_each_get_their_own_row():
    rows = mr.build_market_response([event("NFP"), event("UNEMPLOYMENT_RATE"), event("AVG_HOURLY_EARNINGS_MOM")],
                                    {"QQQ": make_bars()})
    assert [r["event_family"] for r in rows] == ["NFP", "UNEMPLOYMENT_RATE", "AVG_HOURLY_EARNINGS_MOM"]
    assert len({r["ret_post_5m"] for r in rows}) == 1 and len({r["release_timestamp_utc"] for r in rows}) == 1
    assert rows[0]["surprise_raw"] == -53.0 and rows[0]["surprise_z_prior_only"] is None
    assert rows[0]["provider_forecast"] == 75.0 and rows[0]["actual"] == 22.0


def test_symbols_are_independent():
    qqq = make_bars(close=lambda i: 100 + 0.5 * i)
    spy = make_bars(close=lambda i: 200 - 1.0 * i, drop={-1, -2, -3, -4, -5, -6})       # SPY has no baseline
    rows = {r["symbol"]: r for r in mr.build_market_response([event()], {"QQQ": qqq, "SPY": spy})}
    assert rows["QQQ"]["market_window_status"] == mr.OK and rows["QQQ"]["baseline_price"] == 99.5
    assert rows["SPY"]["market_window_status"] == mr.NO_BASELINE_BAR and rows["SPY"]["ret_post_1m"] is None
    only_q = {r["symbol"]: r for r in mr.build_market_response([event()], {"QQQ": qqq})}
    assert only_q["QQQ"]["ret_post_30m"] == rows["QQQ"]["ret_post_30m"]
    assert rows["SPY"]["direction_1m"] is None and rows["QQQ"]["direction_1m"] == 1


def test_event_without_trusted_timestamp_is_reported_not_guessed():
    rows = mr.build_market_response([event("AVG_HOURLY_EARNINGS_YOY", ts=None)], {"QQQ": make_bars(), "SPY": make_bars()})
    assert [r["market_window_status"] for r in rows] == [mr.NO_TRUSTED_EVENT_TIMESTAMP] * 2
    assert all(r["release_timestamp_utc"] is None and r["ret_post_1m"] is None and r["release_session"] is None for r in rows)
    assert all(r["release_timestamp_basis"] == "NO_TRUSTED_EVENT_TIMESTAMP" for r in rows)


def test_row_carries_session_local_time_and_iso_baseline():
    row = mr.build_market_response([event()], {"QQQ": make_bars()})[0]
    assert row["release_session"] == "PREMARKET" and row["release_timestamp_america_new_york"] == "2025-09-05T08:30:00-04:00"
    assert row["baseline_timestamp_utc"] == "2025-09-05T12:29:00+00:00" and row["bars_found"] == 90
    assert list(row) == mr.RESPONSE_COLUMNS


def test_bar_store_rejects_duplicate_timestamps():
    bars = make_bars()
    with pytest.raises(ValueError):
        mr.BarStore(pd.concat([bars, bars.iloc[:1]]))


# ---- loader + CLI ----
def write_massive(root, symbol, bars):
    d = root / symbol / "1min" / "raw"
    d.mkdir(parents=True)
    df = bars.reset_index()
    df["symbol"] = symbol
    df.to_parquet(d / "2025.parquet", index=False)


def test_loader_reads_year_files_and_windows_them(tmp_path):
    write_massive(tmp_path, "QQQ", make_bars())
    df = mr.load_massive_bars("QQQ", T - 5 * M, T + 5 * M, tmp_path)
    assert df.index.min() == T - 5 * M and df.index.max() == T + 5 * M and len(df) == 11
    assert mr.load_massive_bars("SPY", T - 5 * M, T + 5 * M, tmp_path) is None


def nfp_ff_event():
    return MacroEvent(event_id="ff:nfp", event_family="NFP", indicator="Nonfarm Payrolls", reference_period=dt.date(2025, 8, 1),
                      release_timestamp_utc=T, timestamp_quality=TimestampQuality.CONFIRMED, actual=22.0, actual_unit=ValueUnit.THOUSANDS,
                      provider_forecast=75.0, provider_forecast_unit=ValueUnit.THOUSANDS, previous=73.0,
                      previous_unit=ValueUnit.THOUSANDS, source=MacroSource.FOREX_FACTORY,
                      retrieval_timestamp_utc=dt.datetime(2026, 9, 20, tzinfo=UTC))


def test_cli_end_to_end_with_local_files(tmp_path, capsys):
    events_to_dataframe([nfp_ff_event()]).to_parquet(tmp_path / "ff.parquet")
    write_massive(tmp_path / "massive", "QQQ", make_bars())
    write_massive(tmp_path / "massive", "SPY", make_bars(close=lambda i: 300 + i))
    rc = cli.main(["--start", "2025-09-01", "--end", "2025-09-30", "--symbols", "QQQ,SPY", "--ff-events", str(tmp_path / "ff.parquet"),
                   "--mql5-events", str(tmp_path / "none1.parquet"), "--fred-events", str(tmp_path / "none2.parquet"),
                   "--massive-dir", str(tmp_path / "massive"), "--out-parquet", str(tmp_path / "out/resp.parquet"),
                   "--report-dir", str(tmp_path / "rep")])
    assert rc == 0
    df = pd.read_parquet(tmp_path / "out/resp.parquet")
    assert list(df.columns) == mr.RESPONSE_COLUMNS and sorted(df.symbol) == ["QQQ", "SPY"]
    q = df[df.symbol == "QQQ"].iloc[0]
    assert q.market_window_status == "OK" and q.release_timestamp_basis == "FF_CONFIRMED" and q.ret_post_1m == pytest.approx(100 / 99.5 - 1)
    assert q.surprise_raw == -53.0 and q.release_session == "PREMARKET"
    assert (tmp_path / "rep/macro_market_response_2025-09-01_2025-09-30.csv").exists()


def test_cli_rejects_bad_dates():
    assert cli.main(["--start", "2025-09-30", "--end", "2025-09-01"]) == 2
    assert cli.main(["--start", "nope", "--end", "2025-09-01"]) == 2
