import datetime as dt
import json
from types import SimpleNamespace

import pytest

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import fred as fred_fetch


def make_config(tmp_path, **overrides) -> AppConfig:
    fred_cfg = {
        "raw_dir": str(tmp_path / "raw" / "fred"),
        "base_url": "https://api.stlouisfed.org/fred",
        "max_retries": 1,
        "lookback_buffer_days": 400,
        "official_series": [
            {"series_id": "CPIAUCSL", "event_family": "CPI_MOM", "transform": "pct_change_1", "result_unit": "PERCENT"},
        ],
    }
    fred_cfg.update(overrides)
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
        "providers": {"fred": fred_cfg},
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _obs(date, value):
    return {"realtime_start": date, "realtime_end": date, "date": date, "value": str(value) if value is not None else "."}


def test_missing_api_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    with pytest.raises(fred_fetch.MissingCredentialsError):
        fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))


def test_fetch_writes_immutable_timestamped_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_request(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 3, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0), _obs("2020-02-01", 1.1), _obs("2020-03-01", 1.2)],
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", fake_request)
    path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    assert path.exists()
    entry = manifest.get("fred", "CPIAUCSL", "2020-01-01", "2020-12-31")
    assert entry.status == "complete"
    assert entry.rows == 3


def test_fetch_paginates_using_count_offset_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    all_obs = [_obs(f"2020-{m:02d}-01", float(m)) for m in range(1, 13)]

    def fake_request(method, url, session=None, max_retries=None, params=None, **kwargs):
        offset = int(params["offset"])
        limit = 5
        page = all_obs[offset:offset + limit]
        return SimpleNamespace(json=lambda: {"count": len(all_obs), "offset": offset, "limit": limit, "observations": page})

    monkeypatch.setattr(fred_fetch, "request_with_retry", fake_request)
    path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["observations"]) == 12


def test_malformed_response_missing_observations_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def fake_request(*args, **kwargs):
        return SimpleNamespace(json=lambda: {"error_message": "bad request"})

    monkeypatch.setattr(fred_fetch, "request_with_retry", fake_request)
    with pytest.raises(fred_fetch.FredResponseError):
        fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))


# -- regression: empty delta must not erase/regress history (item #4) --

def test_empty_response_after_prior_success_does_not_erase_history(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def good_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 3, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0), _obs("2020-02-01", 1.1), _obs("2020-03-01", 1.2)],
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", good_response)
    first_path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))
    assert manifest.get("fred", "CPIAUCSL", "2020-01-01", "2020-12-31").status == "complete"

    def empty_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {"count": 0, "offset": 0, "limit": 100000, "observations": []})

    monkeypatch.setattr(fred_fetch, "request_with_retry", empty_response)
    second_path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    # The empty attempt is recorded FAILED, not complete/empty -- it must
    # not be treated as valid coverage.
    entry = manifest.get("fred", "CPIAUCSL", "2020-01-01", "2020-12-31")
    assert entry.status == "failed"

    # The first (good) snapshot file is untouched and still has 3 rows.
    first_payload = json.loads(first_path.read_text(encoding="utf-8"))
    assert len(first_payload["observations"]) == 3

    # The "latest verified" snapshot must still be the GOOD one, not the empty one.
    latest = fred_fetch.latest_verified_snapshot_path(config, "CPIAUCSL")
    assert latest == first_path
    assert second_path != first_path  # empty attempt got its own separate file (preserved for diagnosis)


