"""Static checks on the .mq5 exporter source.

MetaTrader is not available in this environment, so the exporter cannot
be compiled or run here (see its header comment and the final report --
this is explicitly unverified at runtime, including `FileMove`'s exact
signature). These tests only confirm the source text implements the
documented fixes; they are NOT a substitute for running it inside a
real MT5 terminal.
"""
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / "mql5_exporter" / "EconomicCalendarExporter.mq5").read_text(encoding="utf-8")
# Eighth review: UTF-8 file I/O, strict UTF-8 validation, CSV row
# splitting, the FNV-1a digest, and interrupted-write tail recovery
# moved into a shared #include so self-tests can call the ACTUAL
# production implementation instead of a duplicated copy -- see
# CalendarExporterCore.mqh's own header comment. Checks against those
# specific pieces of code now read CORE_SOURCE instead of SOURCE.
CORE_SOURCE = (Path(__file__).parents[1] / "mql5_exporter" / "CalendarExporterCore.mqh").read_text(encoding="utf-8")


def test_uses_fixed_1_000_000_scale_not_digits_based_power():
    assert "MQL5_CALENDAR_VALUE_SCALE 1000000.0" in SOURCE
    assert "actual_value / MQL5_CALENDAR_VALUE_SCALE" in SOURCE
    assert "forecast_value / MQL5_CALENDAR_VALUE_SCALE" in SOURCE
    assert "prev_value / MQL5_CALENDAR_VALUE_SCALE" in SOURCE
    assert "revised_prev_value / MQL5_CALENDAR_VALUE_SCALE" in SOURCE
    # The old, wrong per-event-digits scaling must be gone (the phrase
    # itself may still appear in an explanatory comment -- check for the
    # actual division usage, not just any substring match).
    assert "values[i].actual_value / MathPow" not in SOURCE
    assert "double scale = MathPow" not in SOURCE


def test_preserves_long_min_as_missing():
    assert "!= LONG_MIN" in SOURCE
    assert SOURCE.count("!= LONG_MIN") >= 4


def test_unit_and_multiplier_use_distinct_enum_types():
    """Regression: a prior version declared a single
    ENUM_CALENDAR_EVENT_UNIT variable, assigned ev.multiplier (a
    DIFFERENT enum, ENUM_CALENDAR_EVENT_MULTIPLIER) into it, and printed
    it with the unit-labeling function -- a type mismatch."""
    assert "ENUM_CALENDAR_EVENT_UNIT eventUnit" in SOURCE
    assert "ENUM_CALENDAR_EVENT_MULTIPLIER eventMultiplier" in SOURCE
    assert "eventUnit = ev.unit" in SOURCE
    assert "eventMultiplier = ev.multiplier" in SOURCE
    assert "string UnitToString(ENUM_CALENDAR_EVENT_UNIT" in SOURCE
    assert "string MultiplierToString(ENUM_CALENDAR_EVENT_MULTIPLIER" in SOURCE
    # The old buggy signature (multiplier typed as the UNIT enum) must be gone.
    assert "MultiplierToString(ENUM_CALENDAR_EVENT_UNIT" not in SOURCE
    # Both are actually used when building the output line.
    assert "UnitToString(eventUnit)" in SOURCE
    assert "MultiplierToString(eventMultiplier)" in SOURCE


def test_multiplier_enum_values_are_the_real_multiplier_constants():
    for const in ("CALENDAR_MULTIPLIER_THOUSANDS", "CALENDAR_MULTIPLIER_MILLIONS",
                  "CALENDAR_MULTIPLIER_BILLIONS", "CALENDAR_MULTIPLIER_TRILLIONS", "CALENDAR_MULTIPLIER_NONE"):
        assert const in SOURCE


def test_period_field_preserved_for_reference_period_alignment():
    assert "values[i].period" in SOURCE


def test_output_file_opened_in_append_mode_when_it_already_exists():
    # Sixth review: FILE_CSV|FILE_ANSI text mode replaced with FILE_BIN +
    # explicit UTF-8 conversion (FILE_ANSI/FILE_UNICODE cannot produce
    # UTF-8 bytes) -- see WriteUtf8Bytes/ReadUtf8File and the module
    # docstring's "WINDOW INTEGRITY CONTRACT". Seeking to end-of-file is
    # now done via FileSize + FileSeek(..., SEEK_SET), with the return
    # value checked, rather than the unchecked SEEK_END form.
    assert "FILE_READ | FILE_WRITE | FILE_BIN" in SOURCE
    # FILE_ANSI/FILE_UNICODE text-mode flags are gone from every actual
    # FileOpen call (the flag names may still appear in explanatory
    # comments about why they're no longer used).
    assert "| FILE_ANSI" not in SOURCE
    assert "| FILE_UNICODE" not in SOURCE
    assert "FileSeek(fileHandle, (long)endPos, SEEK_SET)" in SOURCE


