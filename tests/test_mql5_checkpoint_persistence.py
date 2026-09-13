"""Regression tests (seventh review, P1): three confirmed defects in
the .mq5 exporter's checkpoint-persistence machinery.

## A. LogWindowStatus was destructive

The original implementation read the entire window log into memory,
then opened it with bare `FILE_WRITE | FILE_BIN` -- empirically
confirmed via `mql5_exporter/FileModeProbe.mq5` (compiled and run
inside an actual MetaTrader 5 terminal) to TRUNCATE the file
immediately on open, before a single byte of the rewrite happens. Any
interruption between that truncating open and the rewrite destroyed
every prior checkpoint record, not just the one being added, and a
failed read of the existing log was silently treated as "log absent"
(same consequence). The fix uses a genuine durable append: opening
with `FILE_READ | FILE_WRITE | FILE_BIN` (empirically confirmed via
the same probe to NOT truncate an existing file) plus an explicit,
checked `FileSeek` to end-of-file before writing.

## B. A failed seek could leave a usable-but-mispositioned writer

`FetchMonthWindow`'s reopen-after-rescan logic assigned the reopened
handle to the caller's `fileHandle` reference BEFORE checking whether
the follow-up `FileSeek` succeeded. On a seek failure, the caller's
only safety check (`fileHandle == INVALID_HANDLE`) passed anyway,
because the handle itself was real and open -- just parked at the
wrong position (empirically confirmed, again via FileModeProbe.mq5,
to be position 0 rather than end-of-file for a fresh reopen with no
explicit seek). The NEXT window's write would then land at that wrong
position, silently corrupting the start of the file. The fix closes
and invalidates the handle immediately whenever the seek fails.

## C. The v4->v5 encoding migration was incomplete

`MigrateLegacyCsvIfNeeded` compared only the DECODED header text
against the current schema's header -- but v4 (ANSI-encoded) and v5
(UTF-8-encoded) share byte-identical, pure-ASCII header text, so this
check could never actually distinguish them. An old ANSI file whose
DATA rows contain non-ASCII bytes (which are NOT valid UTF-8 on their
own) could be silently accepted as "compatible" and receive freshly
appended UTF-8 rows underneath its old, differently-encoded data --
producing a single file mixing two incompatible byte encodings. The
fix validates the RAW BYTES against RFC 3629 (a from-scratch
`IsValidUtf8`, matching the same design rationale as the FNV-1a
digest: no dependency on an unverifiable MQL5 library call) BEFORE
ever trusting a decoded header comparison.

All three fixes are additionally verified via ACTUAL COMPILATION AND
EXECUTION inside a real MetaTrader 5 terminal this session (MetaEditor
`/compile` + a scripted terminal `/config` startup) --
`mql5_exporter/FileModeProbe.mq5` (empirical FileOpen/FileSeek
semantics) and `mql5_exporter/FileRecoverySelfTest.mq5` (8/8
synthetic scenarios covering append-durability, the old bug's
mechanism, valid/invalid/truncated UTF-8, and missing-file handling)
both passed with 0 compiler errors and 100% scenario pass rate. See
the final report for the exact compiler/runtime output. The tests
below are Python simulations of the equivalent CONTROL FLOW
(`tests/_mql5_exporter_sim.py`), not a substitute for that compiled
evidence -- they exist to also pin the DESIGN down at the unit-test
level and to exercise scenarios (checkpoint-failure propagation
through the full `run()` loop, malformed/version-mismatched log
entries) not covered by the standalone .mq5 self-tests.
"""
import datetime as dt

import pytest

from tests._mql5_exporter_sim import (
    CSV_HEADER_LINE,
    SimulatedMql5Exporter,
    simulate_seek_bug_fixed,
    simulate_seek_bug_old,
)


