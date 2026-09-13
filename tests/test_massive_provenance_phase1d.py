"""Phase 1D regression tests (fourth review): fetching February used to
rewrite January's manifest `retrieved_at` even though January itself was
never refetched. `_reverify_and_refresh_siblings()` in
src/data/fetch/massive.py reconciles every sibling checkpoint sharing
the rewritten year-parquet's checksum after a new month is merged in --
but it built a brand-new `ManifestEntry()` without carrying over the
original entry's `retrieved_at` (a dataclass field that defaults to
`now()`), so a pure checksum-only reconciliation silently overwrote
January's real acquisition timestamp with "the moment February was
fetched" -- provenance fiction.

Named acceptance sequence: fetch Jan -> record provenance -> fetch Feb
-> normalize -> confirm Jan provenance unchanged -> delete shared
artifact -> recover both months -> verify accurate recovery provenance
and no missing rows.
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import massive as m
from src.data.normalize.market import normalize_market_symbol
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
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setattr(m, "fetch_window_bars", lambda cfg, symbol, s, e, *a, **k: full_session_bars(s, e))
    return config


# "today" far enough past Jan/Feb 2020 that both months finalize
# "complete" (not "provisional") -- isolates the provenance bug from
# provisional/finalization semantics entirely.
FAR_FUTURE_TODAY = dt.date(2021, 1, 1)


def test_fetching_february_does_not_change_januarys_retrieved_at(isolated):
    config = isolated
    manifest = Manifest(config.manifest_path)
    key = m.cache_key("QQQ", "1min", False)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=FAR_FUTURE_TODAY)
    jan_before = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert jan_before.status == "complete"
    jan_retrieved_at_before = jan_before.retrieved_at

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 2, 1), dt.date(2020, 2, 29), "1min", today=FAR_FUTURE_TODAY)
    feb_entry = manifest.get("massive", key, "2020-02-01", "2020-02-29")
    assert feb_entry.status == "complete"

    jan_after = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert jan_after.status == "complete"  # not invalidated -- its rows are still present
    assert jan_after.retrieved_at == jan_retrieved_at_before, (
        "January's acquisition timestamp must not change just because February's "
        "fetch caused the shared year-file's checksum to be reconciled"
    )

    # normalize (as the real CLI pipeline does) must not disturb this either.
    normalize_market_symbol(config, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), manifest=manifest)
    jan_after_normalize = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    assert jan_after_normalize.retrieved_at == jan_retrieved_at_before


def test_shared_artifact_deletion_then_recovery_has_accurate_provenance_and_no_missing_rows(isolated):
    config = isolated
    manifest = Manifest(config.manifest_path)
    key = m.cache_key("QQQ", "1min", False)

    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 1, 31), "1min", today=FAR_FUTURE_TODAY)
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 2, 1), dt.date(2020, 2, 29), "1min", today=FAR_FUTURE_TODAY)

    year_path = m._year_parquet_path(config, "QQQ", "1min", False, 2020)
    year_path.unlink()  # simulate the shared artifact being lost

    before_recovery = dt.datetime.now(dt.timezone.utc).isoformat()

    # Recover both months in one call, as a real bootstrap/update rerun would.
    m.fetch_massive_symbol(config, manifest, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 2, 29), "1min", today=FAR_FUTURE_TODAY)

    manifest = Manifest(config.manifest_path)
    jan_entry = manifest.get("massive", key, "2020-01-01", "2020-01-31")
    feb_entry = manifest.get("massive", key, "2020-02-01", "2020-02-29")
    assert jan_entry.status == "complete"
    assert feb_entry.status == "complete"
    # Both months were genuinely re-fetched by THIS recovery run -- their
    # provenance must reflect that (not be silently preserved from before
    # the artifact was even lost).
    assert jan_entry.retrieved_at >= before_recovery
    assert feb_entry.retrieved_at >= before_recovery

    df = pd.read_parquet(year_path)
    expected = full_session_bars(dt.date(2020, 1, 1), dt.date(2020, 2, 29))
    assert len(df) == len(expected)
    assert df["timestamp_utc"].nunique() == len(expected)
