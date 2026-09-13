import datetime as dt
from types import SimpleNamespace

import pytest

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import massive as m
from src.data.validation.market import nyse_sessions
from tests._market_fixtures import full_session_bars

# Validation now requires genuine session completeness (see
# src/data/validation/market.py) -- a single bar per session is no
# longer sufficient fixture data to reach "complete" status.
_one_bar_per_session = full_session_bars


def make_config(tmp_path, **overrides) -> AppConfig:
    massive_cfg = {
        "chunk_frequency": "month",
        "raw_dir": str(tmp_path / "raw" / "massive"),
        "base_url": "https://api.massive.com",
        "max_retries": 1,
        "request_delay_seconds": 0,
        "page_limit": 1000,
        "adjusted": False,
        "sort": "asc",
        "revision_overlap_days": 3,
    }
    massive_cfg.update(overrides)
    raw = {
        "historical": {"start_date": "2016-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {"massive": massive_cfg},
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _bar(ts_ms, o=1.0, h=1.5, l=0.5, c=1.2, v=1000, vw=1.1, n=5):
    return {"t": ts_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw, "n": n}


def test_parse_timeframe():
    assert m.parse_timeframe("1min") == (1, "minute")
    assert m.parse_timeframe("5min") == (5, "minute")
    assert m.parse_timeframe("1h") == (1, "hour")
    assert m.parse_timeframe("1day") == (1, "day")
    assert m.parse_timeframe("1d") == (1, "day")
    with pytest.raises(ValueError):
        m.parse_timeframe("weird")


def test_cache_key_includes_timeframe_and_adjustment():
    assert m.cache_key("QQQ", "1min", False) == "QQQ:1min:raw"
    assert m.cache_key("QQQ", "1min", True) == "QQQ:1min:adjusted"
    assert m.cache_key("QQQ", "5min", False) != m.cache_key("QQQ", "1min", False)


def test_validate_response_payload_missing_status_raises():
    with pytest.raises(m.MassiveResponseError):
        m._validate_response_payload({"results": []})


def test_validate_response_payload_error_status_raises():
    with pytest.raises(m.MassiveResponseError):
        m._validate_response_payload({"status": "ERROR", "error": "boom"})


def test_validate_response_payload_ok_and_delayed_pass():
    m._validate_response_payload({"status": "OK"})
    m._validate_response_payload({"status": "DELAYED"})


def test_fetch_window_bars_follows_next_url_pagination_preserving_auth(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    page1_url = "https://api.massive.com/v2/aggs/ticker/QQQ/range/1/minute/2020-01-01/2020-01-01"
    page2_url = "https://api.massive.com/v2/aggs/next?cursor=abc"

    calls = []

    def fake_request(method, url, session=None, max_retries=None, request_delay_seconds=None, params=None, **kwargs):
        calls.append((url, params))
        if url == page1_url:
            return SimpleNamespace(json=lambda: {
                "status": "OK", "results": [_bar(1577880600000)], "next_url": page2_url,
            })
        elif url == page2_url:
            return SimpleNamespace(json=lambda: {"status": "OK", "results": [_bar(1577880660000)]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(m, "request_with_retry", fake_request)

    bars = m.fetch_window_bars(
        config, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 1), 1, "minute", False,
        session=SimpleNamespace(), api_key="secret-key",
    )
    assert len(bars) == 2
    # apiKey must be present on BOTH requests (documented next_url omits it).
    assert calls[0][1]["apiKey"] == "secret-key"
    assert calls[1][1]["apiKey"] == "secret-key"


def test_fetch_window_bars_detects_repeated_pagination_link(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    loop_url = "https://api.massive.com/v2/aggs/next?cursor=stuck"

    def fake_request(method, url, session=None, max_retries=None, request_delay_seconds=None, params=None, **kwargs):
        return SimpleNamespace(json=lambda: {"status": "OK", "results": [_bar(1577880600000)], "next_url": loop_url})

    monkeypatch.setattr(m, "request_with_retry", fake_request)

    with pytest.raises(m.MassiveResponseError, match="repeated pagination"):
        m.fetch_window_bars(
            config, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 1), 1, "minute", False,
            session=SimpleNamespace(), api_key="secret-key",
        )


def test_fetch_window_bars_malformed_response_raises_not_empty_list(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def fake_request(*args, **kwargs):
        return SimpleNamespace(json=lambda: {"resultsCount": 0})  # no "status" at all

    monkeypatch.setattr(m, "request_with_retry", fake_request)

    with pytest.raises(m.MassiveResponseError):
        m.fetch_window_bars(
            config, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 1), 1, "minute", False,
            session=SimpleNamespace(), api_key="secret-key",
        )


def test_missing_api_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    with pytest.raises(m.MissingCredentialsError):
        m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min")


# -- clipped windows / provisional vs complete (regression: Sept 1-10 bug) --

def test_partial_month_request_checkpoints_only_requested_days_not_full_month(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    captured = {}

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        captured["start"] = start
        captured["end"] = end
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)

    # `today` far enough past Sept 10 that the window is NOT provisional.
    m.fetch_massive_symbol(
        config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), "1min",
        today=dt.date(2021, 1, 1),
    )

    assert captured["start"] == dt.date(2020, 9, 1)
    assert captured["end"] == dt.date(2020, 9, 10)  # NOT Sept 30 -- the original bug

    key = m.cache_key("QQQ", "1min", False)
    entry = manifest.get("massive", key, "2020-09-01", "2020-09-10")
    assert entry is not None
    assert entry.status == "complete"
    # No entry should claim coverage through Sept 30.
    assert manifest.get("massive", key, "2020-09-01", "2020-09-30") is None


def test_recent_window_is_provisional_and_not_skipped_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    call_count = {"n": 0}

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        call_count["n"] += 1
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)

    today = dt.date(2020, 9, 10)  # `today` == chunk end -> provisional
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), "1min", today=today)
    key = m.cache_key("QQQ", "1min", False)
    entry = manifest.get("massive", key, "2020-09-01", "2020-09-10")
    assert entry.status == "provisional"
    assert not manifest.is_complete("massive", key, "2020-09-01", "2020-09-10")

    # Rerun on the same day (still provisional) must fetch again, not skip.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 9, 1), dt.date(2020, 9, 10), "1min", today=today)
    assert call_count["n"] == 2