def test_late_released_and_revised_observations_reflected_in_new_snapshot(tmp_path, monkeypatch):
    """A later fetch can both (a) see a period that wasn't released yet at
    the time of the first fetch, and (b) see a revised value for an
    already-published period -- both must show up via the new snapshot,
    proving the full-refetch strategy actually captures them."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def first_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 1, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0)],  # Feb not released yet
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", first_response)
    fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    def second_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 2, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.05), _obs("2020-02-01", 1.1)],  # Jan revised, Feb now present
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", second_response)
    fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    latest = fred_fetch.latest_verified_snapshot_path(config, "CPIAUCSL")
    payload = json.loads(latest.read_text(encoding="utf-8"))
    values = {o["date"]: o["value"] for o in payload["observations"]}
    assert values["2020-01-01"] == "1.05"  # revision visible
    assert values["2020-02-01"] == "1.1"   # late-released observation visible


# -- ALFRED as-of vintage retrieval (second review item #6) --

def test_fetch_observations_as_of_uses_matching_realtime_start_and_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    captured = {}

    def fake_request(method, url, session=None, max_retries=None, params=None, **kwargs):
        captured["params"] = params
        return SimpleNamespace(json=lambda: {"observations": [_obs("2020-01-01", 0.99)]})

    monkeypatch.setattr(fred_fetch, "request_with_retry", fake_request)

    as_of = dt.date(2020, 2, 10)
    path = fred_fetch.fetch_observations_as_of(
        config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 1, 1), as_of,
    )

    assert captured["params"]["realtime_start"] == "2020-02-10"
    assert captured["params"]["realtime_end"] == "2020-02-10"
    assert path.exists()
    assert "asof" in str(path)


def test_fetch_observations_as_of_writes_a_distinct_manifest_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    monkeypatch.setattr(fred_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(
        json=lambda: {"observations": [_obs("2020-01-01", 0.99)]}
    ))

    as_of = dt.date(2020, 2, 10)
    fred_fetch.fetch_observations_as_of(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 1, 1), as_of)

    # Distinct from the default full-refresh key ("CPIAUCSL") -- never
    # collides with or is mistaken for that checkpoint.
    entry = manifest.get("fred", "CPIAUCSL:asof:2020-02-10", "2020-01-01", "2020-01-01")
    assert entry is not None
    assert entry.status == "complete"
    assert manifest.get("fred", "CPIAUCSL", "2020-01-01", "2020-01-01") is None


def test_fetch_observations_as_of_malformed_response_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(fred_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(json=lambda: {"error_message": "bad"}))

    with pytest.raises(fred_fetch.FredResponseError):
        fred_fetch.fetch_observations_as_of(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 1, 1), dt.date(2020, 2, 10))


# -- item #7: reject shortened (not just totally empty) snapshots --

def test_shortened_snapshot_is_rejected_not_selected_as_latest(tmp_path, monkeypatch):
    """Reproduces the exact bug: a 2-observation snapshot followed by a
    1-observation response for the SAME (or overlapping) range must be
    rejected -- not silently accepted as the new latest-verified
    snapshot just because it's non-empty."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def two_obs_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 2, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0), _obs("2020-02-01", 1.1)],
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", two_obs_response)
    first_path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    def one_obs_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 1, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0)],  # missing 2020-02-01 this time
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", one_obs_response)
    fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    entry = manifest.get("fred", "CPIAUCSL", "2020-01-01", "2020-12-31")
    assert entry.status == "failed"
    assert "missing" in entry.error

    # The shortened snapshot must NOT have become the latest verified one.
    latest = fred_fetch.latest_verified_snapshot_path(config, "CPIAUCSL")
    assert latest == first_path


def test_narrower_follow_up_request_is_not_flagged_as_a_regression(tmp_path, monkeypatch):
    """A deliberately smaller/non-overlapping follow-up request returning
    fewer rows is legitimate, not a regression -- must NOT be rejected."""
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    def two_obs_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 2, "offset": 0, "limit": 100000,
            "observations": [_obs("2020-01-01", 1.0), _obs("2020-02-01", 1.1)],
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", two_obs_response)
    fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    def narrower_response(*args, **kwargs):
        return SimpleNamespace(json=lambda: {
            "count": 1, "offset": 0, "limit": 100000,
            "observations": [_obs("2025-06-01", 3.2)],  # a genuinely later, non-overlapping period
        })

    monkeypatch.setattr(fred_fetch, "request_with_retry", narrower_response)
    fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2025, 6, 1), dt.date(2025, 6, 1))

    entry = manifest.get("fred", "CPIAUCSL", "2025-06-01", "2025-06-01")
    assert entry.status == "complete"


def test_pagination_raises_on_early_empty_page_before_count_satisfied(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    # First page under-delivers relative to `count` (only 5 of a
    # declared-larger total), forcing a second (empty) page request.
    def under_delivering(method, url, session=None, max_retries=None, params=None, **kwargs):
        offset = int(params["offset"])
        if offset == 0:
            return SimpleNamespace(json=lambda: {"count": 10, "offset": 0, "limit": 100000, "observations": [_obs(f"2020-0{i}-01", 1.0) for i in range(1, 6)]})
        return SimpleNamespace(json=lambda: {"count": 10, "offset": offset, "limit": 100000, "observations": []})

    monkeypatch.setattr(fred_fetch, "request_with_retry", under_delivering)
    with pytest.raises(fred_fetch.FredResponseError, match="empty page"):
        fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))


def test_latest_verified_snapshot_path_rejects_checksum_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(fred_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(json=lambda: {
        "count": 1, "offset": 0, "limit": 100000, "observations": [_obs("2020-01-01", 1.0)],
    }))

    path = fred_fetch.fetch_fred_series_snapshot(config, manifest, "CPIAUCSL", dt.date(2020, 1, 1), dt.date(2020, 12, 31))
    path.write_text("corrupted after the fact", encoding="utf-8")

    assert fred_fetch.latest_verified_snapshot_path(config, "CPIAUCSL") is None