def test_durable_per_window_status_log_exists():
    assert "WindowLogFile" in SOURCE
    assert "LogWindowStatus" in SOURCE
    assert "WindowAlreadyOk" in SOURCE


def test_value_id_and_revision_preserved_in_output():
    assert "values[i].id" in SOURCE
    assert "values[i].revision" in SOURCE


# -- second-review fixes: exact window-boundary checkpoint identity --

def test_window_log_keyed_by_exact_boundaries_not_year_month():
    # Sixth review: LogWindowStatus now returns bool (its own write
    # success/failure, checked by FetchMonthWindow) rather than void.
    assert "bool LogWindowStatus(datetime winStart, datetime winEnd" in SOURCE
    # Renamed to winEndExclusive (fifth review) when WindowAlreadyOk
    # became digest-verified rather than pure boolean-log lookup -- the
    # exact-boundary keying itself (datetime start/end params) is unchanged.
    assert "bool WindowAlreadyOk(datetime winStart, datetime winEndExclusive)" in SOURCE
    # The old (year, month)-only signature must be gone.
    assert "void LogWindowStatus(int year, int month" not in SOURCE
    assert "bool LogWindowStatus(int year, int month" not in SOURCE
    assert "bool WindowAlreadyOk(int year, int month)" not in SOURCE


def test_window_log_includes_country_currency_and_schema_version():
    assert "EXPORT_SCHEMA_VERSION" in SOURCE
    assert "logCountry == CountryCode" in SOURCE
    assert "logCurrency == CurrencyCode" in SOURCE
    assert "logSchema == EXPORT_SCHEMA_VERSION" in SOURCE


def test_window_log_filename_is_schema_versioned():
    assert 'WindowLogFile = OutputFile + ".windows.v" + EXPORT_SCHEMA_VERSION + ".csv"' in SOURCE


def test_flushes_before_recording_success():
    assert "FileFlush(fileHandle)" in SOURCE
    # The flush must happen before LogWindowStatus(..., "OK", ...) is called
    # for a successful window -- check ordering within FetchMonthWindow.
    fetch_fn_start = SOURCE.index("int FetchMonthWindow(")
    fetch_fn_end = SOURCE.index("\n}\n", fetch_fn_start)
    body = SOURCE[fetch_fn_start:fetch_fn_end]
    assert body.index("FileFlush(fileHandle)") < body.rindex('LogWindowStatus(winStart, winEnd, "OK"')


def test_legacy_csv_migration_function_exists_and_is_called():
    # Fifth review: returns an explicit ENUM_MIGRATION_OUTCOME (not a
    # bare bool) so "compatible header, append" and "migration failed"
    # can no longer collapse into the same caller-visible value -- see
    # tests/test_mql5_migration_control_flow.py for the control-flow
    # regression this fixes.
    assert "ENUM_MIGRATION_OUTCOME MigrateLegacyCsvIfNeeded()" in SOURCE
    assert "MIGRATION_READY_EXISTING" in SOURCE
    assert "MIGRATION_READY_NEW" in SOURCE
    assert "MIGRATION_ERROR" in SOURCE
    assert "MigrateLegacyCsvIfNeeded()" in SOURCE.split("ENUM_MIGRATION_OUTCOME MigrateLegacyCsvIfNeeded()", 1)[1]
    assert "FileMove(OutputFile" in SOURCE
    assert "needFreshFile" in SOURCE
    # The old bare-bool signature (the confirmed control-flow defect) must be gone.
    assert "bool MigrateLegacyCsvIfNeeded()" not in SOURCE


def test_migration_error_aborts_before_any_fetch_or_log_write():
    """Static ordering check: OnStart() must check MIGRATION_ERROR and
    `return` BEFORE opening OutputFile for append/write, matching the
    fixed control flow (see MigrateLegacyCsvIfNeeded's docstring)."""
    onstart_start = SOURCE.index("void OnStart()")
    body = SOURCE[onstart_start:]
    error_check_pos = body.index("migrationOutcome == MIGRATION_ERROR")
    first_file_open_pos = body.index("FileOpen(OutputFile")
    assert error_check_pos < first_file_open_pos


# -- sixth review: file-handle ownership during resume verification --

def test_window_already_ok_decided_before_any_writer_is_opened():
    """Static ordering check for the confirmed defect: OnStart() must
    finish deciding skip-vs-fetch for EVERY window (via WindowAlreadyOk,
    which reads OutputFile) BEFORE opening OutputFile for append/write --
    not interleaved, with a writer held open across the whole decision
    loop, as before."""
    onstart_start = SOURCE.index("void OnStart()")
    body = SOURCE[onstart_start:]
    decision_call_pos = body.rindex("WindowAlreadyOk(windowStart, windowEnd)")
    writer_open_pos = body.index("FileOpen(OutputFile", decision_call_pos)
    assert decision_call_pos < writer_open_pos


