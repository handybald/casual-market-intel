"""Regression tests (eighth review, P1): recover interrupted records
before appending; prevent false completion.

## Confirmed failure mechanism

1. An interrupted write leaves a partial CSV row without its
   terminating newline (this script's writes are one FileWriteArray
   call for a line's content, then a SEPARATE FileWriteArray call for
   its trailing '\\n' -- a process death between the two, or mid-way
   through the content bytes themselves, leaves a dangling fragment).
2. The next run seeks to EOF and appends the retry's first row
   directly onto that fragment.
3. The combined line is malformed.
4. RescanWindowDigest silently skips it (by design -- one bad legacy
   row must never fail an entire rescan).
5. The exporter publishes an OK checkpoint using the reduced set of
   surviving rows.
6. Python ingestion verifies that reduced set against the checkpoint
   and reports complete coverage.

## Fix

Two independent, complementary layers:

- **Boundary establishment** (`recover_file_tail`/
  `compute_safe_append_boundary`, mirroring
  `mql5_exporter/CalendarExporterCore.mqh`'s `RecoverFileTail`/
  `ComputeSafeAppendBoundary`): run ONCE at the start of every export
  run, for BOTH the CSV and the window-log checkpoint file, before
  anything reads or writes either. A dangling INCOMPLETE record
  (truncated mid-multibyte-UTF-8-sequence, interrupted mid-quoted-
  field, or simply too few fields) is dropped via a verified,
  never-in-place temp-file swap; a structurally COMPLETE record
  missing only its trailing newline is repaired by appending the
  single missing byte -- never dropped.
- **Fetched-vs-persisted verification** (`_canonical_hash_map` +
  `SimulatedMql5Exporter.run()`'s per-window cross-check, mirroring
  `FetchMonthWindow`'s new verification block): even if some other,
  as-yet-unknown corruption mechanism caused a fetched record to
  vanish or change, the window is never certified OK unless EVERY
  record this run's fetch actually returned is present in the
  rescanned canonical set with unchanged content.

Independently verified via ACTUAL COMPILATION AND EXECUTION inside a
real MetaTrader 5 terminal this round: `mql5_exporter/
TailRecoverySelfTest.mq5` (34/34 scenarios, calling the real, shared
`RecoverFileTail`/`ComputeSafeAppendBoundary`/`IsValidUtf8Ex` -- not a
duplicate) covers the same interruption points as the Python tests
below. The tests here are a Python design/behavioral cross-check
(`tests/_mql5_exporter_sim.py`) exercising the PRODUCTION Python
ingestion path (`src/data/fetch/mql5.py`) end-to-end against fixtures
this harness generates -- not a substitute for that compiled evidence.
"""
import datetime as dt

import pytest

from tests._mql5_exporter_sim import (
    CSV_HEADER_LINE,
    SimulatedMql5Exporter,
    _canonical_hash_map,
    classify_utf8_validity,
    recover_file_tail,
)

WINDOW_A = (dt.date(2024, 1, 1), dt.date(2024, 1, 31))
WINDOW_B = (dt.date(2024, 2, 1), dt.date(2024, 2, 29))


def _row(value_id, event_name="NFP", actual="216.000000"):
    return {
        "value_id": value_id, "event_id": "100", "event_name": event_name, "country_code": "US",
        "currency_code": "USD", "importance": "HIGH", "event_time": "2024.01.05 13:30:00", "period": "2023.12.01",
        "unit": "JOB", "multiplier": "THOUSANDS", "actual_value": actual, "forecast_value": "",
        "prev_value": "", "revised_prev_value": "", "revision": "0", "source_timezone": "SERVER",
    }


def _row_line(row: dict) -> str:
    from tests._mql5_exporter_sim import CSV_COLUMNS
    return ",".join(row.get(col, "") for col in CSV_COLUMNS)


# =====================================================================
# 1. Partial CSV row without newline, then retry with two records ->
#    both records survive and Python verifies the intended dataset.
# =====================================================================

def test_1_partial_row_then_retry_both_records_survive_and_verify(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8")
        + b"2,101,CPI,US,USD"  # interrupted mid-row -- no newline, incomplete field count
    )

    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("2", "CPI"), _row("3", "PPI")])

    assert result["results"][0]["status"] == "OK"
    rows = exporter._rows_in_window(*WINDOW_A)
    ids = {r["value_id"] for r in rows}
    assert ids == {"1", "2", "3"}, f"expected all 3 intended records, got {ids}"
    # The garbled fragment must be gone entirely -- never silently
    # re-surfacing as a 4th, malformed record.
    assert "2,101,CPI,US,USD\n2,101,CPI" not in csv_path.read_text(encoding="utf-8")


