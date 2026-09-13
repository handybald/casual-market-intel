"""Phase 1B regression tests (fourth review): `scripts/validate_data.py`
used to validate every stored year-parquet against the FULL Jan1-Dec31
calendar year regardless of what was actually requested/fetched for
that (symbol, timeframe, adjustment) key. A dataset that only ever
fetched a partial year (e.g. Sept 1 - Oct 5) would spuriously report
every other month's NYSE sessions as "missing" -- a false hard failure
having nothing to do with real data quality.

These tests exercise the actual `validate_data.main()` CLI entry point
(not just the internal `validate_market` helper) against real, durable
tmp_path storage/manifests produced by the real Massive fetch pipeline
(mocked transport only), per the fourth review's "test the actual
CLI/main function" requirement.
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_data as validate_script  # noqa: E402  (path inserted above)

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import massive as m
from tests._market_fixtures import full_session_bars


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
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


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(validate_script, "load_config", lambda: config)
    # `main()` hard-codes REPO_ROOT/data/manifests/validation_reports as
    # its report directory -- redirect it into tmp_path so this test
    # never writes into the real repository tree.
    monkeypatch.setattr(validate_script, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    return config


def _bootstrap_massive_range(config, start: dt.date, end: dt.date, today: dt.date) -> None:
    manifest = Manifest(config.manifest_path)

    def fake_fetch_window(cfg, symbol, s, e, multiplier, timespan, adjusted, session, api_key):
        return full_session_bars(s, e)

    import unittest.mock as mock

    with mock.patch.object(m, "fetch_window_bars", fake_fetch_window):
        m.fetch_massive_symbol(config, manifest, "QQQ", start, end, "1min", today=today)


def test_partial_past_year_dataset_does_not_spuriously_fail(isolated):
    """Regression: only Sept 1 - Oct 5, 2020 was ever fetched (a fully
    elapsed range relative to the real 'now' this test runs under).
    Validating against the full Jan1-Dec31, 2020 calendar year -- as the
    unfixed script does -- reports Jan-Aug and Nov-Dec as entirely
    missing NYSE sessions, a false hard failure. The fixed script must
    derive its validated range from the manifest's actually-requested
    coverage and exit 0."""
    config = isolated
    _bootstrap_massive_range(config, dt.date(2020, 9, 1), dt.date(2020, 10, 5), today=dt.date(2020, 10, 5))

    code = validate_script.main()
    assert code == 0


def test_partial_current_year_dataset_does_not_spuriously_fail(isolated):
    """Same bug, but for the current year: only a recent, fully-elapsed
    slice of THIS year was ever fetched. Months before it (since
    Jan 1 of the current year) and after it (up to year end) were never
    requested and must not be flagged as missing sessions."""
    config = isolated
    today = dt.date.today()
    fetch_start = today - dt.timedelta(days=20)
    fetch_end = today - dt.timedelta(days=10)
    _bootstrap_massive_range(config, fetch_start, fetch_end, today=fetch_end)

    code = validate_script.main()
    assert code == 0


def test_market_reports_distinct_per_symbol_timeframe_adjustment(isolated):
    """Regression: reports were written to `market_{symbol}_{year}.json`
    only -- two different (timeframe, adjustment) combinations for the
    same symbol/year silently overwrote each other's report, and the
    printed summary didn't distinguish them either."""
    config = isolated
    raw_root = config.provider_raw_dir("massive")

    bars = full_session_bars(dt.date(2020, 9, 1), dt.date(2020, 9, 4))
    df = pd.DataFrame(bars)

    raw_dir = raw_root / "QQQ" / "1min" / "raw"
    adjusted_dir = raw_root / "QQQ" / "1min" / "adjusted"
    raw_dir.mkdir(parents=True)
    adjusted_dir.mkdir(parents=True)
    df.to_parquet(raw_dir / "2020.parquet", index=False)
    df.to_parquet(adjusted_dir / "2020.parquet", index=False)

    manifest = Manifest(config.manifest_path)
    for adjusted in (False, True):
        key = m.cache_key("QQQ", "1min", adjusted)
        manifest.record(
            __import__("src.data.manifest", fromlist=["ManifestEntry"]).ManifestEntry(
                provider="massive", key=key, start="2020-09-01", end="2020-09-04",
                status="complete", rows=len(bars),
            )
        )

    report_dir = validate_script.REPO_ROOT / "data" / "manifests" / "validation_reports"
    any_hard_failure = validate_script.validate_market(config, [], report_dir)
    assert any_hard_failure is False

    report_files = sorted(p.name for p in report_dir.glob("market_QQQ*"))
    assert len(report_files) == 2, f"expected two distinct reports (raw + adjusted), got {report_files}"
