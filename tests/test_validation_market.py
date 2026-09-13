import datetime as dt

import numpy as np
import pandas as pd

from src.data.validation.market import validate_market_bars, nyse_sessions, year_range

# 2020-01-02 is the first NYSE trading session of 2020 (Thursday), a
# normal full day: 14:30-21:00 UTC (9:30am-4:00pm ET).
SESSION_OPEN = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)


def _bar(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=1000):
    return {"timestamp_utc": ts, "open": o, "high": h, "low": l, "close": c, "volume": v, "vwap": o, "transactions": 1}


SESSION_CLOSE = dt.datetime(2020, 1, 2, 21, 0, tzinfo=dt.timezone.utc)


def _full_session_bars(open_utc=SESSION_OPEN, close_utc=SESSION_CLOSE, **overrides):
    bars = []
    t = open_utc
    while t < close_utc:
        bars.append(_bar(t, **overrides))
        t += dt.timedelta(minutes=1)
    return bars


def test_empty_dataframe_over_full_year_is_not_reported_clean():
    """Regression: an empty dataset must NOT be classified as clean --
    every expected NYSE session in the year is genuinely missing."""
    df = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.total_rows == 0
    assert report.is_clean is False
    assert report.is_hard_failure is True
    assert report.trading_sessions_with_data == 0
    assert len(report.missing_sessions) == report.expected_trading_sessions
    assert report.expected_trading_sessions > 200  # sanity: a real year has ~250 NYSE sessions


def test_single_present_session_is_not_counted_missing():
    """5 of 390 expected minutes present: NOT "missing" (zero rows), but
    also nowhere near complete -- it's "incomplete" (see
    test_validation_market_phase1.py for the full completeness model)."""
    rows = [_bar(SESSION_OPEN + dt.timedelta(minutes=i)) for i in range(5)]
    df = pd.DataFrame(rows)
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert "2020-01-02" not in report.missing_sessions
    assert "2020-01-02" in report.incomplete_sessions
    assert len(report.missing_sessions) == report.expected_trading_sessions - 1


