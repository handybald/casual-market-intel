"""Phase 2C regression tests (fourth review): deleting (or replacing/
truncating) the MQL5 exporter CSV leaves its sidecar window-log file
intact, since they are two separate files. A rerun of the exporter (or
simply re-running `ingest_mql5_calendar` against a CSV that was deleted
and recreated by hand, or truncated) would then trust the log's stale
"OK" entries for months the CURRENT CSV no longer actually contains any
rows for -- silently reporting those months "complete" with zero real
data. Window-log evidence alone must never be sufficient; the CSV must
also actually contain the rows it claims for that exact window.

Superseded by the fifth-review "WINDOW INTEGRITY CONTRACT" (see
src/data/fetch/mql5.py's module docstring and
tests/test_mql5_integrity_contract.py): the original fix here compared
only row PRESENCE (count > 0); it has since been replaced by a full
per-window content digest (EXPORT_SCHEMA_VERSION "3" -> "4" -> "5", the
latter for the sixth-review UTF-8 encoding fix -- see
tests/test_mql5_digest_encoding_contract.py), which strictly subsumes
this scenario (zero rows is one specific case of a digest/count
mismatch) plus several the count-only check could not catch
(header-only truncation, partial truncation, same-row-count content
modification). This file is kept as a narrower, still-valid regression
for the exact "zero rows" scenario using the current schema version;
the broader integrity contract has its own dedicated test file.
"""
import datetime as dt
import shutil
from pathlib import Path

from src.data.config import AppConfig
from src.data.manifest import Manifest
from src.data.fetch.mql5 import (
    MQL5_EXPORT_SCHEMA_VERSION,
    _read_raw_csv_rows,
    ingest_mql5_calendar,
    window_content_digest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mql5_sample.csv"  # only has January 2024 rows


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
    """`rows` is a list of (win_start_date, win_end_date_exclusive,
    status). For "OK" entries, computes the REAL canonical row count/
    content digest from the CSV's CURRENT content for that window --
    "test_window_log_ok_month_with_zero_actual_rows_is_not_trusted"
    below deliberately claims OK for a window the CSV has no rows for
    at all, which still produces a well-formed (count=0, digest-of-
    nothing) "OK" entry -- exactly the stale-evidence scenario under test."""
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


def test_window_log_ok_month_with_zero_actual_rows_is_not_trusted(tmp_path):
    """The exact named bug: the window log claims September was
    successfully exported WITH DATA, but the CSV currently on disk
    (e.g. deleted and recreated, or replaced with an unrelated export)
    has zero rows for September. Must be reported as missing/failed,
    never silently "complete". The log's "OK" evidence here is built
    from a row that would have been exported at the time -- NOT
    recomputed from the current (now September-empty) CSV -- since
    evidence must describe the data at successful export time (see
    module docstring)."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)  # fixture only has January 2024 rows -- none in September

    originally_exported_row = {
        "value_id": "900000999001", "event_id": "999", "event_name": "Some September Release",
        "country_code": "US", "currency_code": "USD", "importance": "HIGH",
        "event_time": "2024.09.05 13:30:00", "period": "2024.08.01", "unit": "PERCENT",
        "multiplier": "NONE", "actual_value": "1.000000", "forecast_value": "", "prev_value": "",
        "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }
    count, digest = window_content_digest([originally_exported_row])
    log_path = csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")
    log_path.write_text(
        f"2024.09.01 00:00:00,2024.10.01 00:00:00,US,USD,{MQL5_EXPORT_SCHEMA_VERSION},OK,"
        f"{count},1,2024.09.06 00:00:00,{digest}\n",
        encoding="utf-8",
    )

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 9, 1), dt.date(2024, 9, 30))

    assert "2024-09" not in report.covered_months
    assert "2024-09" in report.missing_months
    entry = manifest.get("mql5", "US:USD", "2024-09-01", "2024-09-30")
    assert entry.status == "failed"


def test_window_log_ok_month_with_actual_rows_present_is_still_trusted(tmp_path):
    """Sanity/non-regression: a genuinely matching window log entry for a
    month the CSV actually has rows for must still be trusted -- the
    fix must not turn into "never trust the window log"."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    shutil.copy(FIXTURE, csv_path)  # has January 2024 rows
    _write_window_log(csv_path, [(dt.date(2024, 1, 1), dt.date(2024, 2, 1), "OK")])

    config = make_config(tmp_path, csv_path)
    manifest = Manifest(config.manifest_path)
    report = ingest_mql5_calendar(config, manifest, dt.date(2024, 1, 1), dt.date(2024, 1, 31))

    assert "2024-01" in report.covered_months
    entry = manifest.get("mql5", "US:USD", "2024-01-01", "2024-01-31")
    assert entry.status == "complete"
