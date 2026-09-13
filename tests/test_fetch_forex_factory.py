import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import forex_factory as ff_fetch

GOOD_HTML = (Path(__file__).parent / "fixtures" / "forex_factory_sample.html").read_text(encoding="utf-8")
ZERO_ROW_HTML = (Path(__file__).parent / "fixtures" / "forex_factory_zero_rows.html").read_text(encoding="utf-8")
BLOCKED_HTML = (
    "<html><body><title>Economic Calendar</title>"
    + ("Please solve this captcha to continue accessing the calendar. " * 50)
    + "</body></html>"
)


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
            "forex_factory": {
                "raw_dir": str(tmp_path / "raw" / "forex_factory"),
                "base_url": "https://www.forexfactory.com/calendar",
                "max_retries": 1,
                "request_delay_seconds": 0,
                "display_timezone": "America/New_York",
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _mock_response(monkeypatch, html: str):
    def fake_request_with_retry(*args, **kwargs):
        return SimpleNamespace(text=html)

    monkeypatch.setattr(ff_fetch, "request_with_retry", fake_request_with_retry)


def test_zero_row_page_is_rejected_not_checkpointed(tmp_path, monkeypatch):
    _mock_response(monkeypatch, ZERO_ROW_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1))

    assert result.status == "failed"
    assert result.parsed_rows == 0
    assert not manifest.is_complete("forex_factory", ff_fetch.MANIFEST_KEY, "2024-01-01", "2024-01-31")
    # Raw response is still preserved for diagnosis, even though it failed.
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == ZERO_ROW_HTML


def test_bot_block_page_is_rejected(tmp_path, monkeypatch):
    _mock_response(monkeypatch, BLOCKED_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1))
    assert result.status == "failed"
    assert "captcha" in result.error.lower() or "bot-check" in result.error.lower()


def test_good_page_is_recorded_complete_with_real_row_count(tmp_path, monkeypatch):
    _mock_response(monkeypatch, GOOD_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    # Requested month is in the past relative to `today` -> not provisional.
    result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1))

    assert result.status == "complete"
    assert result.parsed_rows == 5
    entry = manifest.get("forex_factory", ff_fetch.MANIFEST_KEY, "2024-01-01", "2024-01-31")
    assert entry.rows == 5  # real parsed count, never a 0 placeholder
    assert manifest.is_complete("forex_factory", ff_fetch.MANIFEST_KEY, "2024-01-01", "2024-01-31")


def test_current_month_is_provisional_and_always_retried(tmp_path, monkeypatch):
    _mock_response(monkeypatch, GOOD_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    today = dt.date(2024, 1, 15)  # inside the requested month

    result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=today)
    assert result.status == "provisional"
    assert not manifest.is_complete("forex_factory", ff_fetch.MANIFEST_KEY, "2024-01-01", "2024-01-31")

    # A second run (still the "current" month) must re-fetch, not skip.
    calls = {"count": 0}

    def counting_request(*args, **kwargs):
        calls["count"] += 1
        return SimpleNamespace(text=GOOD_HTML)

    monkeypatch.setattr(ff_fetch, "request_with_retry", counting_request)
    ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=today)
    assert calls["count"] == 1  # actually called again, not skipped as "cached"


def test_future_month_is_skipped_by_orchestrator_without_manifest_entry(tmp_path, monkeypatch):
    _mock_response(monkeypatch, GOOD_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    today = dt.date(2024, 1, 15)

    results = ff_fetch.fetch_forex_factory(
        config, manifest, dt.date(2024, 1, 1), dt.date(2024, 3, 31), today=today
    )
    statuses = {(r.year, r.month): r.status for r in results}
    assert statuses[(2024, 2)] == "skipped_future"
    assert statuses[(2024, 3)] == "skipped_future"
    assert manifest.entries_for("forex_factory") == [
        e for e in manifest.entries_for("forex_factory") if e.start.startswith("2024-01")
    ]


def test_month_identity_mismatch_is_rejected(tmp_path, monkeypatch):
    _mock_response(monkeypatch, GOOD_HTML)  # fixture rows are all January 2024
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    # Ask for a month the fixture's rows don't actually belong to.
    result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 6, today=dt.date(2024, 9, 1))
    assert result.status == "failed"
    assert "identity mismatch" in result.error


# -- raw immutability / provenance (second review item #8) --

def test_failed_retry_does_not_overwrite_the_last_good_attempt(tmp_path, monkeypatch):
    """Regression: a prior version wrote every attempt to ONE fixed
    filename per month, so a failed retry destroyed the last good page.
    Each attempt must be its own immutable file."""
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    _mock_response(monkeypatch, GOOD_HTML)
    good_result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1), force=True)
    assert good_result.status == "complete"
    assert good_result.path.read_text(encoding="utf-8") == GOOD_HTML

    _mock_response(monkeypatch, ZERO_ROW_HTML)
    bad_result = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1), force=True)
    assert bad_result.status == "failed"

    # The good attempt's file is untouched -- a different file entirely.
    assert good_result.path != bad_result.path
    assert good_result.path.exists()
    assert good_result.path.read_text(encoding="utf-8") == GOOD_HTML

    # And the "currently trusted" artifact still points at the good one.
    raw_dir = config.provider_raw_dir("forex_factory")
    trusted = ff_fetch.latest_verified_forex_factory_path(raw_dir, 2024, 1)
    assert trusted == good_result.path


def test_skipped_cached_month_reports_original_acquisition_time_not_now(tmp_path, monkeypatch):
    """Regression: normalizing a cached (already-downloaded) month must
    use the time it was ACTUALLY fetched, not the current wall-clock
    time of this later run."""
    _mock_response(monkeypatch, GOOD_HTML)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    first = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 3, 1))
    assert first.status == "complete"
    original_retrieved_at = first.retrieved_at
    assert original_retrieved_at is not None

    # A "later" run (force=False, not current month) just skips -- but
    # must still report the ORIGINAL acquisition time, not "now".
    second = ff_fetch.fetch_forex_factory_month(config, manifest, 2024, 1, today=dt.date(2024, 6, 1))
    assert second.status == "skipped_cached"
    assert second.retrieved_at == original_retrieved_at
