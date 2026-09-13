import datetime as dt
import shutil
from pathlib import Path

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch.mql5 import (
    MQL5_EXPORT_SCHEMA_VERSION,
    _read_raw_csv_rows,
    _window_key_for_chunk,
    ingest_mql5_calendar,
    read_window_log,
    window_content_digest,
)

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
            "mql5": {
                "chunk_frequency": "month",
                "raw_dir": str(tmp_path / "raw" / "mql5"),
                "input_csv": str(csv_path),
                "broker_timezone": None,
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def _write_window_log(csv_path: Path, rows) -> Path:
    """`rows` is a list of (win_start_date, win_end_date_exclusive, status)
    date tuples; writes the current 10-field, digest-based (v4) window
    log format. For "OK" entries, computes the REAL canonical row
    count/content digest from the CSV's CURRENT content for that exact
    window -- so a genuinely matching "OK" claim in a test is actually
    correct, exactly like a real exporter run would record (never a
    fabricated digest that happens to be accepted)."""
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    lines = []
    for start, end_exclusive, status in rows:
        if status == "OK":
            end_inclusive = end_exclusive - dt.timedelta(days=1)
            raw_rows = _read_raw_csv_rows(csv_path) if csv_path.exists() else []
            window_rows = [
                r for r in raw_rows
                if start <= dt.datetime.strptime(r["event_time"], "%Y.%m.%d %H:%M:%S").date() <= end_inclusive
            ]
            count, digest = window_content_digest(window_rows)
        else:
            count, digest = 0, ""
        lines.append(
            f"{start:%Y.%m.%d} 00:00:00,{end_exclusive:%Y.%m.%d} 00:00:00,US,USD,"
            f"{MQL5_EXPORT_SCHEMA_VERSION},{status},{count},1,2024.01.31 00:00:00,{digest}"
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def test_ingest_reports_covered_and_missing_months_no_window_log(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)  # fixture only has Jan 2024 rows, no sidecar log
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    report = ingest_mql5_calendar(config, manifest, dt.date(2023, 12, 1), dt.date(2024, 1, 31))

    assert report.covered_months == ["2024-01"]
    assert report.missing_months == ["2023-12"]
    assert report.total_rows == 4
    assert report.coverage_inferred is True  # honest: no window log, row-presence fallback


def test_row_presence_fallback_is_provisional_not_finalized(tmp_path):
    """Regression: the fallback must NEVER finalize coverage -- a month
    with rows but no verifying window log is recorded PROVISIONAL, which
    `is_complete` never treats as done."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    key = "US:USD"
    entry = manifest.get("mql5", key, "2024-01-01", "2024-01-31")
    assert entry.status == "provisional"
    assert not manifest.is_complete("mql5", key, "2024-01-01", "2024-01-31")


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


# -- durable per-window status log: row presence is NOT proof of completeness --

def test_window_log_failed_month_is_not_covered_even_with_rows_present(tmp_path):
    """Reproduces the bug: a month can have rows in the CSV (e.g. from a
    partial/interrupted fetch) yet NOT actually be complete. When the
    exporter's durable window log says FAILED, that must win over row
    presence."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    _write_window_log(csv_path, [(dt.date(2024, 1, 1), dt.date(2024, 2, 1), "FAILED")])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert report.coverage_inferred is False
    assert report.covered_months == []
    assert report.missing_months == ["2024-01"]
    assert not manifest.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")


def test_window_log_ok_month_is_covered(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    _write_window_log(csv_path, [(dt.date(2024, 1, 1), dt.date(2024, 2, 1), "OK")])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert report.coverage_inferred is False
    assert report.covered_months == ["2024-01"]
    assert manifest.is_complete("mql5", "US:USD", "2024-01-01", "2024-01-31")


def test_window_log_last_status_wins_on_retry(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write_window_log(
        csv_path,
        [(dt.date(2024, 1, 1), dt.date(2024, 2, 1), "FAILED"), (dt.date(2024, 1, 1), dt.date(2024, 2, 1), "OK")],
    )
    statuses = read_window_log(csv_path, "US", "USD")
    key = _window_key_for_chunk(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert statuses[key].status == "OK"


def test_window_log_entry_for_different_country_is_ignored(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    log_path.write_text(
        f"2024.01.01 00:00:00,2024.02.01 00:00:00,DE,EUR,{MQL5_EXPORT_SCHEMA_VERSION},OK,4,1,2024.01.31 00:00:00,abc123\n",
        encoding="utf-8",
    )
    statuses = read_window_log(csv_path, "US", "USD")
    assert statuses == {}  # a DE/EUR entry must never satisfy a US/USD lookup


def test_window_log_entry_for_wrong_schema_version_is_ignored(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    log_path.write_text(
        "2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,1,OK,4,1,2024.01.31 00:00:00,abc123\n", encoding="utf-8",
    )
    statuses = read_window_log(csv_path, "US", "USD")
    assert statuses == {}


def test_window_log_entry_with_malformed_field_count_is_ignored(tmp_path):
    """The full v4 row shape (10 fields) must be validated -- a
    truncated/incomplete row must never certify completion."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    log_path.write_text(
        f"2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,{MQL5_EXPORT_SCHEMA_VERSION},OK,4,1\n",  # missing 2 fields
        encoding="utf-8",
    )
    statuses = read_window_log(csv_path, "US", "USD")
    assert statuses == {}


def test_window_log_ok_entry_with_empty_digest_is_ignored(tmp_path):
    """An "OK" row must carry a non-empty digest to be trusted -- an
    empty digest field (e.g. from an old/differently-shaped writer)
    must never certify completion."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    log_path.write_text(
        f"2024.01.01 00:00:00,2024.02.01 00:00:00,US,USD,{MQL5_EXPORT_SCHEMA_VERSION},OK,4,1,2024.01.31 00:00:00,\n",
        encoding="utf-8",
    )
    statuses = read_window_log(csv_path, "US", "USD")
    assert statuses == {}


def test_coverage_inferred_flagged_true_only_without_window_log(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert report.coverage_inferred is True

    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.request_meta.get("coverage_inferred") is True


# -- regression: partial-month export followed by a wider export must NOT
# skip the rest of the month (second review item #3, core reproduction) --

def test_partial_month_window_does_not_satisfy_a_wider_later_request(tmp_path):
    """Exporting Sept 1-10 logs that EXACT window as OK. A later request
    for the full month (Sept 1-30) must NOT be considered covered by
    that narrower log entry -- the old (year, month)-only key would
    incorrectly treat them as the same checkpoint."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)  # only has Jan 2024 rows; window log is what we're testing here
    # Exporter logged a partial window: Sept 1 -> Sept 10 inclusive, i.e.
    # exclusive end Sept 11 (see the uniform inclusive-date contract in
    # src/data/fetch/mql5.py's module docstring).
    _write_window_log(csv_path, [(dt.date(2024, 9, 1), dt.date(2024, 9, 11), "OK")])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    # Request the FULL month now.
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 9, 1), dt.date(2024, 9, 30))

    assert "2024-09" in report.missing_months  # NOT considered covered by the partial window
    assert not manifest.is_complete("mql5", "US:USD", "2024-09-01", "2024-09-30")


def test_partial_month_window_key_matches_a_rerun_of_the_same_partial_request(tmp_path):
    """The flip side: re-running the SAME partial request (Sept 1-10
    again) must correctly match and be treated as covered."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)
    # A window log "OK" entry alone is not enough (see
    # test_mql5_stale_window_log_phase2c.py) -- the CSV must actually
    # contain a row for the month it claims.
    with open(csv_path, "a", encoding="utf-8") as fh:
        fh.write(
            "900000200001,200001,CPI m/m,US,USD,HIGH,2024.09.05 13:30:00,2024.08.01,PERCENT,NONE,"
            "0.300000,0.300000,0.300000,,0,SERVER\n"
        )
    # Sept 1-10 inclusive -> exclusive end Sept 11.
    _write_window_log(csv_path, [(dt.date(2024, 9, 1), dt.date(2024, 9, 11), "OK")])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)

    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 9, 1), dt.date(2024, 9, 10))

    assert "2024-09" in report.covered_months
    assert manifest.is_complete("mql5", "US:USD", "2024-09-01", "2024-09-10")


def test_window_key_for_chunk_full_month_is_exclusive_day_after():
    start, end = _window_key_for_chunk(dt.date(2024, 9, 1), dt.date(2024, 9, 30))
    assert start == "2024.09.01 00:00:00"
    assert end == "2024.10.01 00:00:00"  # exclusive: first day of the NEXT month


def test_window_key_for_chunk_clipped_end_is_also_day_after_uniformly():
    """No month-end special-casing: a clipped/partial window's inclusive
    end is converted to an exclusive boundary the SAME way a full
    month's is -- always +1 day, regardless of whether it happens to
    land on a natural month end."""
    start, end = _window_key_for_chunk(dt.date(2024, 9, 1), dt.date(2024, 9, 10))
    assert start == "2024.09.01 00:00:00"
    assert end == "2024.09.11 00:00:00"