def test_rescan_never_runs_while_a_write_handle_is_open_in_fetch_month_window():
    """FetchMonthWindow must close its write handle before calling
    RescanWindowDigest (which independently opens OutputFile for
    reading) and reopen only afterward."""
    fn_start = SOURCE.index("int FetchMonthWindow(")
    fn_end = SOURCE.index("\n}\n", fn_start)
    body = SOURCE[fn_start:fn_end]
    close_pos = body.index("FileClose(fileHandle)")
    rescan_pos = body.index("RescanWindowDigest(winStart, winEnd, canonicalCount, digest, persistedIds, persistedHashes)")
    reopen_pos = body.index("FileOpen(OutputFile", rescan_pos)
    assert close_pos < rescan_pos < reopen_pos


def test_file_open_close_reopen_seek_results_are_checked():
    assert "== INVALID_HANDLE" in SOURCE
    assert "bool seekOk" in SOURCE
    assert "FileSeek(" in SOURCE
    # Seventh review, defect B: a failed seek on the reopened handle must
    # be folded into `fileHandle` itself (closed + set to INVALID_HANDLE)
    # BEFORE the caller's single `fileHandle == INVALID_HANDLE` guard --
    # never left as a live-but-mispositioned handle that guard can't see.
    fn_start = SOURCE.index("int FetchMonthWindow(")
    fn_end = SOURCE.index("\n}\n", fn_start)
    body = SOURCE[fn_start:fn_end]
    reopen_pos = body.index("int reopened = FileOpen(OutputFile, FILE_READ | FILE_WRITE | FILE_BIN);")
    seek_pos = body.index("seekOk = FileSeek(reopened", reopen_pos)
    invalidate_pos = body.index("reopened = INVALID_HANDLE;", seek_pos)
    assign_pos = body.index("fileHandle = reopened;", invalidate_pos)
    # Eighth review, P1: the guard now also folds in fetchVerified (the
    # fetched-vs-persisted cross-check), computed BEFORE the reopen --
    # a missing/altered record must block success exactly like a failed
    # rescan/reopen/seek does.
    guard_pos = body.index("if(!rescanOk || !fetchVerified || fileHandle == INVALID_HANDLE)", assign_pos)
    assert reopen_pos < seek_pos < invalidate_pos < assign_pos < guard_pos


def test_never_continues_writing_through_an_invalid_handle():
    onstart_start = SOURCE.index("void OnStart()")
    body = SOURCE[onstart_start:]
    assert "if(fileHandle == INVALID_HANDLE)" in body
    assert "never continue writing through an invalid handle" in body.lower()


# -- sixth review: UTF-8 encoding contract --

def test_digest_hashes_utf8_bytes_not_string_get_character():
    """Fnv1a64 must hash real UTF-8 bytes (via StringToCharArray) -- the
    original defect hashed StringGetCharacter's UTF-16 code units,
    which differ from UTF-8 bytes for any non-ASCII character. Fnv1a64
    now lives in CalendarExporterCore.mqh (eighth review) -- shared,
    unmodified, by EconomicCalendarExporter.mq5 and every self-test."""
    fn_start = CORE_SOURCE.index("ulong Fnv1a64(string s)")
    fn_end = CORE_SOURCE.index("\n}\n", fn_start)
    body = CORE_SOURCE[fn_start:fn_end]
    assert "StringToCharArray(s, bytes, 0, -1, UTF8_CODEPAGE)" in body
    assert "StringGetCharacter" not in body


def test_string_to_char_array_null_terminator_is_excluded_everywhere():
    """Every ACTUAL CODE call site of StringToCharArray(..., UTF8_CODEPAGE)
    (excluding mentions inside `//` comments), across both the exporter
    and its shared CalendarExporterCore.mqh include, must exclude the
    trailing NUL terminator it appends -- both from what is written to
    disk and from what is hashed."""
    combined_source = SOURCE + "\n" + CORE_SOURCE
    code_lines = [line for line in combined_source.splitlines() if "StringToCharArray(" in line and not line.strip().startswith("//")]
    assert len(code_lines) >= 2  # WriteUtf8Bytes and Fnv1a64, at minimum
    for line in code_lines:
        assert "UTF8_CODEPAGE" in line, f"StringToCharArray call not using UTF8_CODEPAGE: {line!r}"
    for pos, _ in enumerate(code_lines):
        idx = combined_source.index(code_lines[pos])
        window = combined_source[idx:idx + 300]
        assert "n - 1" in window, f"no NUL-terminator exclusion found near {code_lines[pos]!r}"


def test_no_file_ansi_or_file_unicode_text_mode_used_anywhere():
    assert "| FILE_ANSI" not in SOURCE
    assert "| FILE_UNICODE" not in SOURCE
    assert "FILE_BIN" in SOURCE


def test_file_write_array_return_value_is_checked():
    # WriteUtf8Bytes/WriteUtf8Line now live in CalendarExporterCore.mqh
    # (eighth review) -- shared, unmodified, by the exporter and every
    # self-test.
    assert "uint written = FileWriteArray" in CORE_SOURCE
    assert "(int)written == len" in CORE_SOURCE or "written == 1" in CORE_SOURCE
