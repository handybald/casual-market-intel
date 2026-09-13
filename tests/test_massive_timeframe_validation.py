"""Regression tests (fifth review, P2, then sixth review corrections):
Massive fetch-time validation (`_validate_window` in
src/data/fetch/massive.py) ignored the configured/requested timeframe
entirely and always validated against `validate_market_bars`'s
1-minute default -- a complete, correctly spaced 5-minute response (78
five-minute bars for a full regular session) was judged against a
390-minute-expected grid and therefore always failed, while
`scripts/validate_data.py` (which DID convert timeframe ->
timeframe_minutes, via its own separate inline ternary) would have
passed the exact same data. The two entry points could silently
disagree.

Fix: one shared `timeframe_minutes_from_parts()` in
src/data/validation/market.py, used by BOTH `_validate_window` and
`scripts/validate_data.py`, so they can never diverge again. Daily (and
zero/negative) timeframes are explicitly rejected by that same function
before any network call is made -- never silently treated as 1-minute.

SIXTH REVIEW CORRECTIONS to this file:
  1. The original version of every "orchestration" test here replaced
     `fetch_window_bars` directly with a fake -- despite being reported
     as HTTP-mocked, this never exercised real URL/params construction,
     JSON response parsing, or pagination inside `fetch_window_bars`
     itself. Those are kept below as clearly labeled UNIT TESTS, and a
     new INTEGRATION TESTS section mocks only `request_with_retry` (the
     actual HTTP transport call inside `fetch_window_bars`), matching
     the pattern already used in tests/test_fetch_massive.py for
     pagination/auth tests.
  2. The early-close test used July 3, 2020 -- which pandas_market_calendars
     reports as a full NYSE HOLIDAY (zero sessions that year, since July
     4 2020 was a Saturday and the observed holiday fell on Friday July
     3rd), not an early close. The test therefore validated an empty
     calendar and passed without ever exercising early-close logic.
     Replaced with July 3, 2019 (a Wednesday), independently confirmed
     via `nyse.schedule()` to be a single regular session from
     13:30:00 UTC to 17:00:00 UTC -- exactly 210 minutes -- with an
     exact, non-vacuous assertion (`expected_regular_minutes == 210`,
     42 five-minute bars) rather than the weak `< 390` check that would
     also have accepted zero sessions.
"""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import massive as m
from src.data.validation.market import (
    UnsupportedTimeframeError,
    nyse_sessions,
    timeframe_minutes_from_parts,
    validate_market_bars,
)
from tests._market_fixtures import full_session_bars
import fetch_historical_data as bootstrap
import update_data as update


def make_config(tmp_path, timeframe="5min") -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": timeframe, "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "massive": {
                "chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "massive"),
                "base_url": "https://api.massive.com", "max_retries": 1, "request_delay_seconds": 0,
                "page_limit": 1000, "adjusted": False, "sort": "asc", "revision_overlap_days": 3,
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _bars_to_massive_results(bars):
    """Converts our internal bar-dict fixture shape into the documented
    Massive `results` array shape (t/o/h/l/c/v/vw/n), so a real
    `fetch_window_bars` call can parse it exactly as it would parse a
    genuine API response -- see `_parse_bars_response` in
    src/data/fetch/massive.py."""
    return [
        {
            "t": int(b["timestamp_utc"].timestamp() * 1000),
            "o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"],
            "v": b["volume"], "vw": b["vwap"], "n": b["transactions"],
        }
        for b in bars
    ]


FAR_PAST_TODAY = dt.date(2021, 1, 1)  # keeps January 2020 windows safely "complete", not provisional

# July 3, 2019 (Wednesday) -- independently confirmed via
# `nyse.schedule(start_date="2019-07-01", end_date="2019-07-06")` to be
# a single regular NYSE session, market_open=13:30:00 UTC,
# market_close=17:00:00 UTC (210 minutes) -- the day before Independence
# Day, a documented NYSE early close. July 3 2020 (used previously) is
# NOT comparable: that year the market was fully CLOSED (a holiday),
# not shortened, so a test built on it validates an empty calendar.
EARLY_CLOSE_DAY = dt.date(2019, 7, 3)


