import datetime as dt
from types import SimpleNamespace

import pytest

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch import bls as bls_fetch


def make_config(tmp_path, **overrides) -> AppConfig:
    bls_cfg = {
        "enabled": True,
        "raw_dir": str(tmp_path / "raw" / "bls"),
        "base_url": "https://api.bls.gov/publicAPI/v2",
        "max_retries": 1,
        "max_years_per_request": 10,
        "series": ["CUUR0000SA0"],
    }
    bls_cfg.update(overrides)
    raw = {
        "historical": {"start_date": "2016-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {"bls": bls_cfg},
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _bls_response(status="REQUEST_SUCCEEDED", series_id="CUUR0000SA0", data=None):
    return {
        "status": status,
        "Results": {"series": [{"seriesID": series_id, "data": data or []}]},
    }


def test_year_windows_splits_into_max_year_chunks():
    assert bls_fetch.year_windows(2010, 2025, 10) == [(2010, 2019), (2020, 2025)]
    assert bls_fetch.year_windows(2020, 2020, 10) == [(2020, 2020)]


def test_year_windows_invalid_range_raises():
    with pytest.raises(ValueError):
        bls_fetch.year_windows(2025, 2020, 10)


def test_fetch_series_window_writes_snapshot_and_manifest(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)

    data = [{"year": "2020", "period": "M01", "periodName": "January", "value": "257.9"}]
    monkeypatch.setattr(bls_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(json=lambda: _bls_response(data=data)))

    path = bls_fetch.fetch_bls_series_window(config, manifest, "CUUR0000SA0", 2016, 2020)
    assert path.exists()
    entry = manifest.get("bls", "CUUR0000SA0", "2016-01-01", "2020-12-31")
    assert entry.status == "complete"
    assert entry.rows == 1


def test_fetch_series_window_sends_post_with_correct_body(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    captured = {}

    def fake_request(method, url, session=None, max_retries=None, json=None, headers=None, **kwargs):
        captured["method"] = method
        captured["json"] = json
        return SimpleNamespace(json=lambda: _bls_response())

    monkeypatch.setattr(bls_fetch, "request_with_retry", fake_request)
    bls_fetch.fetch_bls_series_window(config, manifest, "CUUR0000SA0", 2016, 2020)

    assert captured["method"] == "POST"
    assert captured["json"]["seriesid"] == ["CUUR0000SA0"]
    assert captured["json"]["startyear"] == "2016"
    assert captured["json"]["endyear"] == "2020"
    assert "registrationkey" not in captured["json"]  # no BLS_API_KEY set


def test_fetch_series_window_includes_registration_key_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "secret-key")
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    captured = {}

    def fake_request(method, url, session=None, max_retries=None, json=None, headers=None, **kwargs):
        captured["json"] = json
        return SimpleNamespace(json=lambda: _bls_response())

    monkeypatch.setattr(bls_fetch, "request_with_retry", fake_request)
    bls_fetch.fetch_bls_series_window(config, manifest, "CUUR0000SA0", 2016, 2020)
    assert captured["json"]["registrationkey"] == "secret-key"


def test_error_status_raises_and_records_failure(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(bls_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(
        json=lambda: {"status": "REQUEST_NOT_PROCESSED", "message": ["invalid series"]}
    ))

    with pytest.raises(bls_fetch.BlsResponseError):
        bls_fetch.fetch_bls_series_window(config, manifest, "CUUR0000SA0", 2016, 2020)

    entry = manifest.get("bls", "CUUR0000SA0", "2016-01-01", "2020-12-31")
    assert entry.status == "failed"


def test_missing_series_in_results_raises(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(bls_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(
        json=lambda: {"status": "REQUEST_SUCCEEDED", "Results": {"series": []}}
    ))

    with pytest.raises(bls_fetch.BlsResponseError):
        bls_fetch.fetch_bls_series_window(config, manifest, "CUUR0000SA0", 2016, 2020)


def test_fetch_bls_series_chunks_a_wide_range_into_multiple_windows(tmp_path, monkeypatch):
    config = make_config(tmp_path, max_years_per_request=5)
    manifest = Manifest(config.manifest_path)
    calls = []

    def fake_request(method, url, session=None, max_retries=None, json=None, headers=None, **kwargs):
        calls.append((json["startyear"], json["endyear"]))
        return SimpleNamespace(json=lambda: _bls_response())

    monkeypatch.setattr(bls_fetch, "request_with_retry", fake_request)
    paths = bls_fetch.fetch_bls_series(config, manifest, "CUUR0000SA0", dt.date(2010, 1, 1), dt.date(2024, 12, 31))

    assert calls == [("2010", "2014"), ("2015", "2019"), ("2020", "2024")]
    assert len(paths) == 3


def test_parse_observations_handles_missing_value():
    payload = {"observations": [
        {"year": "2020", "period": "M01", "periodName": "January", "value": "257.9"},
        {"year": "2020", "period": "M02", "periodName": "February", "value": ""},
    ]}
    obs = bls_fetch.parse_observations(payload)
    assert obs[0].value == 257.9
    assert obs[1].value is None


def test_configured_series_ids_empty_by_default(tmp_path):
    config = make_config(tmp_path, series=[])
    assert bls_fetch.configured_series_ids(config) == []


def test_fetch_all_bls_series_uses_configured_list(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manifest = Manifest(config.manifest_path)
    monkeypatch.setattr(bls_fetch, "request_with_retry", lambda *a, **k: SimpleNamespace(json=lambda: _bls_response()))

    results = bls_fetch.fetch_all_bls_series(config, manifest, dt.date(2016, 1, 1), dt.date(2020, 12, 31))
    assert "CUUR0000SA0" in results
