"""Regression tests (fifth review, P1, issue #2): `MigrateLegacyCsvIfNeeded()`
in mql5_exporter/EconomicCalendarExporter.mq5 returned `false` for BOTH
"file has the correct header, append in place" and "failed to read/move
an incompatible file" -- two semantically opposite outcomes collapsed
into one boolean. `OnStart()`'s guard

    if(!needFreshFile && !FileIsExist(OutputFile))
        return;

only catches the sub-case where the incompatible file no longer exists
after a failed move; if `FileMove` fails and the original (incompatible)
file is still there, this guard is false, execution falls through into
append mode, and the exporter appends CURRENT-schema rows underneath an
INCOMPATIBLE header -- exactly the corruption `MigrateLegacyCsvIfNeeded`
claims to prevent, despite its own error message saying it "refuses to
append".

No MetaTrader compiler/runtime exists in this environment. This file
uses `tests/_mql5_exporter_sim.py`'s `simulate_old_buggy_onstart_control_flow`
(a literal transliteration of the ORIGINAL control flow) to reproduce
the defect under simulation, and `SimulatedMql5Exporter` (a mirror of
the REDESIGNED three-outcome `ENUM_MIGRATION_OUTCOME` control flow) to
prove the fix. These are Python simulations of MQL5 control flow, NOT
compiled or executed MQL5 tests -- see the final report for the
distinction and a manual MetaTrader verification procedure.
"""
import datetime as dt
from pathlib import Path

from tests._mql5_exporter_sim import (
    CSV_HEADER_LINE,
    SimulatedMql5Exporter,
    simulate_old_buggy_onstart_control_flow,
)

INCOMPATIBLE_HEADER = "event_id,event_name,country_code,currency_code,importance,event_time"


def _write(path: Path, header: str, *data_lines: str) -> None:
    path.write_text("\n".join([header, *data_lines]) + ("\n" if data_lines or header else ""), encoding="utf-8")


# -- baseline: reproduce the defect against the ORIGINAL control flow --

def test_baseline_old_control_flow_appends_under_incompatible_header_after_failed_migration(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, INCOMPATIBLE_HEADER, "1,Foo,US,USD,HIGH,2024.01.01 00:00:00")
    original_bytes = csv_path.read_bytes()

    outcome = simulate_old_buggy_onstart_control_flow(csv_path, force_migration_failure=True)

    assert outcome == "APPENDED_INCOMPATIBLE", (
        "this reproduces the confirmed defect: migration failure must abort, but the "
        "original control flow falls through into append mode"
    )
    # The file wasn't actually appended to by this minimal repro helper,
    # but the DECISION to open it for append (rather than abort) is
    # exactly the confirmed defect -- assert the original bytes are what
    # would be corrupted next.
    assert csv_path.read_bytes() == original_bytes


def test_baseline_old_control_flow_returns_false_identically_for_compatible_and_error(tmp_path):
    """Documents the root cause directly: two opposite outcomes both
    produce needFreshFile == false (Python bool False here)."""
    compatible_csv = tmp_path / "compatible.csv"
    _write(compatible_csv, CSV_HEADER_LINE)
    compatible_outcome = simulate_old_buggy_onstart_control_flow(compatible_csv, force_migration_failure=False)

    incompatible_csv = tmp_path / "incompatible.csv"
    _write(incompatible_csv, INCOMPATIBLE_HEADER, "1,Foo,US,USD,HIGH,2024.01.01 00:00:00")
    error_outcome = simulate_old_buggy_onstart_control_flow(incompatible_csv, force_migration_failure=True)

    assert compatible_outcome == "APPENDED_COMPATIBLE"
    assert error_outcome == "APPENDED_INCOMPATIBLE"
    # Both are reached via the exact same `needFreshFile == false` branch
    # in the original code -- the bug is that the control flow cannot
    # tell them apart, even though the two simulated outcomes differ.


# -- fixed design: SimulatedMql5Exporter's three-way outcome --