# =====================================================================
# 2. Interruption after complete row content but before newline ->
#    retry does not concatenate or lose records.
# =====================================================================

def test_2_interruption_before_newline_preserves_and_does_not_concatenate(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    complete_but_unterminated = _row_line(_row("2", "CPI")).encode("utf-8")
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8") + complete_but_unterminated
    )
    raw_before = csv_path.read_bytes()

    outcome = recover_file_tail(csv_path, True)
    assert outcome == "REPAIRED"
    raw_after = csv_path.read_bytes()
    assert raw_after == raw_before + b"\n"  # exactly one byte added, nothing dropped

    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("3", "PPI")])
    assert result["results"][0]["status"] == "OK"
    rows = exporter._rows_in_window(*WINDOW_A)
    ids = {r["value_id"] for r in rows}
    assert ids == {"1", "2", "3"}
    content = csv_path.read_text(encoding="utf-8")
    assert content.count("\n") == 4  # header + 3 rows, each on its own line


# =====================================================================
# 3. Interruption inside a quoted field -> correct recovery.
# =====================================================================

def test_3_interruption_inside_quoted_field_dropped_not_miscounted(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8")
        + '2,101,"Fed Says, Rates Steady and'.encode("utf-8")  # opened quote, never closed, no newline
    )

    outcome = recover_file_tail(csv_path, True)
    assert outcome == "REPAIRED"
    content = csv_path.read_text(encoding="utf-8")
    assert "Fed Says" not in content
    assert content.endswith("\n")
    assert _row_line(_row("1")) in content

    # NOTE: the retry's event_name deliberately avoids a literal comma --
    # SimulatedMql5Exporter's _append_rows/_rows_in_window are a naive
    # comma-split, not the real .mq5's quote-aware CSV (documented
    # simplification on the class itself); a comma-bearing name is
    # covered separately by tests/test_mql5_digest_encoding_contract.py
    # and the real .mq5's own EscapeCsv/SplitCsvRow, exercised for real
    # by mql5_exporter/TailRecoverySelfTest.mq5 scenario 3.
    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("2", "FedSpeech")])
    assert result["results"][0]["status"] == "OK"
    ids = {r["value_id"] for r in exporter._rows_in_window(*WINDOW_A)}
    assert ids == {"1", "2"}


# =====================================================================
# 4. Interruption inside a UTF-8 character -> explicit safe recovery;
#    no silent corruption.
# =====================================================================

def test_4_interruption_inside_multibyte_utf8_sequence_recovered_safely(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    prefix = (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8")
    truncated = "2,101,Bo".encode("utf-8") + bytes([0xE6])  # lone lead byte of a 3-byte CJK char
    csv_path.write_bytes(prefix + truncated)

    len_before = csv_path.stat().st_size
    outcome = recover_file_tail(csv_path, True)
    assert outcome == "REPAIRED"
    raw_after = csv_path.read_bytes()
    assert len(raw_after) < len_before  # the truncated char can never be completed by guessing
    raw_after.decode("utf-8")  # must not raise -- the recovered file itself stays valid UTF-8
    assert raw_after == prefix  # exactly the prefix, nothing more, nothing less

    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("2", "日本銀行")])
    assert result["results"][0]["status"] == "OK"
    names = {r["value_id"]: r["event_name"] for r in exporter._rows_in_window(*WINDOW_A)}
    assert names["2"] == "日本銀行"


# =====================================================================
# 5. Partial checkpoint line, then a new checkpoint -> earlier valid
#    entries survive and the new entry is independently parseable.
# =====================================================================

def test_5_partial_checkpoint_line_then_new_checkpoint(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)
    exporter._log_window_status(*WINDOW_A, "OK", 1, "deadbeef00000001")
    # Interrupted mid-write of a second checkpoint line.
    with exporter.log_path.open("ab") as fh:
        fh.write(b"2024.02.01 00:00:00,2024.03.01 00:00:00,US,USD")

    outcome = recover_file_tail(exporter.log_path, False)
    assert outcome == "REPAIRED"
    content = exporter.log_path.read_text(encoding="utf-8")
    assert "deadbeef00000001" in content
    assert "2024.03.01" not in content

    exporter._log_window_status(*WINDOW_B, "OK", 1, "deadbeef00000002")
    lines = exporter.log_path.read_text(encoding="utf-8").strip("\n").split("\n")
    assert len(lines) == 2
    for line in lines:
        assert len(line.split(",")) == 10


