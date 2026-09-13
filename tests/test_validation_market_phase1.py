"""Phase 1A regression tests (third review): explicit session-completeness
and macro-window-offset validation. Written against the TARGET API before
the fix -- run once to record failing/erroring results, then again after
the fix to confirm they pass.
"""
import datetime as dt

import pandas as pd
import pytest

from src.data.validation.market import validate_market_bars, nyse_sessions, year_range

SESSION_OPEN = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)  # 9:30am ET, first 2020 session
SESSION_CLOSE = dt.datetime(2020, 1, 2, 21, 0, tzinfo=dt.timezone.utc)  # 4:00pm ET
FAR_FUTURE_AS_OF = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)  # "we are validating well after the fact"


def _bar(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=1000):
    return {"timestamp_utc": ts, "open": o, "high": h, "low": l, "close": c, "volume": v, "vwap": o, "transactions": 1}


def _full_session_bars(open_utc: dt.datetime, close_utc: dt.datetime):
    """Every 1-minute bar in [open_utc, close_utc)."""
    bars = []
    t = open_utc
    while t < close_utc:
        bars.append(_bar(t))
        t += dt.timedelta(minutes=1)
    return bars


def test_full_valid_session_passes():
    df = pd.DataFrame(_full_session_bars(SESSION_OPEN, SESSION_CLOSE))
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF)
    assert report.is_clean
    assert report.incomplete_sessions == []
    assert report.missing_sessions == []


def test_one_bar_session_fails_completeness():
    """Reproduces the exact bug: one bar for an entire session must NOT be clean."""
    df = pd.DataFrame([_bar(SESSION_OPEN)])
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF)
    assert report.is_clean is False
    assert report.is_hard_failure is True
    assert "2020-01-02" in report.incomplete_sessions
    assert "2020-01-02" not in report.missing_sessions  # it has SOME data -- distinct category from zero data


def test_missing_opening_minute_detected():
    bars = _full_session_bars(SESSION_OPEN + dt.timedelta(minutes=1), SESSION_CLOSE)  # skip minute 0
    df = pd.DataFrame(bars)
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF)
    assert "2020-01-02" in report.sessions_missing_open_edge


def test_missing_closing_minute_detected():
    bars = _full_session_bars(SESSION_OPEN, SESSION_CLOSE - dt.timedelta(minutes=1))  # skip last minute
    df = pd.DataFrame(bars)
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF)
    assert "2020-01-02" in report.sessions_missing_close_edge


def test_missing_release_minute_fails_despite_nearby_bars():
    """Reproduces: 3 nearby bars is not the requirement -- the release
    minute itself (offset 0) must be present."""
    release = SESSION_OPEN  # 8:30am ET equivalent test instant, doesn't need to be a real macro time
    bars = [
        _bar(release - dt.timedelta(minutes=3)),
        _bar(release - dt.timedelta(minutes=2)),
        _bar(release + dt.timedelta(minutes=4)),
    ]
    df = pd.DataFrame(bars)
    report = validate_market_bars(
        df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF,
        macro_release_timestamps_utc=[release], macro_required_offsets_minutes=[0],
    )
    assert release.isoformat() in report.macro_windows_missing_coverage
    assert 0 in report.macro_windows_missing_offsets[release.isoformat()]


def test_release_minute_present_satisfies_requirement():
    release = SESSION_OPEN
    bars = [_bar(release)]
    df = pd.DataFrame(bars)
    report = validate_market_bars(
        df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF,
        macro_release_timestamps_utc=[release], macro_required_offsets_minutes=[0],
    )
    assert release.isoformat() not in report.macro_windows_missing_coverage


def test_multi_offset_requirement_all_must_be_present():
    release = SESSION_OPEN
    # Only offset 0 present; -1 and +1 missing.
    df = pd.DataFrame([_bar(release)])
    report = validate_market_bars(
        df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF,
        macro_release_timestamps_utc=[release], macro_required_offsets_minutes=[-1, 0, 1],
    )
    assert release.isoformat() in report.macro_windows_missing_coverage
    assert set(report.macro_windows_missing_offsets[release.isoformat()]) == {-1, 1}


def test_early_close_uses_correct_expected_timestamps():
    sessions = nyse_sessions(dt.date(2024, 7, 3), dt.date(2024, 7, 3))
    open_utc, close_utc = sessions.iloc[0]["market_open"].to_pydatetime(), sessions.iloc[0]["market_close"].to_pydatetime()
    df = pd.DataFrame(_full_session_bars(open_utc, close_utc))
    report = validate_market_bars(df, "QQQ", dt.date(2024, 7, 3), dt.date(2024, 7, 3), as_of=dt.datetime(2024, 8, 1, tzinfo=dt.timezone.utc))
    assert report.is_clean
    assert "2024-07-03" not in report.incomplete_sessions


def test_holiday_produces_zero_expected_sessions_not_missing():
    df = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"])
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 1), as_of=FAR_FUTURE_AS_OF)  # New Year's Day
    assert report.expected_trading_sessions == 0
    assert report.missing_sessions == []
    assert report.incomplete_sessions == []


def test_current_in_progress_session_is_provisional_not_incomplete():
    """Premarket/current-session updates: only elapsed minutes are
    expected. The not-yet-happened remainder must not be flagged missing
    or incomplete."""
    as_of = SESSION_OPEN + dt.timedelta(minutes=30)  # we are 30 minutes into the session
    bars = _full_session_bars(SESSION_OPEN, as_of)  # exactly the elapsed portion, fully present
    df = pd.DataFrame(bars)
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=as_of)
    assert "2020-01-02" in report.provisional_sessions
    assert "2020-01-02" not in report.missing_sessions
    assert "2020-01-02" not in report.incomplete_sessions
    assert report.is_hard_failure is False


def test_future_session_not_counted_as_expected_at_all():
    """Before a session starts, its missing bars are not a historical
    collection failure -- the session shouldn't even be "expected" yet."""
    as_of = SESSION_OPEN - dt.timedelta(hours=1)  # one hour before market open
    df = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"])
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=as_of)
    assert report.expected_trading_sessions == 0
    assert report.missing_sessions == []
    assert report.incomplete_sessions == []
    assert report.provisional_sessions == []


def test_completeness_ratio_field_reported():
    df = pd.DataFrame([_bar(SESSION_OPEN)])
    report = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF)
    # 1 bar out of 390 expected minutes -- must be reported as a small ratio, not silently omitted.
    assert report.regular_hours_coverage_ratio < 0.01


def test_configurable_completeness_threshold():
    """A session missing just 1 of 390 minutes should NOT fail at a
    lenient threshold, but SHOULD fail at a strict one -- proving the
    threshold is real and not hardcoded to only catch near-total absence."""
    bars = _full_session_bars(SESSION_OPEN, SESSION_CLOSE - dt.timedelta(minutes=1))
    df = pd.DataFrame(bars)
    lenient = validate_market_bars(
        df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF,
        min_session_completeness_ratio=0.5,
    )
    strict = validate_market_bars(
        df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), as_of=FAR_FUTURE_AS_OF,
        min_session_completeness_ratio=0.999,
    )
    assert "2020-01-02" not in lenient.incomplete_sessions
    assert "2020-01-02" in strict.incomplete_sessions