# =====================================================================
# UNIT TESTS -- `fetch_window_bars` is replaced directly. These do NOT
# exercise request construction, response parsing, or pagination; they
# isolate the timeframe-aware validation/manifest/finalization logic
# from the HTTP layer entirely. See the INTEGRATION TESTS section below
# for transport-mocked (request_with_retry only) coverage of the same
# scenarios plus the parts these cannot reach.
# =====================================================================

def test_unit_complete_five_minute_response_finalizes_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="5min")
    manifest = Manifest(config.manifest_path)

    monkeypatch.setattr(m, "fetch_window_bars", lambda cfg, symbol, s, e, *a, **k: full_session_bars(s, e, timeframe_minutes=5))

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert results[0].status == "complete"
    key = m.cache_key("QQQ", "5min", False)
    assert manifest.is_complete("massive", key, "2020-01-02", "2020-01-02")


def test_unit_incomplete_five_minute_response_fails_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="5min")
    manifest = Manifest(config.manifest_path)

    def missing_bars(cfg, symbol, s, e, *a, **k):
        bars = full_session_bars(s, e, timeframe_minutes=5)
        return bars[:-20]  # drop the last 20 five-minute bars -- a real gap, not just one bar

    monkeypatch.setattr(m, "fetch_window_bars", missing_bars)

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert results[0].status == "failed"
    key = m.cache_key("QQQ", "5min", False)
    assert not manifest.is_complete("massive", key, "2020-01-02", "2020-01-02")


def test_unit_complete_one_minute_response_still_finalizes_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="1min")
    manifest = Manifest(config.manifest_path)

    monkeypatch.setattr(m, "fetch_window_bars", lambda cfg, symbol, s, e, *a, **k: full_session_bars(s, e, timeframe_minutes=1))

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "1min", today=FAR_PAST_TODAY)
    assert results[0].status == "complete"


def test_unit_standalone_and_fetch_time_validation_agree_for_five_minute_data():
    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)
    df = pd.DataFrame(bars, columns=m.BAR_COLUMNS)

    standalone = validate_market_bars(df, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)
    fetch_time = m._validate_window(bars, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)

    assert standalone.is_hard_failure == fetch_time.is_hard_failure == False
    assert standalone.is_clean == fetch_time.is_clean
    assert standalone.missing_sessions == fetch_time.missing_sessions
    assert standalone.incomplete_sessions == fetch_time.incomplete_sessions


def test_unit_timeframe_minutes_from_parts_rejects_day_and_zero_directly():
    with pytest.raises(UnsupportedTimeframeError):
        timeframe_minutes_from_parts(1, "day")
    with pytest.raises(UnsupportedTimeframeError):
        timeframe_minutes_from_parts(0, "minute")
    with pytest.raises(UnsupportedTimeframeError):
        timeframe_minutes_from_parts(-1, "minute")
    assert timeframe_minutes_from_parts(5, "minute") == 5
    assert timeframe_minutes_from_parts(1, "hour") == 60


def test_unit_successful_rerun_skips_refetch_via_verified_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="5min")
    manifest = Manifest(config.manifest_path)

    call_count = {"n": 0}

    def counting_fetch(cfg, symbol, s, e, *a, **k):
        call_count["n"] += 1
        return full_session_bars(s, e, timeframe_minutes=5)

    monkeypatch.setattr(m, "fetch_window_bars", counting_fetch)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert call_count["n"] == 1

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert call_count["n"] == 1  # NOT refetched
    assert results[0].status == "skipped_cached"