def _row(value_id="1", event_time="2024.01.05 13:30:00"):
    return {
        "value_id": value_id, "event_id": "100", "event_name": "NFP", "country_code": "US",
        "currency_code": "USD", "importance": "HIGH", "event_time": event_time, "period": "2023.12.01",
        "unit": "JOB", "multiplier": "THOUSANDS", "actual_value": "216.000000", "forecast_value": "",
        "prev_value": "", "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }


WINDOW_A = (dt.date(2024, 1, 1), dt.date(2024, 1, 31))
WINDOW_B = (dt.date(2024, 2, 1), dt.date(2024, 2, 29))


# =====================================================================
# Defect A: destructive checkpoint-log rewrite
# =====================================================================

def test_A_baseline_old_buggy_log_write_destroys_history_on_interruption(tmp_path):
    """Reproduces the defect: an interruption between the truncating
    open and the rewrite leaves the log file EMPTY, destroying prior
    checkpoint records that had nothing wrong with them."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    exporter._log_window_status_old_buggy(*WINDOW_A, "OK", 1, "deadbeef00000001")
    assert exporter.log_path.read_text(encoding="utf-8").strip() != ""

    ok = exporter._log_window_status_old_buggy(
        *WINDOW_B, "OK", 1, "deadbeef00000002", simulate_interruption_after_truncate=True,
    )
    assert ok is False
    # The interruption happened AFTER the truncating open -- window A's
    # earlier, perfectly valid checkpoint record is gone.
    assert exporter.log_path.read_text(encoding="utf-8") == ""


def test_A_baseline_old_buggy_log_write_treats_read_failure_as_absent(tmp_path, monkeypatch):
    """The original bug's second failure mode: a failed READ of the
    existing log (not just a missing one) was silently treated as "no
    prior history", proceeding to truncate anyway."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    exporter._log_window_status_old_buggy(*WINDOW_A, "OK", 1, "deadbeef00000001")
    assert exporter.log_path.exists()

    original_read_text = type(exporter.log_path).read_text

    def failing_read_text(self, *a, **k):
        if self == exporter.log_path:
            raise OSError("simulated unreadable/corrupt log")
        return original_read_text(self, *a, **k)

    monkeypatch.setattr(type(exporter.log_path), "read_text", failing_read_text)
    exporter._log_window_status_old_buggy(*WINDOW_B, "OK", 1, "deadbeef00000002")
    monkeypatch.undo()

    # Window A's record is gone -- a read FAILURE (not "file absent") was
    # silently treated the same way, and the truncating open still ran.
    content = exporter.log_path.read_text(encoding="utf-8")
    assert "deadbeef00000001" not in content
    assert "deadbeef00000002" in content


def test_A_fixed_durable_append_preserves_history_across_multiple_calls(tmp_path):
    exporter = SimulatedMql5Exporter(tmp_path / "us_macro_calendar.csv")
    exporter._log_window_status(*WINDOW_A, "OK", 1, "deadbeef00000001")
    exporter._log_window_status(*WINDOW_B, "OK", 1, "deadbeef00000002")
    content = exporter.log_path.read_text(encoding="utf-8")
    assert "deadbeef00000001" in content
    assert "deadbeef00000002" in content
    assert len(content.strip().splitlines()) == 2


def test_A_checkpoint_persistence_failure_propagates_to_window_outcome(tmp_path):
    """Data is genuinely fetched/verified, but the FINAL checkpoint
    write itself fails -- the window's outcome must reflect that
    (never "OK"), and no evidence claiming success may exist."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    result = exporter.run(
        [WINDOW_A], lambda ws, we: [_row()],
        simulate_checkpoint_failure_for={WINDOW_A},
    )
    assert result["results"][0]["status"] == "CHECKPOINT_FAILED"
    assert result["results"][0]["status"] != "OK"
    # No durable "OK" evidence exists for this window.
    log_content = exporter.log_path.read_text(encoding="utf-8") if exporter.log_path.exists() else ""
    assert ",OK," not in log_content

    # A subsequent identical run must NOT treat this as already done --
    # it should genuinely re-attempt the window (converges: repair works).
    calls = []
    result2 = exporter.run([WINDOW_A], lambda ws, we: calls.append((ws, we)) or [_row()])
    assert calls == [WINDOW_A]
    assert result2["results"][0]["status"] == "OK"


# =====================================================================
# Defect B: seek failure leaving a usable-but-mispositioned writer
# =====================================================================

def test_B_baseline_old_buggy_seek_handling_corrupts_existing_bytes(tmp_path):
    path = tmp_path / "us_macro_calendar.csv"
    first_write = b"HEADER\nEXISTING_ROW_ONE\nEXISTING_ROW_TWO\n"
    second_write = b"CORRUPT"

    result = simulate_seek_bug_old(path, first_write, second_write)

    # The bug: window 2's write landed at position 0, overwriting the
    # START of the file instead of appending -- the original content is
    # no longer intact, and the file does NOT simply equal
    # first_write + second_write (correct append).
    assert result != first_write + second_write
    assert not result.startswith(first_write)
    assert result.startswith(second_write)  # "CORRUPT" clobbered the header/first row


def test_B_fixed_seek_handling_leaves_existing_bytes_unchanged(tmp_path):
    """The exact scenario required by the review: a multi-window run
    where the first window's seek fails -- existing bytes must remain
    unchanged by (i.e. never touched by) any subsequent window."""
    path = tmp_path / "us_macro_calendar.csv"
    first_write = b"HEADER\nEXISTING_ROW_ONE\nEXISTING_ROW_TWO\n"
    second_write = b"CORRUPT"

    result = simulate_seek_bug_fixed(path, first_write, second_write)

    assert result == first_write  # completely unchanged -- window 2 never wrote at all


# =====================================================================
# Defect C: v4 -> v5 encoding migration incomplete
# =====================================================================

def test_C_baseline_old_buggy_migration_accepts_ansi_file_with_matching_ascii_header(tmp_path):
    """Reproduces the defect: an ANSI-encoded legacy file whose header
    happens to be pure ASCII (identical to the v5 header) is wrongly
    accepted for continued append, even though its DATA rows contain
    bytes that are not valid UTF-8 at all."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    # A lone 0xE9 byte ("e" with an acute accent in Windows-1252) is not
    # a valid standalone UTF-8 sequence.
    legacy_bytes = CSV_HEADER_LINE.encode("ascii") + b"\n1,100,Caf\xe9,US,USD,HIGH,x,x,x,x,x,x,x,x,0,SERVER\n"
    csv_path.write_bytes(legacy_bytes)

    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed_old_buggy()

    assert outcome == "READY_EXISTING"  # the bug: accepted despite invalid UTF-8 content


def test_C_fixed_migration_rejects_the_same_ansi_file(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    legacy_bytes = CSV_HEADER_LINE.encode("ascii") + b"\n1,100,Caf\xe9,US,USD,HIGH,x,x,x,x,x,x,x,x,0,SERVER\n"
    csv_path.write_bytes(legacy_bytes)

    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed()

    assert outcome == "READY_NEW"  # correctly rejected -- moved aside instead
    legacy_files = list(tmp_path.glob("us_macro_calendar.csv.legacy_*.csv"))
    assert len(legacy_files) == 1
    assert legacy_files[0].read_bytes() == legacy_bytes  # preserved byte-for-byte, untouched


def test_C_fixed_migration_accepts_genuine_valid_utf8_file(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    utf8_bytes = (CSV_HEADER_LINE + "\n1,100,Café,US,USD,HIGH,x,x,x,x,x,x,x,x,0,SERVER\n").encode("utf-8")
    csv_path.write_bytes(utf8_bytes)

    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed()

    assert outcome == "READY_EXISTING"


def test_C_fixed_migration_accepts_pure_ascii_legacy_content(tmp_path):
    """Pure ASCII is byte-identical under ANSI and UTF-8 -- this is the
    one case where accepting an old file without a definitive encoding
    signal is actually safe, per the documented design decision."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    ascii_bytes = (CSV_HEADER_LINE + "\n1,100,NFP,US,USD,HIGH,x,x,x,x,x,x,x,x,0,SERVER\n").encode("ascii")
    csv_path.write_bytes(ascii_bytes)

    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed()

    assert outcome == "READY_EXISTING"


def test_C_fixed_migration_rejects_malformed_utf8_and_preserves_legacy(tmp_path):
    """A truncated/malformed multi-byte sequence (not merely a single
    high byte) must also be rejected."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    malformed = CSV_HEADER_LINE.encode("ascii") + b"\n1,100,X" + bytes([0xE2, 0x82]) + b",US,USD,HIGH,x,x,x,x,x,x,x,x,0,SERVER\n"
    csv_path.write_bytes(malformed)

    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed()

    assert outcome == "READY_NEW"
    legacy_files = list(tmp_path.glob("us_macro_calendar.csv.legacy_*.csv"))
    assert len(legacy_files) == 1
    assert legacy_files[0].read_bytes() == malformed


def test_C_fixed_migration_handles_missing_and_version_mismatched_metadata(tmp_path):
    """No existing file at all -> fresh; a file with the WRONG header
    (regardless of encoding) -> migrated aside, never silently accepted."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    assert exporter.migrate_legacy_csv_if_needed() == "READY_NEW"  # missing file

    csv_path.write_text("totally,different,header\n1,2,3\n", encoding="utf-8")
    outcome = exporter.migrate_legacy_csv_if_needed()
    assert outcome == "READY_NEW"  # valid UTF-8, but header doesn't match -- still migrated aside
    assert len(list(tmp_path.glob("us_macro_calendar.csv.legacy_*.csv"))) == 1
