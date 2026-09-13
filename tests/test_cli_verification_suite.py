"""The explicit CLI-level verification suite requested in the second
review: every scenario here drives `bootstrap.main()` / `update.main()`
directly (the actual `python scripts/fetch_historical_data.py` /
`python scripts/update_data.py` entry points), not just internal fetch
functions -- so a regression in the CLI wiring itself (argument parsing,
which functions get called, how exit codes are computed) would be
caught here even if the underlying fetch-layer unit tests still pass.

Covers: (1) bootstrap, (2) interrupted collection, (3) resume,
(4) shared-file recovery, (5) successful provisional refresh through
today, (6) failed historical-gap repair, (7) validation-driven failure
exit codes.
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
from src.data.fetch import massive as massive_fetch
from tests._market_fixtures import full_session_bars
import fetch_historical_data as bootstrap
import update_data as update


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "mql5": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "mql5"),
                      "input_csv": str(tmp_path / "raw" / "mql5" / "us_macro_calendar.csv"), "broker_timezone": None},
            "forex_factory": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "forex_factory"),
                               "base_url": "https://www.forexfactory.com/calendar", "max_retries": 1,
                               "request_delay_seconds": 0, "display_timezone": "America/New_York",
                               "display_timezone_verified": False},
            "massive": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "massive"),
                        "base_url": "https://api.massive.com", "max_retries": 1, "request_delay_seconds": 0,
                        "page_limit": 1000, "adjusted": False, "sort": "asc", "revision_overlap_days": 3},
            "fred": {"raw_dir": str(tmp_path / "raw" / "fred"), "base_url": "https://api.stlouisfed.org/fred",
                     "max_retries": 1, "lookback_buffer_days": 400, "official_series": []},
            "bls": {"enabled": False, "raw_dir": str(tmp_path / "raw" / "bls"),
                    "base_url": "https://api.bls.gov/publicAPI/v2", "max_retries": 1,
                    "max_years_per_request": 10, "series": []},
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(bootstrap, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "load_dotenv_if_present", lambda: None)
    monkeypatch.setattr(update, "load_config", lambda: config)
    monkeypatch.setattr(update, "load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    return config


_one_bar_per_session = full_session_bars  # legacy name; now genuinely full-density (see tests/_market_fixtures.py)


# -- (1) bootstrap, (2) interrupted collection, (3) resume --

def test_1_2_3_bootstrap_interrupted_then_resumed(isolated, monkeypatch):
    calls = {"n": 0}

    def flaky_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        calls["n"] += 1
        if start.month == 2:
            raise ConnectionError("simulated interruption fetching February")
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", flaky_fetch_window)

    # (1) Bootstrap, (2) interrupted mid-way (February fails).
    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-02-29"])
    assert code != 0

    year_path = massive_fetch._year_parquet_path(isolated, "QQQ", "1min", False, 2020)
    assert year_path.exists()
    jan_only = pd.read_parquet(year_path)
    assert (jan_only["timestamp_utc"].dt.month == 1).all()  # January survived the interruption

    # (3) Resume: rerun the SAME command, February now succeeds.
    def recovered_fetch_window(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", recovered_fetch_window)
    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-02-29"])
    assert code == 0

    both_months = pd.read_parquet(year_path)
    assert set(both_months["timestamp_utc"].dt.month.unique()) == {1, 2}


# -- (4) shared-file recovery --

def test_4_shared_file_recovery_via_cli(isolated, monkeypatch):
    monkeypatch.setattr(massive_fetch, "fetch_window_bars", lambda cfg, symbol, start, end, *a, **k: _one_bar_per_session(start, end))

    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-02-29"])
    assert code == 0

    year_path = massive_fetch._year_parquet_path(isolated, "QQQ", "1min", False, 2020)
    year_path.unlink()  # simulate the shared artifact being lost

    # Rerun for January ONLY. This run legitimately discovers and
    # invalidates February's now-unverifiable checkpoint too (the shared
    # file was gone, so BOTH prior checkpoints stop being trustworthy --
    # see Manifest.invalidate_entries_for_missing_or_corrupt_path) --
    # that invalidation is itself a real, truthfully-reported failure
    # for this run, not a bug to paper over.
    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-01-31"])
    assert code != 0

    key = massive_fetch.cache_key("QQQ", "1min", False)
    from src.data.manifest import Manifest

    manifest = Manifest(isolated.manifest_path)
    assert manifest.is_complete("massive", key, "2020-01-01", "2020-01-31")  # January itself IS fine
    assert not manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")  # NOT silently blessed

    # And February must show up as an unresolved gap on the next update,
    # which must repair it (not skip it as if it were still complete).
    code = update.main(["--sources", "massive", "--end", "2020-12-31"])
    assert code == 0
    manifest = Manifest(isolated.manifest_path)
    assert manifest.is_complete("massive", key, "2020-02-01", "2020-02-29")


# -- (5) successful provisional refresh through today --
# (already covered end-to-end in test_cli.py::
#  test_update_successful_provisional_refresh_through_today_exits_zero;
#  included here too for a single consolidated suite location)

def test_5_successful_provisional_refresh_through_today(isolated, monkeypatch):
    today = dt.date.today()
    monkeypatch.setattr(massive_fetch, "fetch_window_bars", lambda cfg, symbol, start, end, *a, **k: _one_bar_per_session(start, end))
    isolated._raw["historical"]["start_date"] = (today - dt.timedelta(days=10)).isoformat()  # noqa: SLF001

    code = update.main(["--sources", "massive", "--end", today.isoformat()])
    assert code == 0


# -- (6) failed historical-gap repair --

def test_6_failed_historical_gap_repair(isolated, monkeypatch):
    def fail_july(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        if start.month == 7:
            raise ConnectionError("simulated failure")
        return _one_bar_per_session(start, end)

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", fail_july)
    code = bootstrap.main(["--sources", "massive", "--start", "2020-06-01", "--end", "2020-08-31"])
    assert code != 0

    key = massive_fetch.cache_key("QQQ", "1min", False)
    from src.data.manifest import Manifest

    manifest = Manifest(isolated.manifest_path)
    assert manifest.get("massive", key, "2020-07-01", "2020-07-31").status == "failed"
    assert manifest.get("massive", key, "2020-08-01", "2020-08-31").status == "complete"

    # An update (which spans the full configured historical range through
    # today) must repair July even though August already succeeded.
    monkeypatch.setattr(massive_fetch, "fetch_window_bars", lambda cfg, symbol, start, end, *a, **k: _one_bar_per_session(start, end))
    isolated._raw["historical"]["start_date"] = "2020-06-01"  # noqa: SLF001
    code = update.main(["--sources", "massive", "--end", "2020-08-31"])
    assert code == 0

    manifest = Manifest(isolated.manifest_path)
    assert manifest.get("massive", key, "2020-07-01", "2020-07-31").status == "complete"


# -- (7) validation-driven failure exit codes --

def test_7_validation_driven_failure_exit_code(isolated, monkeypatch):
    def bad_ohlcv(cfg, symbol, start, end, multiplier, timespan, adjusted, session, api_key):
        bars = _one_bar_per_session(start, end)
        bars[0]["high"] = -5.0  # invalid: negative price
        return bars

    monkeypatch.setattr(massive_fetch, "fetch_window_bars", bad_ohlcv)
    code = bootstrap.main(["--sources", "massive", "--start", "2020-01-01", "--end", "2020-01-31"])
    assert code != 0

    key = massive_fetch.cache_key("QQQ", "1min", False)
    from src.data.manifest import Manifest

    manifest = Manifest(isolated.manifest_path)
    entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert entry.status == "failed"
    assert "validation hard failure" in entry.error