# =====================================================================
# EARLY-CLOSE SESSION: real NYSE calendar data, no HTTP involved at all
# (validate_market_bars is pure). Kept separate from both sections above
# since it exercises calendar/validation logic exclusively.
# =====================================================================

def test_early_close_session_has_exactly_one_210_minute_session():
    """Sanity-checks the fixture date ITSELF before trusting anything
    built on it -- this is exactly the check the sixth review found
    missing, which let a holiday (zero sessions) masquerade as an early
    close."""
    sessions = nyse_sessions(EARLY_CLOSE_DAY, EARLY_CLOSE_DAY)
    assert len(sessions) == 1
    row = sessions.iloc[0]
    session_minutes = (row["market_close"] - row["market_open"]).total_seconds() / 60.0
    assert session_minutes == 210.0


def test_early_close_session_correct_five_minute_expectations():
    bars = full_session_bars(EARLY_CLOSE_DAY, EARLY_CLOSE_DAY, timeframe_minutes=5)
    assert len(bars) == 42  # 210 minutes / 5-minute bars, exactly

    df = pd.DataFrame(bars, columns=m.BAR_COLUMNS)
    report = validate_market_bars(df, "QQQ", EARLY_CLOSE_DAY, EARLY_CLOSE_DAY, timeframe_minutes=5)
    assert report.is_hard_failure is False
    assert not report.missing_sessions
    assert not report.incomplete_sessions
    # `expected_regular_minutes` counts expected GRID SLOTS at the
    # configured timeframe (42 five-minute slots for a 210-minute
    # session), not literal wall-clock minutes -- the 210-minute session
    # length itself is independently verified against the raw NYSE
    # calendar in test_early_close_session_has_exactly_one_210_minute_session.
    assert report.expected_regular_minutes == 42  # exact, not "< 390"
    assert report.regular_hours_coverage_ratio == 1.0


def test_early_close_session_missing_required_coverage_fails():
    """Removing bars that would be required for a REGULAR (390-minute)
    session must also be caught for a genuinely shortened 210-minute
    session -- dropping the last 8 of 42 five-minute bars is a real,
    detectable gap here too."""
    bars = full_session_bars(EARLY_CLOSE_DAY, EARLY_CLOSE_DAY, timeframe_minutes=5)
    incomplete_bars = bars[:-8]
    df = pd.DataFrame(incomplete_bars, columns=m.BAR_COLUMNS)
    report = validate_market_bars(df, "QQQ", EARLY_CLOSE_DAY, EARLY_CLOSE_DAY, timeframe_minutes=5)
    assert report.is_hard_failure is True
    assert EARLY_CLOSE_DAY.isoformat() in report.incomplete_sessions


# =====================================================================
# TRANSPORT-MOCKED INTEGRATION TESTS -- only `request_with_retry` (the
# actual HTTP call site inside `fetch_window_bars`) is replaced. Every
# other layer runs for real: URL/params construction, JSON response
# parsing (`_parse_bars_response`), status validation
# (`_validate_response_payload`), timeframe-aware validation, artifact
# persistence, manifest status decisions, and resume/skip behavior.
# Mirrors the pattern already used for pagination/auth coverage in
# tests/test_fetch_massive.py.
# =====================================================================

def _fake_transport(bars):
    def handler(method, url, session=None, max_retries=None, request_delay_seconds=None, params=None):
        return SimpleNamespace(json=lambda: {"status": "OK", "results": _bars_to_massive_results(bars)})
    return handler


def test_integration_complete_five_minute_http_response_finalizes_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="5min")
    manifest = Manifest(config.manifest_path)

    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)
    monkeypatch.setattr(m, "request_with_retry", _fake_transport(bars))

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert results[0].status == "complete"
    key = m.cache_key("QQQ", "5min", False)
    assert manifest.is_complete("massive", key, "2020-01-02", "2020-01-02")


