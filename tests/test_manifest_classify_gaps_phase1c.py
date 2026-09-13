"""Phase 1C regression tests (fourth review): `Manifest.classify_gaps()`
used to classify an entire unverified sub-range as "failed" the moment
ANY overlapping manifest entry -- however old, however narrow, however
long since superseded -- had status "failed", even when a newer,
wider attempt had since successfully re-covered (as "provisional" or
"complete") the very same dates. Concretely: Sept 1-10 fails; Sept 1-11
is later fetched successfully (provisional, since it touches "today")
-- but classify_gaps kept reporting the whole thing "failed" because
the old Sept 1-10 failed entry still overlapped the unverified gap.

The fix uses the NEWEST (by `retrieved_at`) entry covering each date to
decide that date's classification, splitting a gap into sub-intervals
where recency changes the verdict, and verifies a provisional entry's
own backing artifact before trusting it (a provisional record whose
file has since been deleted must not be treated as good evidence).

The first two tests exercise the actual bootstrap/update CLI
orchestration (`fetch_historical_data.main()` / `update_data.main()`)
against real tmp_path storage/manifests with a mocked Massive
transport -- not just the internal `classify_gaps` helper -- per the
fourth review's requirement.
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import AppConfig
from src.data.fetch import massive as massive_fetch
from src.data.manifest import Manifest, ManifestEntry, checksum_file
from tests._market_fixtures import full_session_bars
import fetch_historical_data as bootstrap
import update_data as update


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


def test_repaired_wider_provisional_range_supersedes_older_failed_subrange(isolated, monkeypatch):
    """The exact named scenario: a narrower window fails; a wider window
    covering (and extending past) it later succeeds as provisional. The
    old failure must no longer block the run."""
    today = dt.date.today()
    wide_start = today - dt.timedelta(days=6)
    narrow_end = today - dt.timedelta(days=1)
    wide_end = today
    isolated._raw["historical"]["start_date"] = wide_start.isoformat()  # noqa: SLF001

    monkeypatch.setattr(
        massive_fetch, "fetch_window_bars",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("simulated failure")),
    )
    code = bootstrap.main(["--sources", "massive", "--start", wide_start.isoformat(), "--end", narrow_end.isoformat()])
    assert code != 0

    key = massive_fetch.cache_key("QQQ", "1min", False)
    manifest = Manifest(isolated.manifest_path)
    assert manifest.get("massive", key, wide_start.isoformat(), narrow_end.isoformat()).status == "failed"

    monkeypatch.setattr(
        massive_fetch, "fetch_window_bars",
        lambda cfg, symbol, s, e, *a, **k: full_session_bars(s, e),
    )
    code = update.main(["--sources", "massive", "--end", wide_end.isoformat()])
    assert code == 0  # THE regression: must not be blocked by the now-superseded old failure

    manifest = Manifest(isolated.manifest_path)
    wide_entry = manifest.get("massive", key, wide_start.isoformat(), wide_end.isoformat())
    assert wide_entry.status == "provisional"

    gaps = manifest.classify_gaps("massive", key, wide_start, wide_end, today=wide_end)
    assert all(g.reason != "failed" for g in gaps), gaps


def test_partial_repair_splits_gap_preserving_still_failed_prefix(isolated, monkeypatch):
    """A wider failure is only PARTIALLY repaired by a later, narrower
    successful attempt covering just its tail (and extending beyond it).
    The still-untouched prefix of the original failure must remain
    reported as "failed"; only the actually-repaired suffix should
    become "provisional" -- classify_gaps must split the gap, not treat
    the whole thing as one verdict either way."""
    today = dt.date.today()
    wide_fail_start = today - dt.timedelta(days=8)
    wide_fail_end = today - dt.timedelta(days=3)
    narrow_repair_start = today - dt.timedelta(days=4)  # inside the failed range
    wide_end = today

    isolated._raw["historical"]["start_date"] = wide_fail_start.isoformat()  # noqa: SLF001

    monkeypatch.setattr(
        massive_fetch, "fetch_window_bars",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("simulated failure")),
    )
    code = bootstrap.main(["--sources", "massive", "--start", wide_fail_start.isoformat(), "--end", wide_fail_end.isoformat()])
    assert code != 0

    key = massive_fetch.cache_key("QQQ", "1min", False)
    manifest = Manifest(isolated.manifest_path)
    assert manifest.get("massive", key, wide_fail_start.isoformat(), wide_fail_end.isoformat()).status == "failed"

    # Only request the NARROWER tail-through-today range this time --
    # deliberately "forgetting" the earlier prefix by moving the
    # configured dataset start forward, so this run's own success does
    # not touch (and cannot itself repair) wide_fail_start..narrow_repair_start-1.
    isolated._raw["historical"]["start_date"] = narrow_repair_start.isoformat()  # noqa: SLF001
    monkeypatch.setattr(
        massive_fetch, "fetch_window_bars",
        lambda cfg, symbol, s, e, *a, **k: full_session_bars(s, e),
    )
    code = update.main(["--sources", "massive", "--end", wide_end.isoformat()])
    assert code == 0

    manifest = Manifest(isolated.manifest_path)
    repaired_entry = manifest.get("massive", key, narrow_repair_start.isoformat(), wide_end.isoformat())
    assert repaired_entry.status == "provisional"

    gaps = manifest.classify_gaps("massive", key, wide_fail_start, wide_end, today=wide_end)
    by_reason = {g.reason: (g.start, g.end) for g in gaps}
    assert by_reason.get("failed") == (wide_fail_start, narrow_repair_start - dt.timedelta(days=1)), gaps
    assert by_reason.get("provisional") == (narrow_repair_start, wide_end), gaps


def test_classify_gaps_treats_provisional_entry_with_missing_artifact_as_failed(tmp_path):
    """A provisional entry's status alone is not sufficient evidence --
    if its backing artifact has since been deleted or corrupted, that
    "successful acquisition" claim can no longer be trusted and must be
    treated as a real failure, not silently passed through as
    "provisional"."""
    manifest = Manifest(tmp_path / "manifest.json")
    artifact = tmp_path / "2020.parquet"
    artifact.write_bytes(b"pretend-parquet-bytes")
    checksum = checksum_file(artifact)
    manifest.record(ManifestEntry(
        provider="massive", key="QQQ:1min:raw", start="2020-09-01", end="2020-09-10",
        status="provisional", checksum=checksum, path=str(artifact),
    ))

    artifact.unlink()  # the shared artifact is gone

    gaps = manifest.classify_gaps(
        "massive", "QQQ:1min:raw", dt.date(2020, 9, 1), dt.date(2020, 9, 10), today=dt.date(2020, 9, 10),
    )
    assert len(gaps) == 1
    assert gaps[0].reason == "failed"
