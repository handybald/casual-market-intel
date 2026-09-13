"""Regression tests (sixth review, P1, issue #1): `OnStart()` opened
OutputFile for writing and held that handle open for the ENTIRE
per-window loop, including every `WindowAlreadyOk()` call -- which
internally calls `RescanWindowDigest()`, opening the SAME path again
for reading, despite that function's own docstring explicitly
requiring no write handle to be open. This finding is based on source
inspection of the (then-current) control flow; it has not been
executed inside MetaTrader.

THE FIX (see mql5_exporter/EconomicCalendarExporter.mq5's OnStart, and
its "FILE HANDLE OWNERSHIP" note): resume verification for EVERY window
now runs in a first pass, before any writer is ever opened. A writer is
opened only for a second pass over the windows that actually need
fetching -- and FetchMonthWindow itself still explicitly closes that
writer before its own post-write RescanWindowDigest call (unchanged
from the fifth review), reopening only afterward, with every open/
close/reopen/seek result checked.

No MetaTrader compiler/runtime exists in this environment. This file
uses `tests/_mql5_exporter_sim.py`'s `SimulatedMql5Exporter`, now
extended with an `ExclusiveFileHandleRegistry` that raises
`FileHandleConflictError` if two "handles" to the same path are ever
held open concurrently -- modeling the assumption (stated in the real
.mq5 file) that MQL5 does not safely support this, so the simulator can
actually DETECT this specific defect rather than silently tolerating it
the way ordinary Python file I/O would. `run_with_old_buggy_handle_ordering`
is a structural reproduction of the ORIGINAL OnStart() (writer opened
before the loop, held throughout); `run()` is the FIXED design. These
are Python simulations of MQL5 control flow, NOT compiled or executed
MQL5 tests -- see the final report for a manual MetaTrader verification
procedure.
"""
import datetime as dt

import pytest

from tests._mql5_exporter_sim import FileHandleConflictError, SimulatedMql5Exporter


def _row(value_id="1", event_time="2024.01.05 13:30:00", actual="216.000000"):
    return {
        "value_id": value_id, "event_id": "100", "event_name": "NFP", "country_code": "US",
        "currency_code": "USD", "importance": "HIGH", "event_time": event_time, "period": "2023.12.01",
        "unit": "JOB", "multiplier": "THOUSANDS", "actual_value": actual, "forecast_value": "",
        "prev_value": "", "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }


WINDOW = (dt.date(2024, 1, 1), dt.date(2024, 1, 31))


# -- baseline: reproduce the defect via the enforced-exclusivity simulator --

def test_baseline_old_buggy_ordering_raises_handle_conflict_against_existing_data(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"

    # Populate real, verifiable data+evidence first, using the FIXED run().
    setup_exporter = SimulatedMql5Exporter(csv_path)
    setup_exporter.run([WINDOW], lambda ws, we: [_row()])

    # A fresh "process" (new SimulatedMql5Exporter instance, own handle
    # registry) resuming against that SAME on-disk data, using the
    # ORIGINAL (buggy) handle-ordering structure, must hit the conflict
    # the moment it tries to verify the existing window.
    resume_exporter = SimulatedMql5Exporter(csv_path)
    with pytest.raises(FileHandleConflictError):
        resume_exporter.run_with_old_buggy_handle_ordering([WINDOW], lambda ws, we: [_row()])


def test_fixed_ordering_does_not_conflict_for_the_same_scenario(tmp_path):
    """The same setup that reproduces the defect above must NOT raise
    under the fixed design -- proving the fix, not just the bug."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    setup_exporter = SimulatedMql5Exporter(csv_path)
    setup_exporter.run([WINDOW], lambda ws, we: [_row()])

    resume_exporter = SimulatedMql5Exporter(csv_path)
    result = resume_exporter.run([WINDOW], lambda ws, we: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert result["aborted"] is False
    assert result["results"][0]["status"] == "SKIPPED"


# -- 1: initial export succeeds --

def test_1_initial_export_succeeds(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW], lambda ws, we: [_row()])
    assert result["aborted"] is False
    assert result["results"][0]["status"] == "OK"
    assert csv_path.exists()
    assert exporter.log_path.exists()


# -- 2: identical second run skips completed windows without fetching --

def test_2_identical_second_run_skips_without_fetching(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    exporter.run([WINDOW], lambda ws, we: [_row()])

    calls = []
    result = exporter.run([WINDOW], lambda ws, we: calls.append((ws, we)) or [_row()])
    assert calls == []
    assert result["results"][0]["status"] == "SKIPPED"


# -- 3: partial truncation causes repair --

def test_3_partial_truncation_causes_repair(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    two_rows = [_row("1", "2024.01.05 13:30:00"), _row("2", "2024.01.10 13:30:00")]
    exporter = SimulatedMql5Exporter(csv_path)
    exporter.run([WINDOW], lambda ws, we: two_rows)

    # Damage: truncate the CSV down to just its header (simulating a
    # partial/failed write outside this script's control).
    csv_path.write_text(csv_path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")

    calls = []
    result = exporter.run([WINDOW], lambda ws, we: calls.append((ws, we)) or two_rows)
    assert len(calls) == 1  # re-fetched, not skipped
    assert result["results"][0]["status"] == "OK"

    # Repaired: the CSV now has both rows again, and fresh matching evidence.
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # header + 2 rows


# -- 4: verification read failure is reported clearly (not silently OK, not a crash) --

def test_4_verification_with_missing_log_or_csv_reports_false_cleanly(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    exporter.run([WINDOW], lambda ws, we: [_row()])

    # Missing window log -- verification must cleanly report "not OK",
    # never raise and never silently claim success.
    exporter.log_path.unlink()
    assert exporter._window_already_ok(*WINDOW) is False

    # Missing CSV entirely.
    csv_path.unlink()
    assert exporter._window_already_ok(*WINDOW) is False


# -- 5: reopen/write failure cannot produce successful evidence --

def test_5_simulated_write_failure_produces_no_successful_evidence(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    result = exporter.run([WINDOW], lambda ws, we: [_row()], simulate_write_failure_for={WINDOW})
    assert result["results"][0]["status"] == "WRITE_FAILED"
    # No checkpoint evidence at all was published for this window.
    assert not exporter.log_path.exists()


# -- 6: multiple windows alternate between skipped and fetched, no handle errors --

def test_6_mixed_skip_and_fetch_windows_in_one_run_no_handle_errors(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    window_a = (dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    window_b = (dt.date(2024, 2, 1), dt.date(2024, 2, 29))

    exporter = SimulatedMql5Exporter(csv_path)
    exporter.run([window_a], lambda ws, we: [_row("1", "2024.01.05 13:30:00")])  # A already OK afterward

    calls = []

    def fetch_fn(ws, we):
        calls.append((ws, we))
        return [_row("2", "2024.02.05 13:30:00")]

    result = exporter.run([window_a, window_b], fetch_fn)  # A should skip, B should fetch
    assert result["aborted"] is False
    statuses = {r["window"]: r["status"] for r in result["results"]}
    assert statuses[window_a] == "SKIPPED"
    assert statuses[window_b] == "OK"
    assert calls == [window_b]
