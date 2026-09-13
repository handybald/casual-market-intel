"""Phase 2B regression tests (fourth review):
`MigrateLegacyCsvIfNeeded()` in mql5_exporter/EconomicCalendarExporter.mq5
only checked the CSV's first column name ("value_id") to decide whether
an existing export file matched the current schema. The schema
IMMEDIATELY BEFORE the current one (EXPORT_SCHEMA_VERSION "2", which
added the `period` column) also started with `value_id` as its first
column -- so the old check would wrongly treat that older, incompatible
15-column file as "current schema" and start appending new 16-column
rows underneath its 15-column header, corrupting the file (every
column from `unit` onward silently shifted/misaligned for any tool that
reads it positionally or reconstructs it by fixed header names).

No MetaTrader compiler/runtime is available in this environment, so
`tests/_mql5_exporter_sim.py` provides a pure-Python mirror of both the
original and fixed `MigrateLegacyCsvIfNeeded()` decision logic (full
header vs. first-field comparison) to prove the fix actually
distinguishes the two schemas -- this is a behavioral-simulation cross-
check, not a substitute for compiling and running the real .mq5 file.

The Python-side ingestion (`parse_mql5_csv`) gets an independent,
defense-in-depth regression test: a file where new-schema (16-field)
rows were appended under an old-schema (15-field) header -- exactly
what the un-fixed exporter bug would produce -- must never be silently
misparsed (values landing under the wrong field names); such rows must
be rejected/skipped with a clear warning, not silently accepted.
"""
import logging

from tests._mql5_exporter_sim import (
    CURRENT_CSV_HEADER,
    OLDEST_CSV_HEADER_NO_VALUE_ID,
    PREVIOUS_CSV_HEADER_MISSING_PERIOD,
    migrate_legacy_csv_first_field_only,
    migrate_legacy_csv_full_header,
)

from src.data.fetch.mql5 import parse_mql5_csv


def test_original_first_field_check_wrongly_treats_previous_schema_as_current():
    """Documents the exact bug: the immediately-previous schema (has
    value_id, missing period) fools a first-field-only comparison."""
    assert migrate_legacy_csv_first_field_only(PREVIOUS_CSV_HEADER_MISSING_PERIOD, CURRENT_CSV_HEADER) is True


def test_fixed_full_header_check_correctly_rejects_previous_schema():
    assert migrate_legacy_csv_full_header(PREVIOUS_CSV_HEADER_MISSING_PERIOD, CURRENT_CSV_HEADER) is False


def test_fixed_full_header_check_correctly_rejects_oldest_schema():
    assert migrate_legacy_csv_full_header(OLDEST_CSV_HEADER_NO_VALUE_ID, CURRENT_CSV_HEADER) is False


def test_fixed_full_header_check_accepts_exact_current_schema():
    assert migrate_legacy_csv_full_header(CURRENT_CSV_HEADER, CURRENT_CSV_HEADER) is True


def test_fixed_full_header_check_rejects_truncated_header():
    truncated = CURRENT_CSV_HEADER.rsplit(",", 3)[0]  # missing the last 3 columns
    assert migrate_legacy_csv_full_header(truncated, CURRENT_CSV_HEADER) is False


def test_fixed_full_header_check_rejects_empty_header():
    assert migrate_legacy_csv_full_header("", CURRENT_CSV_HEADER) is False


def test_parse_mql5_csv_rejects_rows_with_mismatched_field_count_instead_of_silently_misaligning(tmp_path, caplog):
    """Simulates the corruption the fixed .mq5 exporter now prevents at
    the source: a new-schema (16-field, with `period`) row appended
    underneath an old-schema (15-field, no `period`) header. Reading
    such a file must never silently shift values into wrong-named
    fields (e.g. a release date landing in the `unit` column)."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    good_row = "1,100,Unemployment Rate,US,USD,HIGH,2024.01.05 13:30:00,PERCENT,NONE,3.7,3.8,3.7,,0,SERVER"
    # 16 comma-separated values under a 15-column header -- as if a
    # `period` field ("2024.01.01") had been inserted, exactly what the
    # un-fixed exporter bug would append.
    corrupted_row = "2,101,CPI m/m,US,USD,HIGH,2024.02.05 13:30:00,2024.01.01,PERCENT,NONE,0.3,0.3,0.3,,0,SERVER"
    csv_path.write_text(
        PREVIOUS_CSV_HEADER_MISSING_PERIOD + "\n" + good_row + "\n" + corrupted_row + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        rows = parse_mql5_csv(csv_path)

    assert len(rows) == 1
    assert rows[0].event_id == "100"
    assert any("malformed" in msg.lower() or "mismatch" in msg.lower() for msg in caplog.messages)