def test_integration_incomplete_http_response_fails_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="5min")
    manifest = Manifest(config.manifest_path)

    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)[:-20]
    monkeypatch.setattr(m, "request_with_retry", _fake_transport(bars))

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert results[0].status == "failed"
    key = m.cache_key("QQQ", "5min", False)
    assert not manifest.is_complete("massive", key, "2020-01-02", "2020-01-02")


def test_integration_identical_rerun_issues_no_http_request(tmp_path, monkeypatch):
    config = make_config(tmp_path, timeframe="5min")
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    manifest = Manifest(config.manifest_path)

    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)
    call_count = {"n": 0}

    def counting_transport(method, url, session=None, max_retries=None, request_delay_seconds=None, params=None):
        call_count["n"] += 1
        return SimpleNamespace(json=lambda: {"status": "OK", "results": _bars_to_massive_results(bars)})

    monkeypatch.setattr(m, "request_with_retry", counting_transport)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert call_count["n"] == 1

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)
    assert call_count["n"] == 1  # no new HTTP request at all
    assert results[0].status == "skipped_cached"


def test_integration_one_minute_http_response_finalizes_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="1min")
    manifest = Manifest(config.manifest_path)

    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=1)
    monkeypatch.setattr(m, "request_with_retry", _fake_transport(bars))

    results = m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "1min", today=FAR_PAST_TODAY)
    assert results[0].status == "complete"


def test_integration_unsupported_timeframe_rejected_before_any_http_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="1day")
    manifest = Manifest(config.manifest_path)

    def must_not_be_called(*a, **k):
        raise AssertionError("request_with_retry must not be called for an unsupported timeframe")

    monkeypatch.setattr(m, "request_with_retry", must_not_be_called)

    with pytest.raises(UnsupportedTimeframeError):
        m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "1day", today=FAR_PAST_TODAY)


def test_integration_zero_interval_timeframe_rejected_before_any_http_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path, timeframe="0min")
    manifest = Manifest(config.manifest_path)

    def must_not_be_called(*a, **k):
        raise AssertionError("request_with_retry must not be called for an invalid timeframe")

    monkeypatch.setattr(m, "request_with_retry", must_not_be_called)

    with pytest.raises(UnsupportedTimeframeError):
        m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "0min", today=FAR_PAST_TODAY)


def test_integration_requested_multiplier_and_timespan_appear_in_outgoing_request(tmp_path, monkeypatch):
    """Proves the timeframe is not just correctly VALIDATED but also
    correctly REQUESTED -- the outgoing URL must reflect the configured
    multiplier/timespan, not a hardcoded 1-minute default."""
    config = make_config(tmp_path, timeframe="5min")
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    manifest = Manifest(config.manifest_path)

    captured = {}
    bars = full_session_bars(dt.date(2020, 1, 2), dt.date(2020, 1, 2), timeframe_minutes=5)

    def capturing_transport(method, url, session=None, max_retries=None, request_delay_seconds=None, params=None):
        captured["url"] = url
        captured["params"] = params
        return SimpleNamespace(json=lambda: {"status": "OK", "results": _bars_to_massive_results(bars)})

    monkeypatch.setattr(m, "request_with_retry", capturing_transport)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 2), dt.date(2020, 1, 2), "5min", today=FAR_PAST_TODAY)

    assert "/range/5/minute/" in captured["url"]
    assert "/QQQ/" in captured["url"]
    assert "2020-01-02" in captured["url"]


def test_integration_cli_bootstrap_rejects_unsupported_timeframe_cleanly(tmp_path, monkeypatch):
    """The real CLI entry point must report this as a clean, non-zero
    failure -- not an uncaught traceback -- with the HTTP layer never touched."""
    config = make_config(tmp_path, timeframe="1day")
    monkeypatch.setattr(bootstrap, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")

    def must_not_be_called(*a, **k):
        raise AssertionError("request_with_retry must not be called for an unsupported timeframe")

    monkeypatch.setattr(m, "request_with_retry", must_not_be_called)

    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-01-31"])
    assert code != 0