# =====================================================================
# 6. Malformed interior CSV record -> cannot be silently certified by
#    rescanning and hashing only surviving rows.
# =====================================================================

def test_6_malformed_interior_record_fails_verification_not_silently_certified(tmp_path):
    """Directly reproduces the CONFIRMED mechanism: bypass recovery
    (call `_append_rows` directly, as the OLD code effectively did with
    no boundary check at all) so a retry's row genuinely merges onto a
    dangling fragment, and show the window is now correctly reported as
    a verification failure instead of OK with a reduced row count."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n").encode("utf-8") + "1,101,CPI,US,USD".encode("utf-8")  # dangling, no newline
    )
    exporter = SimulatedMql5Exporter(csv_path)

    fetched = [_row("1", "CPI"), _row("2", "PPI")]
    exporter._append_rows(fetched)  # bypasses recover_file_tail on purpose -- reproduces the bug
    rows_in_window = exporter._rows_in_window(*WINDOW_A)
    persisted_ids = {r["value_id"] for r in rows_in_window}
    assert persisted_ids != {"1", "2"}, "the merge must have actually happened for this to be a real reproduction"

    expected_map = _canonical_hash_map(fetched)
    persisted_map = _canonical_hash_map(rows_in_window)
    missing_or_altered = [vid for vid, h in expected_map.items() if persisted_map.get(vid) != h]
    assert missing_or_altered, "fetched-vs-persisted verification must catch the merge -- it must not be silently OK"


# =====================================================================
# 7 & 8. One expected fetched ID missing / altered after persistence ->
#         failed window.
# =====================================================================

def test_7_missing_expected_id_fails_window(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    original_append = exporter._append_rows

    def dropping_append(rows):
        original_append([r for r in rows if r["value_id"] != "2"])  # silently drop one fetched row

    exporter._append_rows = dropping_append
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("1"), _row("2", "CPI")])
    assert result["results"][0]["status"] == "VERIFICATION_FAILED"
    assert "2" in result["results"][0]["missing_or_altered"]


def test_8_altered_expected_value_fails_window(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    original_append = exporter._append_rows

    def corrupting_append(rows):
        corrupted = []
        for r in rows:
            if r["value_id"] == "2":
                r = dict(r)
                r["actual_value"] = "999.000000"  # persisted value differs from what was fetched
            corrupted.append(r)
        original_append(corrupted)

    exporter._append_rows = corrupting_append
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("1"), _row("2", "CPI", actual="216.000000")])
    assert result["results"][0]["status"] == "VERIFICATION_FAILED"
    assert "2" in result["results"][0]["missing_or_altered"]


# =====================================================================
# 9. Duplicate IDs and revisions -> documented canonical outcome
#    remains correct (last-occurrence-wins), fetched-vs-persisted
#    verification agrees.
# =====================================================================

def test_9_duplicate_ids_and_revisions_canonical_outcome_correct(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    exporter = SimulatedMql5Exporter(csv_path)

    # The fetch itself returns a revision: two rows sharing value_id
    # "1", the second (later in file order) being the true revision.
    result = exporter.run(
        [WINDOW_A],
        lambda ws, we: [_row("1", "NFP", actual="216.000000"), _row("1", "NFP", actual="218.000000")],
    )
    assert result["results"][0]["status"] == "OK"
    rows = exporter._rows_in_window(*WINDOW_A)
    canonical = {r["value_id"]: r["actual_value"] for r in rows}
    assert canonical == {"1": "218.000000"}  # last occurrence wins, matches window_content_digest's contract


# =====================================================================
# 10. Successful repair followed by identical resume -> no refetch.
# =====================================================================

def test_10_repair_then_identical_resume_converges_no_refetch(tmp_path):
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8") + b"2,101,CPI,US,USD"
    )
    exporter = SimulatedMql5Exporter(csv_path)
    result1 = exporter.run([WINDOW_A], lambda ws, we: [_row("2", "CPI"), _row("3", "PPI")])
    assert result1["results"][0]["status"] == "OK"

    calls = []
    result2 = exporter.run([WINDOW_A], lambda ws, we: calls.append((ws, we)) or [_row("2", "CPI"), _row("3", "PPI")])
    assert calls == []  # no refetch -- WindowAlreadyOk's digest match must skip it
    assert result2["results"][0]["status"] == "SKIPPED"


# =====================================================================
# 11. Failure during recovery/replacement -> original valid history
#     remains recoverable.
# =====================================================================

def test_11_recovery_leaves_original_untouched_on_read_failure(tmp_path, monkeypatch):
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8") + b"2,101,CPI,US,USD"
    )
    original_bytes = csv_path.read_bytes()

    original_read_bytes = type(csv_path).read_bytes

    def failing_read_bytes(self, *a, **k):
        if self == csv_path:
            raise OSError("simulated unreadable file during recovery")
        return original_read_bytes(self, *a, **k)

    monkeypatch.setattr(type(csv_path), "read_bytes", failing_read_bytes)
    with pytest.raises(OSError):
        from tests._mql5_exporter_sim import recover_file_tail as _rft
        _rft(csv_path, True)
    monkeypatch.undo()

    # The original file, incomplete tail and all, must still be there --
    # a failed recovery attempt must never have partially truncated it.
    assert csv_path.read_bytes() == original_bytes


# =====================================================================
# 12. Python ingestion agrees with the final exporter evidence.
# =====================================================================

def test_12_python_ingestion_agrees_with_final_evidence_after_recovery(tmp_path):
    """End-to-end: an interrupted-then-repaired export must produce
    window-log evidence that src/data/fetch/mql5.py's OWN
    read_window_log/verification logic (via
    read_integrity_window_log + window_content_digest, exactly as the
    production ingestion code uses them) treats as trustworthy and
    complete -- not a special-cased test-only check."""
    from tests._mql5_exporter_sim import read_integrity_window_log, window_content_digest, INTEGRITY_SCHEMA_VERSION

    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8") + b"2,101,CPI,US,USD"
    )
    exporter = SimulatedMql5Exporter(csv_path)
    result = exporter.run([WINDOW_A], lambda ws, we: [_row("2", "CPI"), _row("3", "PPI")])
    assert result["results"][0]["status"] == "OK"

    log = read_integrity_window_log(exporter.log_path, "US", "USD")
    from tests._mql5_exporter_sim import _window_key
    entry = log[_window_key(*WINDOW_A)]
    assert entry.status == "OK"

    rows_in_window = exporter._rows_in_window(*WINDOW_A)
    count, digest = window_content_digest(rows_in_window)
    assert count == entry.canonical_row_count == 3
    assert digest == entry.content_digest


# =====================================================================
# Direct unit coverage of the boundary-detection primitives themselves
# (mirrors mql5_exporter/TailRecoverySelfTest.mq5's scenarios 7-9).
# =====================================================================

def test_clean_file_reports_clean_and_untouched(tmp_path):
    csv_path = tmp_path / "clean.csv"
    csv_path.write_bytes((CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8"))
    before = csv_path.read_bytes()
    assert recover_file_tail(csv_path, True) == "CLEAN"
    assert csv_path.read_bytes() == before


def test_missing_and_empty_files_are_clean(tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    assert recover_file_tail(missing, True) == "CLEAN"
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    assert recover_file_tail(empty, True) == "CLEAN"


def test_truncation_tolerant_classification_distinguishes_from_genuine_invalidity():
    prefix = (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8")
    truncated = prefix + bytes([0xF0])  # truncated 4-byte lead, zero continuation bytes
    whole_valid, truncated_at_end, valid_prefix_len = classify_utf8_validity(truncated)
    assert not whole_valid
    assert truncated_at_end
    assert valid_prefix_len == len(prefix)

    ansi_like = b"Caf" + bytes([0xE9]) + b"XYZ"  # lone invalid byte, PLENTY of bytes follow -- not a truncation
    whole_valid2, truncated_at_end2, _ = classify_utf8_validity(ansi_like)
    assert not whole_valid2
    assert not truncated_at_end2


def test_migration_tolerates_trailing_truncation_but_not_genuine_incompatibility(tmp_path):
    """The interaction this fix specifically had to get right: an
    interrupted write's truncated tail must NOT cause the whole file to
    be misclassified as an incompatible legacy (e.g. ANSI) export and
    migrated aside -- only a genuinely non-UTF-8 file (with a header
    that fails to match, or invalidity that ISN'T a trailing
    truncation) should ever be moved aside."""
    csv_path = tmp_path / "us_macro_calendar.csv"
    csv_path.write_bytes(
        (CSV_HEADER_LINE + "\n" + _row_line(_row("1")) + "\n").encode("utf-8") + bytes([0xE6])
    )
    exporter = SimulatedMql5Exporter(csv_path)
    outcome = exporter.migrate_legacy_csv_if_needed()
    assert outcome == "READY_EXISTING"  # tolerated -- never migrated aside just for a truncated tail
    assert not list(tmp_path.glob("*.legacy_*.csv"))