# -- durability: per-chunk persistence, checksum propagation (item #9) --

def test_durable_per_chunk_write_survives_a_later_chunk_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        if start.month == 1:
            return _one_bar_per_session(start, end)
        raise RuntimeError("simulated network failure in February")

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)

    m.fetch_massive_symbol(
        config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1)
    )

    key = m.cache_key("QQQ", "1min", False)
    jan_entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    feb_entry = manifest.get("massive", key, "2020-02-01", "2020-02-29")
    assert jan_entry.status == "complete"
    assert feb_entry.status == "failed"
    # January's artifact is durably on disk, independent of February failing.
    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    assert year_path.exists()
    import pandas as pd
    df = pd.read_parquet(year_path)
    assert len(df) == len(full_session_bars(dt.date(2020, 1, 1), dt.date(2020, 1, 31)))


def test_checksum_propagates_to_sibling_entries_sharing_year_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)

    m.fetch_massive_symbol(
        config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1)
    )

    key = m.cache_key("QQQ", "1min", False)
    jan_entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    feb_entry = manifest.get("massive", key, "2020-02-01", "2020-02-29")
    # Both months share one year file -> after February's write, January's
    # checksum must have been refreshed to match the CURRENT file content.
    assert jan_entry.checksum == feb_entry.checksum
    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")
    assert manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")


# -- regression: shared-artifact recovery must not silently bless a
# sibling checkpoint whose rows are gone (second-review item #1) --

def test_deleted_shared_file_full_rerun_refetches_every_month_not_just_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    fetch_calls = []

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        fetch_calls.append((start, end))
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)
    key = m.cache_key("QQQ", "1min", False)

    # Bootstrap: January + February into the same yearly parquet.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1))
    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")
    assert manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")

    # Delete the shared parquet entirely.
    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    year_path.unlink()

    # Rerun the SAME full range.
    fetch_calls.clear()
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1))

    # Both months must have actually been re-fetched -- not just January
    # with February silently "recovered" via a blessed checksum.
    assert (dt.date(2020, 1, 1), dt.date(2020, 1, 31)) in fetch_calls
    assert (dt.date(2020, 2, 1), dt.date(2020, 2, 29)) in fetch_calls
    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")
    assert manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")

    import pandas as pd

    df = pd.read_parquet(year_path)
    assert len(df) == len(full_session_bars(dt.date(2020, 1, 1), dt.date(2020, 2, 29)))  # every session fully present