def test_detects_duplicate_timestamps():
    df = pd.DataFrame([_bar(SESSION_OPEN), _bar(SESSION_OPEN)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.duplicate_timestamp_count == 1
    assert not report.is_clean
    assert report.is_hard_failure


def test_detects_invalid_ohlc_relationship():
    df = pd.DataFrame([_bar(SESSION_OPEN, o=100, h=90, l=99, c=95)])  # high < low
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.invalid_ohlc_count == 1
    assert report.is_hard_failure


def test_detects_non_positive_prices():
    df = pd.DataFrame([_bar(SESSION_OPEN, o=0, h=1, l=-1, c=0.5)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.non_positive_price_count == 1


def test_detects_unsorted_timestamps():
    t1 = SESSION_OPEN
    t2 = SESSION_OPEN - dt.timedelta(minutes=1)
    df = pd.DataFrame([_bar(t1), _bar(t2)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.is_sorted is False


def test_detects_negative_volume():
    df = pd.DataFrame([_bar(SESSION_OPEN, v=-5)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.negative_volume_count == 1


def test_detects_suspicious_volume_outlier():
    # A COMPLETE session (100% coverage) with one volume outlier -- isolates
    # the volume-outlier signal from completeness entirely.
    bars = _full_session_bars(v=1000)
    outlier_idx = 5
    bars[outlier_idx] = _bar(SESSION_OPEN + dt.timedelta(minutes=outlier_idx), v=50000)
    df = pd.DataFrame(bars)
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2))
    assert report.suspicious_volume_count == 1
    # A volume outlier alone is a soft signal, not a hard failure.
    assert report.is_hard_failure is False


# -- regression: NaN rows must not silently pass OHLC/price checks (comparisons with NaN are False) --

def test_null_and_nonfinite_values_are_detected_and_excluded_from_other_checks():
    rows = [
        _bar(SESSION_OPEN, o=100, h=101, l=99, c=100.5),
        {"timestamp_utc": SESSION_OPEN + dt.timedelta(minutes=1), "open": np.nan, "high": 101, "low": 99, "close": 100, "volume": 1000, "vwap": 100, "transactions": 1},
        {"timestamp_utc": SESSION_OPEN + dt.timedelta(minutes=2), "open": 100, "high": np.inf, "low": 99, "close": 100, "volume": 1000, "vwap": 100, "transactions": 1},
    ]
    df = pd.DataFrame(rows)
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.null_or_nonfinite_count == 2
    assert not report.is_clean
    assert report.is_hard_failure
    # The NaN/inf rows are excluded from OHLC/price checks (not silently
    # counted as "valid" the way a raw `<` comparison against NaN would).
    assert report.invalid_ohlc_count == 0
    assert report.non_positive_price_count == 0


def test_intraday_gap_detected_within_session():
    # A near-complete session (384/390 = 98.5%, above the 98% default
    # threshold) with ONE small gap in the middle -- isolates gap
    # detection from the completeness/incomplete-session concept. A
    # small, isolated gap (e.g. a genuine no-qualifying-trade stretch)
    # must remain tolerated, not an automatic hard failure.
    bars = _full_session_bars()
    removed_start = SESSION_OPEN + dt.timedelta(minutes=100)
    bars = [b for b in bars if not (removed_start <= b["timestamp_utc"] < removed_start + dt.timedelta(minutes=6))]
    df = pd.DataFrame(bars)
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), gap_threshold_minutes=5.0)
    assert report.gap_count == 1
    assert report.largest_gap_minutes == 7.0
    assert "2020-01-02" not in report.incomplete_sessions
    assert report.is_hard_failure is False


def test_extended_vs_regular_hours_classification():
    premarket = SESSION_OPEN - dt.timedelta(hours=5, minutes=30)  # 4:00am ET
    regular = SESSION_OPEN
    df = pd.DataFrame([_bar(premarket), _bar(regular)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    assert report.regular_hours_rows == 1
    assert report.extended_hours_rows == 1


# -- early close (item #10: appropriate trading calendar, not a hand-rolled 9:30-16:00 guess) --

def test_early_close_session_has_shorter_expected_window():
    sessions = nyse_sessions(dt.date(2024, 7, 3), dt.date(2024, 7, 3))
    open_utc, close_utc = sessions.iloc[0]["market_open"], sessions.iloc[0]["market_close"]
    session_minutes = (close_utc - open_utc).total_seconds() / 60.0
    assert session_minutes < 390  # July 3rd, 2024 is a documented NYSE early-close day

    rows = [_bar(open_utc + dt.timedelta(minutes=i)) for i in range(0, int(session_minutes), 10)]
    df = pd.DataFrame(rows)
    report = validate_market_bars(df, "QQQ", dt.date(2024, 7, 3), dt.date(2024, 7, 3))
    assert "2024-07-03" not in report.missing_sessions
    assert report.expected_regular_minutes < 390


# -- macro-event-window coverage (item #10/#5: explicit 08:30 ET coverage, N bars not just 1) --

def test_macro_window_coverage_flags_missing_release_but_not_covered_one():
    covered_release = SESSION_OPEN  # 9:30am ET, we have every required offset right here
    missing_release = SESSION_OPEN + dt.timedelta(days=1)  # no data anywhere near this
    df = pd.DataFrame([_bar(SESSION_OPEN + dt.timedelta(minutes=i)) for i in range(-2, 3)])

    # Explicit required offsets matching exactly what's provided for the
    # "covered" release (see test_validation_market_phase1.py for the
    # full explicit-offset mechanism this replaces "N bars nearby" with).
    report = validate_market_bars(
        df, "QQQ", *year_range(2020),
        macro_release_timestamps_utc=[covered_release, missing_release],
        macro_required_offsets_minutes=[-2, -1, 0, 1, 2],
    )
    assert report.macro_windows_checked == 2
    assert missing_release.isoformat() in report.macro_windows_missing_coverage
    assert covered_release.isoformat() not in report.macro_windows_missing_coverage
    assert not report.is_clean


def test_macro_window_requires_every_offset_not_just_one_bar():
    """Regression: 'one nearby bar is insufficient' -- a release window
    with exactly ONE bar (at the release minute) must still be flagged
    as missing coverage when additional offsets are required."""
    df = pd.DataFrame([_bar(SESSION_OPEN)])  # exactly one bar, right at the release
    report = validate_market_bars(
        df, "QQQ", *year_range(2020),
        macro_release_timestamps_utc=[SESSION_OPEN],
        macro_required_offsets_minutes=[-1, 0, 1, 2, 5],
    )
    assert SESSION_OPEN.isoformat() in report.macro_windows_missing_coverage
    assert set(report.macro_windows_missing_offsets[SESSION_OPEN.isoformat()]) == {-1, 1, 2, 5}


def test_macro_window_ignores_releases_outside_report_range():
    df = pd.DataFrame([_bar(SESSION_OPEN + dt.timedelta(minutes=i)) for i in range(5)])
    release_in_2021 = dt.datetime(2021, 1, 5, 13, 30, tzinfo=dt.timezone.utc)
    report = validate_market_bars(df, "QQQ", *year_range(2020), macro_release_timestamps_utc=[release_in_2021])
    assert report.macro_windows_checked == 0
    assert report.macro_windows_missing_coverage == []  # excluded, not flagged, since it's out of range


def test_report_to_dict_is_json_serializable():
    import json

    df = pd.DataFrame([_bar(SESSION_OPEN)])
    report = validate_market_bars(df, "QQQ", *year_range(2020))
    json.dumps(report.to_dict())  # must not raise


# -- regression: session checks scoped to the requested interval, not the whole year (item #5) --

def test_session_checks_scoped_to_requested_interval_only():
    """A 10-day window must only report NYSE sessions within those 10
    days as missing -- NOT every other session in the calendar year."""
    df = pd.DataFrame([_bar(SESSION_OPEN)])  # only Jan 2 has data
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 10))
    # Jan 1 2020 is a holiday (no session); Jan 2 is covered; the rest of
    # the window's sessions (Jan 3, 6, 7, 8, 9, 10) are genuinely missing
    # -- but nothing from February through December should appear here.
    assert report.expected_trading_sessions < 10
    assert all(d.startswith("2020-01") for d in report.missing_sessions)
    assert "2020-01-02" not in report.missing_sessions


def test_window_with_no_trading_sessions_at_all_has_zero_expected():
    """A window entirely on a weekend has zero expected sessions -- this
    is the signal fetch/massive.py uses to distinguish a verified-empty
    response from a suspicious one (see its own tests)."""
    report = validate_market_bars(
        pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"]),
        "QQQ", dt.date(2020, 1, 4), dt.date(2020, 1, 5),  # Sat/Sun
    )
    assert report.expected_trading_sessions == 0
    assert report.missing_sessions == []
