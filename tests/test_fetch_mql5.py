import datetime as dt
import shutil
from pathlib import Path

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch.mql5 import ingest_mql5_calendar

FIXTURE = Path(__file__).parent / "fixtures" / "mql5_sample.csv"


def make_config(tmp_path, csv_path) -> AppConfig:
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
            "mql5": {"chunk_frequency": "month", "raw_dir": str(tmp_path / "raw" / "mql5"), "input_csv": str(csv_path)},
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_ingest_reports_covered_and_missing_months(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)  # fixture only has Jan 2024 rows
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    report = ingest_mql5_calendar(config, manifest, dt.date(2023, 12, 1), dt.date(2024, 1, 31))

    assert report.covered_months == ["2024-01"]
    assert report.missing_months == ["2023-12"]
    assert report.total_rows == 4


def test_ingest_records_manifest_entries_for_resume(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    key = "US:USD"
    assert manifest.is_complete("mql5", key, "2024-01-01", "2024-01-31")


def test_ingest_missing_csv_reports_all_months_missing(tmp_path):
    csv_path = tmp_path / "does_not_exist.csv"
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 2, 28))

    assert report.covered_months == []
    assert report.missing_months == ["2024-01", "2024-02"]
    assert len(manifest.failed_entries("mql5")) == 2


def test_ingest_is_idempotent_on_rerun(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    entries = manifest.entries_for("mql5")
    assert len(entries) == 1  # re-running doesn't duplicate manifest entries