def test_deleted_shared_file_partial_rerun_leaves_untouched_month_as_gap_not_blessed(tmp_path, monkeypatch):
    """Reproduces the exact bug: bootstrap Jan+Feb, delete the shared
    file, rerun for January ONLY. February must end up as a reported gap
    (or at least NOT `is_complete`) -- never silently marked recovered
    just because January's rewrite produced a checksum that used to
    match February's entry too."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)
    key = m.cache_key("QQQ", "1min", False)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1))

    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    year_path.unlink()

    # Rerun for January ONLY -- February is out of scope for this call.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=dt.date(2021, 1, 1))

    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")
    # THE CORE ASSERTION: February must NOT be treated as complete/covered.
    assert not manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")

    feb_entry = manifest.get("massive", key, "2020-02-01", "2020-02-29")
    assert feb_entry.status == "failed"

    gaps = manifest.coverage_gaps("massive", key, dt.date(2020, 1, 1), dt.date(2020, 2, 29))
    assert (dt.date(2020, 2, 1), dt.date(2020, 2, 29)) in gaps  # the manifest must REPORT this gap


def test_corrupted_shared_file_invalidates_siblings_before_rewrite(tmp_path, monkeypatch):
    """Same bug, but via silent corruption (checksum mismatch) rather
    than outright deletion -- must be caught the same way."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(m, "fetch_window_bars", fake_fetch_window)
    key = m.cache_key("QQQ", "1min", False)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=dt.date(2021, 1, 1))

    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    year_path.write_bytes(b"corrupted garbage, not valid parquet")

    # Rerun for January ONLY.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=dt.date(2021, 1, 1))

    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")
    assert not manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")
    assert manifest.get("massive", key, "2020-02-01", "2020-02-29").status == "failed"


# -- regression: enforce validation before finalization (second review item #5) --

def test_empty_response_for_a_normal_trading_month_is_failed_not_empty(tmp_path, monkeypatch):
    """Reproduces the exact bug: a zero-bar response for a window that
    genuinely contains NYSE trading sessions must NOT be accepted as
    verified-empty."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    monkeypatch.setattr(m, "fetch_window_bars", lambda *a, **k: [])

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=dt.date(2021, 1, 1))

    key = m.cache_key("QQQ", "1min", False)
    entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert entry.status == "failed"
    assert "0 bars" in entry.error


def test_empty_response_for_a_genuinely_non_trading_window_is_verified_empty(tmp_path, monkeypatch):
    """A window with literally zero NYSE sessions (e.g. a weekend) IS
    legitimately empty -- this must still be accepted as "empty"."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    monkeypatch.setattr(m, "fetch_window_bars", lambda *a, **k: [])

    # Jan 4-5, 2020 is a Sat/Sun -- zero NYSE sessions.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 4), dt.date(2020, 1, 5), "1min", today=dt.date(2021, 1, 1))

    key = m.cache_key("QQQ", "1min", False)
    entry = manifest.get("massive", key, "2020-01-04", "2020-01-05")
    assert entry.status == "empty"


def test_hard_validation_failure_prevents_finalization_but_preserves_data(tmp_path, monkeypatch):
    """Invalid OHLCV in a fetched response must not be finalized as
    "complete" -- but the raw (bad) data is still written to disk for
    diagnosis, not discarded."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def bad_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        bars = _one_bar_per_session(start, end)
        bars[0]["high"] = -1.0  # invalid: negative price
        return bars

    monkeypatch.setattr(m, "fetch_window_bars", bad_fetch_window)
    key = m.cache_key("QQQ", "1min", False)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=dt.date(2021, 1, 1))

    entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert entry.status == "failed"
    assert "validation hard failure" in entry.error

    # Data is preserved on disk despite not being finalized.
    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    assert year_path.exists()
    import pandas as pd

    df = pd.read_parquet(year_path)
    assert len(df) == len(full_session_bars(dt.date(2020, 1, 1), dt.date(2020, 1, 31)))


def test_validation_report_is_persisted_durably(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(m, "fetch_window_bars", lambda cfg, symbol, start, end, *a, **k: _one_bar_per_session(start, end))

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=dt.date(2021, 1, 1))

    report_path = m._validation_report_dir(config) / "massive_QQQ_2020-01-01_2020-01-31.json"
    assert report_path.exists()
    import json

    payload = json.loads(report_path.read_text())
    assert payload["symbol"] == "QQQ"
    assert payload["is_hard_failure"] is False
