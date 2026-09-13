"""Phase 3 regression test (fourth review): the FRED ALFRED "as of"
historical-vintage workflow (`fetch_observations_as_of` /
`normalize_fred_asof_events`) existed only as internal helper functions
-- nothing wired it into an actual runnable command, so using it
required hand-writing a script every time. `scripts/fetch_fred_asof.py`
is the documented CLI entry point that makes it usable directly:

    python scripts/fetch_fred_asof.py --event-family CPI_MOM \
        --observation-start 2024-01-01 --observation-end 2024-01-31 \
        --as-of 2024-02-05

This test drives the actual `main()` CLI function (not just the
internal fetch/normalize helpers) with a mocked HTTP transport, proving
the command actually fetches, normalizes, and merges an AS_OF snapshot
into data/interim/macro/fred_events.parquet end to end.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
from src.data.fetch import fred as fred_fetch
from src.data.normalize.io import read_events

import fetch_fred_asof


def make_config(tmp_path) -> AppConfig:
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
        "providers": {
            "fred": {
                "raw_dir": str(tmp_path / "raw" / "fred"),
                "base_url": "https://api.stlouisfed.org/fred",
                "max_retries": 1,
                "lookback_buffer_days": 400,
                "official_series": [
                    {"series_id": "CPIAUCSL", "event_family": "CPI_MOM", "transform": "pct_change_1", "result_unit": "PERCENT"},
                ],
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _obs(date, value):
    return {"realtime_start": date, "realtime_end": date, "date": date, "value": str(value)}


def test_fetch_fred_asof_cli_end_to_end(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(fetch_fred_asof, "load_config", lambda: config)
    monkeypatch.setattr(fetch_fred_asof, "load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    # pct_change_1 needs at least 2 consecutive observations to produce 1 event.
    monkeypatch.setattr(
        fred_fetch, "request_with_retry",
        lambda *a, **k: SimpleNamespace(json=lambda: {
            "observations": [_obs("2023-12-01", 300.0), _obs("2024-01-01", 301.0)],
        }),
    )

    code = fetch_fred_asof.main([
        "--event-family", "CPI_MOM",
        "--observation-start", "2023-12-01",
        "--observation-end", "2024-01-31",
        "--as-of", "2024-02-05",
    ])
    assert code == 0

    events_path = config.interim_root / "macro" / "fred_events.parquet"
    events = read_events(events_path)
    assert len(events) == 1
    assert events[0].official_vintage_kind == "AS_OF"
    import datetime as dt
    assert events[0].official_vintage_date == dt.date(2024, 2, 5)


def test_fetch_fred_asof_cli_unknown_event_family_fails_cleanly(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(fetch_fred_asof, "load_config", lambda: config)
    monkeypatch.setattr(fetch_fred_asof, "load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    code = fetch_fred_asof.main([
        "--event-family", "TOTALLY_UNCONFIGURED_FAMILY",
        "--observation-start", "2024-01-01",
        "--observation-end", "2024-01-31",
        "--as-of", "2024-02-05",
    ])
    assert code != 0


def test_fetch_fred_asof_cli_missing_credentials_fails_cleanly(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(fetch_fred_asof, "load_config", lambda: config)
    monkeypatch.setattr(fetch_fred_asof, "load_dotenv_if_present", lambda: None)
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    code = fetch_fred_asof.main([
        "--event-family", "CPI_MOM",
        "--observation-start", "2024-01-01",
        "--observation-end", "2024-01-31",
        "--as-of", "2024-02-05",
    ])
    assert code != 0