def test_1_compatible_header_append_permitted(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, CSV_HEADER_LINE, "1,100,NFP,US,USD,HIGH,2024.01.05 13:30:00,2023.12.01,JOB,THOUSANDS,216.000000,,,,0,SERVER")

    exporter = SimulatedMql5Exporter(csv_path)
    fetch_calls = []

    def fetch_fn(ws, we):
        fetch_calls.append((ws, we))
        return [{
            "value_id": "2", "event_id": "101", "event_name": "CPI", "country_code": "US",
            "currency_code": "USD", "importance": "HIGH", "event_time": "2024.02.05 13:30:00",
            "period": "2024.01.01", "unit": "PERCENT", "multiplier": "NONE",
            "actual_value": "0.300000", "forecast_value": "", "prev_value": "", "revised_prev_value": "",
            "revision": "0", "source_timezone": "SERVER",
        }]

    result = exporter.run([(dt.date(2024, 2, 1), dt.date(2024, 2, 29))], fetch_fn)
    assert result["aborted"] is False
    assert len(fetch_calls) == 1
    assert result["results"][0]["status"] == "OK"
    # Original January row preserved (append, not truncate).
    assert "NFP" in csv_path.read_text(encoding="utf-8")


def test_2_no_existing_file_creates_current_schema(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    result = exporter.run(
        [(dt.date(2024, 1, 1), dt.date(2024, 1, 31))],
        lambda ws, we: [],
    )
    assert result["aborted"] is False
    assert csv_path.exists()
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == CSV_HEADER_LINE


def test_3_incompatible_header_successful_migration_preserves_legacy_and_creates_fresh(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, INCOMPATIBLE_HEADER, "1,Foo,US,USD,HIGH,2024.01.01 00:00:00")

    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([(dt.date(2024, 1, 1), dt.date(2024, 1, 31))], lambda ws, we: [])

    assert result["aborted"] is False
    legacy_files = list(tmp_path.glob("us_macro_calendar.csv.legacy_*.csv"))
    assert len(legacy_files) == 1
    assert legacy_files[0].read_text(encoding="utf-8").splitlines()[0] == INCOMPATIBLE_HEADER
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == CSV_HEADER_LINE  # fresh file


def test_4_incompatible_header_failed_migration_aborts_with_original_bytes_unchanged(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, INCOMPATIBLE_HEADER, "1,Foo,US,USD,HIGH,2024.01.01 00:00:00")
    original_bytes = csv_path.read_bytes()

    exporter = SimulatedMql5Exporter(csv_path)
    fetch_calls = []
    result = exporter.run(
        [(dt.date(2024, 1, 1), dt.date(2024, 1, 31))],
        lambda ws, we: fetch_calls.append((ws, we)) or [],
        force_migration_failure=True,
    )

    assert result["aborted"] is True
    assert result["results"] == []
    assert fetch_calls == []
    assert csv_path.read_bytes() == original_bytes
    assert not exporter.log_path.exists()


def test_5_header_read_failure_aborts(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, CSV_HEADER_LINE)

    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run(
        [(dt.date(2024, 1, 1), dt.date(2024, 1, 31))],
        lambda ws, we: (_ for _ in ()).throw(AssertionError("must not be called")),
        force_read_failure=True,
    )
    assert result["aborted"] is True
    assert result["results"] == []


def test_6_migration_failure_causes_no_fetch_no_append_no_success_log(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    _write(csv_path, INCOMPATIBLE_HEADER, "1,Foo,US,USD,HIGH,2024.01.01 00:00:00")

    exporter = SimulatedMql5Exporter(csv_path)
    calls = []
    result = exporter.run(
        [(dt.date(2024, 1, 1), dt.date(2024, 1, 31)), (dt.date(2024, 2, 1), dt.date(2024, 2, 29))],
        lambda ws, we: calls.append((ws, we)) or [],
        force_migration_failure=True,
    )

    assert result["aborted"] is True
    assert calls == []  # no calendar requests issued
    assert not exporter.log_path.exists()  # no checkpoint evidence written
