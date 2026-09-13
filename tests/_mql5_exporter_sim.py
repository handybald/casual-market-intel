"""Pure-Python line-for-line simulation of the date-window chunking
logic in mql5_exporter/EconomicCalendarExporter.mq5's OnStart() loop
(MonthStart/NextMonthStart/DayStart/NextDayStart + the while loop) --
used ONLY so tests can prove Python's window-key reconstruction
(`_window_key_for_chunk` in src/data/fetch/mql5.py) genuinely agrees
with what the exporter computes for a given (StartDate, EndDate), since
no MetaTrader compiler/runtime is available in this environment to
build and run the real .mq5 file.

This is a documentation/behavioral cross-check, NOT a substitute for
actually compiling and running the real script inside MetaTrader --
see the final report's fixture-tested vs. live-tested distinction.

Not a test module itself (no `test_` prefix).
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def day_start(t: dt.datetime) -> dt.datetime:
    return dt.datetime(t.year, t.month, t.day)


def next_day_start(d: dt.datetime) -> dt.datetime:
    return d + dt.timedelta(days=1)


def month_start(t: dt.datetime) -> dt.datetime:
    return dt.datetime(t.year, t.month, 1)


def next_month_start(d: dt.datetime) -> dt.datetime:
    if d.month == 12:
        return dt.datetime(d.year + 1, 1, 1)
    return dt.datetime(d.year, d.month + 1, 1)


CURRENT_CSV_HEADER = (
    "value_id,event_id,event_name,country_code,currency_code,importance,event_time,"
    "period,unit,multiplier,actual_value,forecast_value,prev_value,revised_prev_value,"
    "revision,source_timezone"
)

# The schema immediately before EXPORT_SCHEMA_VERSION "2" added `period`
# -- it already had `value_id` as its first column (added in an even
# earlier revision), so checking only the first field cannot tell it
# apart from the current schema.
PREVIOUS_CSV_HEADER_MISSING_PERIOD = (
    "value_id,event_id,event_name,country_code,currency_code,importance,event_time,"
    "unit,multiplier,actual_value,forecast_value,prev_value,revised_prev_value,"
    "revision,source_timezone"
)

# The oldest schema, before `value_id` existed at all.
OLDEST_CSV_HEADER_NO_VALUE_ID = (
    "event_id,event_name,country_code,currency_code,importance,event_time,"
    "unit,multiplier,actual_value,forecast_value,prev_value,revised_prev_value,revision,source_timezone"
)


def migrate_legacy_csv_first_field_only(existing_header: str, current_header: str) -> bool:
    """Mirrors the ORIGINAL (buggy) MigrateLegacyCsvIfNeeded(): true means
    "append in place, treated as current schema". Compared only the
    first CSV field."""
    existing_first_field = existing_header.split(",")[0] if existing_header else ""
    current_first_field = current_header.split(",")[0]
    return existing_first_field == current_first_field


def migrate_legacy_csv_full_header(existing_header: str, current_header: str) -> bool:
    """Mirrors the FIXED MigrateLegacyCsvIfNeeded(): true means "append
    in place, treated as current schema". Compares the entire header
    line verbatim -- any difference (missing/reordered/extra column,
    truncated/malformed header) triggers safe migrate-aside instead."""
    return existing_header == current_header


# ---------------------------------------------------------------------------
# Integrity contract simulation (fifth review, issues #1 and #2).
#
# No MetaTrader compiler/runtime exists in this environment, so this is a
# faithful line-for-line Python mirror of the REDESIGNED .mq5 file's
# control flow (MigrateLegacyCsvIfNeeded's three-way outcome, OnStart's
# abort-on-error handling, FetchMonthWindow's rescan-and-digest, and
# WindowAlreadyOk's digest-verified skip decision) -- used to prove the
# DESIGN is correct and to generate realistic v4 CSV + window-log
# fixtures for testing the real, production `src/data/fetch/mql5.py`
# ingestion side. It is a documentation/behavioral simulation, never a
# substitute for actually compiling and running the .mq5 file.
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "value_id", "event_id", "event_name", "country_code", "currency_code", "importance",
    "event_time", "period", "unit", "multiplier", "actual_value", "forecast_value",
    "prev_value", "revised_prev_value", "revision", "source_timezone",
]
CSV_HEADER_LINE = ",".join(CSV_COLUMNS)

INTEGRITY_SCHEMA_VERSION = "5"  # window-log format version -- must match MQL5_EXPORT_SCHEMA_VERSION in src/data/fetch/mql5.py

_FNV_OFFSET_BASIS_64 = 0xCBF29CE484222325
_FNV_PRIME_64 = 0x100000001B3
_MASK_64 = 0xFFFFFFFFFFFFFFFF


def fnv1a_64(data: bytes) -> int:
    """A small, from-scratch 64-bit FNV-1a hash -- deliberately NOT a
    cryptographic hash (no MQL5 crypto library call is used, to avoid
    ANY dependency whose exact API/behavior cannot be verified without a
    compiler). Uses only 64-bit unsigned multiply/xor, which both MQL5
    (`ulong`) and Python support identically (wrap-on-overflow modulo
    2**64), so the SAME algorithm is independently implementable on
    both sides of the contract."""
    h = _FNV_OFFSET_BASIS_64
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME_64) & _MASK_64
    return h


def canonical_row_string(row: Dict[str, str]) -> str:
    """Deterministic serialization of one CSV row's raw field text (NOT
    reformatted/reparsed) in CSV_COLUMNS order, joined by the ASCII unit
    separator (0x1F) -- a character that cannot appear in any of these
    fields under normal export conditions, so no field-boundary
    ambiguity is possible."""
    return "\x1f".join(row.get(col, "") or "" for col in CSV_COLUMNS)


def window_content_digest(rows: List[Dict[str, str]]) -> Tuple[int, str]:
    """The per-window integrity evidence: (canonical_row_count, digest).

    CANONICAL records, not physical rows: deduplicated by `value_id`,
    keeping the LAST occurrence in file order. This is the explicit
    design decision for overlapping requests/reruns/revisions -- an
    overlapping re-fetch that re-writes the same value_ids is not
    "extra" data, and a genuine revision (same value_id, updated
    fields, appended later) is reflected by its newest occurrence only.

    Order-independent: per-row hashes are XOR-combined, so the result
    does not depend on which occurrence of a duplicated value_id came
    first in the file, or on dict/array iteration order in whichever
    language recomputes it.
    """
    canonical: Dict[str, Dict[str, str]] = {}
    for row in rows:
        canonical[row["value_id"]] = row  # last occurrence wins
    combined = 0
    for row in canonical.values():
        combined ^= fnv1a_64(canonical_row_string(row).encode("utf-8"))
    return len(canonical), f"{combined:016x}"


# ---------------------------------------------------------------------------
# Interrupted-write tail recovery (eighth review, P1) -- Python mirror of
# CalendarExporterCore.mqh's IsValidUtf8Ex/ComputeSafeAppendBoundary/
# RecoverFileTail, independently compiled and RUN inside a real MetaTrader
# terminal this round (mql5_exporter/TailRecoverySelfTest.mq5, 34/34
# scenarios passed). This is a documentation/behavioral cross-check of that
# real, compiled evidence -- and, separately, the shared test infrastructure
# SimulatedMql5Exporter.run() below uses to reproduce the confirmed defect
# (an interrupted write's retry silently merging onto a dangling fragment,
# certified OK anyway by a digest of only the rows that still parse) against
# the real, production ingestion code in src/data/fetch/mql5.py.
# ---------------------------------------------------------------------------

def classify_utf8_validity(raw: bytes) -> Tuple[bool, bool, int]:
    """Mirrors IsValidUtf8Ex using Python's OWN strict UTF-8 codec as the
    ground truth (rather than re-deriving RFC 3629 a second time in
    Python): returns (whole_buffer_valid, truncated_at_end,
    valid_prefix_len). `truncated_at_end` is true only when the sole
    problem is the buffer running out of bytes partway through an
    otherwise well-formed final sequence -- Python's codec reports this
    precisely via UnicodeDecodeError.reason == "unexpected end of data"
    at the very end of the buffer, distinct from any other decode
    failure (bad lead/continuation byte, overlong encoding, surrogate,
    out-of-range), which is never treated as a tolerable interruption.
    """
    try:
        raw.decode("utf-8", errors="strict")
        return True, False, len(raw)
    except UnicodeDecodeError as exc:
        truncated_at_end = (exc.reason == "unexpected end of data" and exc.end == len(raw))
        return False, truncated_at_end, exc.start


def _split_csv_row_strict(line: str) -> Optional[List[str]]:
    """Mirrors SplitCsvRow: quote-aware, returns None unless the row has
    EXACTLY len(CSV_COLUMNS) fields and every opened quote was closed
    (a row ending while still inside a quoted field -- an interrupted
    write stopped mid-field -- is never treated as complete)."""
    fields: List[str] = []
    current: List[str] = []
    in_quotes = False
    n = len(line)
    i = 0
    while i < n:
        ch = line[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    current.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not current:
            in_quotes = True
            i += 1
            continue
        if ch == ",":
            if len(fields) >= len(CSV_COLUMNS):
                return None
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if in_quotes:
        return None
    if len(fields) >= len(CSV_COLUMNS):
        return None
    fields.append("".join(current))
    if len(fields) != len(CSV_COLUMNS):
        return None
    return fields


def _is_complete_tail_record(tail: bytes, is_csv_format: bool) -> bool:
    valid, truncated_at_end, _ = classify_utf8_validity(tail)
    if not valid:
        return False  # includes a truncated-at-end tail -- ALWAYS incomplete on its own
    text = tail.decode("utf-8")
    if is_csv_format:
        return _split_csv_row_strict(text) is not None
    return len(text.split(",")) == 10


def compute_safe_append_boundary(raw: bytes, is_csv_format: bool) -> Tuple[int, bool]:
    """Mirrors ComputeSafeAppendBoundary. Returns (safe_offset,
    last_line_complete) -- `last_line_complete=False` signals the
    caller must append a single '\\n' byte (a structurally complete
    record was found, just missing its terminator) rather than drop
    anything."""
    last_nl = raw.rfind(b"\n")
    tail_start = last_nl + 1
    tail = raw[tail_start:]
    if not tail:
        return len(raw), True
    if _is_complete_tail_record(tail, is_csv_format):
        return len(raw), False
    return tail_start, True


def recover_file_tail(path: Path, is_csv_format: bool) -> str:
    """Mirrors RecoverFileTail. Returns "CLEAN" | "REPAIRED". Never
    truncates `path` in place -- a dropped tail goes through a
    brand-new sibling file, written and then atomically substituted in
    (`Path.replace`, POSIX-atomic), exactly mirroring the real .mq5
    fix's temp-file-then-FileMove(FILE_REWRITE) procedure."""
    if not path.exists():
        return "CLEAN"
    raw = path.read_bytes()
    if len(raw) == 0:
        return "CLEAN"
    safe_offset, last_line_complete = compute_safe_append_boundary(raw, is_csv_format)
    if safe_offset == len(raw) and last_line_complete:
        return "CLEAN"
    if safe_offset == len(raw) and not last_line_complete:
        path.write_bytes(raw + b"\n")
        return "REPAIRED"
    tmp = path.with_name(path.name + ".recover_tmp")
    tmp.write_bytes(raw[:safe_offset])
    tmp.replace(path)
    return "REPAIRED"


def _canonical_hash_map(rows: List[Dict[str, str]]) -> Dict[str, int]:
    """Canonical (last-occurrence-wins by value_id) id -> row-hash map --
    the per-id building block `window_content_digest` XOR-combines into
    a single digest. Exposed separately so a caller can compare WHICH
    specific ids are missing/altered between two row sets (used by
    `run()`'s fetched-vs-persisted verification), not just whether
    their combined digests happen to differ."""
    canonical: Dict[str, Dict[str, str]] = {}
    for row in rows:
        canonical[row["value_id"]] = row
    return {vid: fnv1a_64(canonical_row_string(row).encode("utf-8")) for vid, row in canonical.items()}


def _parse_event_date(event_time_raw: str) -> dt.date:
    return dt.datetime.strptime(event_time_raw, "%Y.%m.%d %H:%M:%S").date()


def _window_key(window_start: dt.date, window_end_inclusive: dt.date) -> Tuple[str, str]:
    start_str = f"{window_start:%Y.%m.%d} 00:00:00"
    end_exclusive = window_end_inclusive + dt.timedelta(days=1)
    end_str = f"{end_exclusive:%Y.%m.%d} 00:00:00"
    return start_str, end_str


class WindowLogEntry:
    __slots__ = ("status", "canonical_row_count", "content_digest")

    def __init__(self, status: str, canonical_row_count: int, content_digest: str):
        self.status = status
        self.canonical_row_count = canonical_row_count
        self.content_digest = content_digest


def read_integrity_window_log(log_path: Path, country: str, currency: str) -> Dict[Tuple[str, str], WindowLogEntry]:
    """Mirrors the FIXED read_window_log(): validates the full 10-field
    v4 row shape (country/currency/schema match, a parseable integer
    row count, a non-empty digest) -- any row that doesn't fully
    validate is simply not present in the returned map (never partially
    trusted). Last matching row per key wins."""
    if not log_path.exists():
        return {}
    entries: Dict[Tuple[str, str], WindowLogEntry] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split(",")
        if len(fields) != 10:
            continue  # malformed/incomplete/old-schema row -- never certifies completion
        win_start, win_end, log_country, log_currency, schema_version, status, row_count_raw, _attempts, _logged_at, digest = fields
        if log_country != country or log_currency != currency:
            continue
        if schema_version != INTEGRITY_SCHEMA_VERSION:
            continue
        try:
            row_count = int(row_count_raw)
        except ValueError:
            continue
        if not digest:
            continue
        entries[(win_start, win_end)] = WindowLogEntry(status, row_count, digest)
    return entries


class FileHandleConflictError(RuntimeError):
    """Raised by ExclusiveFileHandleRegistry when a path already has an
    open handle and a second one is requested concurrently.

    Models the assumption stated in the real .mq5 file's own header
    comment: two simultaneously open handles to the SAME path are NOT
    assumed safe in MQL5. Ordinary Python file I/O does not enforce
    this on its own (multiple handles to one path are perfectly fine
    at the OS level) -- without this registry, the simulator would
    silently tolerate the exact defect (issue #1, sixth review) it
    needs to be able to detect: a writer held open on OutputFile while
    WindowAlreadyOk's RescanWindowDigest tries to independently open
    the same path for reading.
    """


class ExclusiveFileHandleRegistry:
    """Tracks currently "open" paths for SimulatedMql5Exporter. Every
    file-touching operation must acquire (and release) a handle through
    this registry -- acquiring a path that's already held raises
    FileHandleConflictError, exactly modeling "no two concurrently open
    handles to the same path"."""

    def __init__(self):
        self._open: Dict[str, str] = {}

    @contextmanager
    def acquire(self, path, mode: str):
        key = str(path)
        if key in self._open:
            raise FileHandleConflictError(
                f"{path} already has an open handle (mode={self._open[key]!r}) -- "
                f"cannot open again (mode={mode!r}) concurrently"
            )
        self._open[key] = mode
        try:
            yield
        finally:
            del self._open[key]


class SimulatedMql5Exporter:
    """Mirrors the REDESIGNED .mq5 file end to end: MigrateLegacyCsvIfNeeded
    (three-way outcome), OnStart's abort-on-error control flow,
    FetchMonthWindow's write-then-rescan-then-digest-then-log sequence,
    and WindowAlreadyOk's digest-verified skip decision. Operates on
    real temp files so it can be used both to test this DESIGN directly
    and to produce realistic fixtures for testing the real Python
    ingestion side against.

    SIMPLIFICATION vs. the real .mq5 file: `_rows_in_window` here
    tokenizes CSV rows with a naive comma-split, not full CSV-quote-
    aware parsing -- the real .mq5 file's `RescanWindowDigest` reads via
    FILE_CSV mode (quote-aware, matching Python's `csv` module), and the
    real Python production code (`_read_raw_csv_rows` in
    src/data/fetch/mql5.py) uses the standard `csv` module. This
    simplification only matters for a row whose `event_name` contains a
    literal comma (which triggers CSV quoting) -- none of this harness's
    test fixtures do that. It does not affect the digest/dedup
    ALGORITHM under test, only this harness's own tokenizing.
    """

    def __init__(self, output_csv: Path, country: str = "US", currency: str = "USD"):
        self.output_csv = output_csv
        self.log_path = output_csv.with_name(output_csv.name + f".windows.v{INTEGRITY_SCHEMA_VERSION}.csv")
        self.country = country
        self.currency = currency
        # Every file-touching operation below acquires a handle through
        # this registry -- see ExclusiveFileHandleRegistry/
        # FileHandleConflictError for why (issue #1, sixth review).
        self._handles = ExclusiveFileHandleRegistry()

    # -- MigrateLegacyCsvIfNeeded mirror --------------------------------
    def migrate_legacy_csv_if_needed(self, force_migration_failure: bool = False, force_read_failure: bool = False) -> str:
        """Returns "READY_EXISTING" | "READY_NEW" | "ERROR" -- mirrors the
        FIXED MQL5 ENUM_MIGRATION_OUTCOME (seventh review, defect C):
        the existing file's RAW BYTES must be valid UTF-8 (checked here
        via Python's own `bytes.decode("utf-8", errors="strict")` as
        ground truth -- the real .mq5 file has its own from-scratch
        byte-level validator, `IsValidUtf8`, independently compiled and
        run against synthetic ASCII/valid-UTF-8/ANSI/truncated vectors
        in mql5_exporter/FileRecoverySelfTest.mq5) BEFORE its decoded
        header text is ever compared -- a v4-or-earlier ANSI file's
        header is pure ASCII and is otherwise indistinguishable from a
        genuine v5 UTF-8 file by header text alone. See
        `migrate_legacy_csv_if_needed_old_buggy` for the pre-fix
        behavior this replaces. `force_read_failure`/
        `force_migration_failure` simulate a FileOpen/FileMove failure
        this Python harness cannot organically reproduce on a real
        filesystem but which the real .mq5 code must still handle safely.
        """
        if not self.output_csv.exists():
            return "READY_NEW"
        if force_read_failure:
            return "ERROR"
        with self._handles.acquire(self.output_csv, "read"):
            raw = self.output_csv.read_bytes()

        # TRUNCATION-TOLERANT (eighth review, P1): an interrupted write
        # can leave the file's LAST byte sequence cut short mid-
        # multibyte-character. A plain whole-buffer strict-decode check
        # would then reject the ENTIRE file (every earlier, perfectly
        # valid record included) as "incompatible" and migrate the
        # whole history aside needlessly. classify_utf8_validity
        # distinguishes that specific case (truncated_at_end) from
        # every OTHER kind of invalidity (still genuinely incompatible,
        # e.g. real ANSI content) -- only the confirmed-valid PREFIX is
        # ever decoded for the header check; recover_file_tail (called
        # by `run()` right after migration) repairs the truncated tail
        # itself afterward.
        whole_valid, truncated_at_end, valid_prefix_len = classify_utf8_validity(raw)
        safe_to_trust_prefix = whole_valid or truncated_at_end
        decode_len = len(raw) if whole_valid else valid_prefix_len
        content = raw[:decode_len].decode("utf-8") if safe_to_trust_prefix and decode_len > 0 else ""
        existing_header = content.splitlines()[0] if content else ""
        if safe_to_trust_prefix and existing_header == CSV_HEADER_LINE:
            return "READY_EXISTING"
        if force_migration_failure:
            return "ERROR"
        legacy_name = self.output_csv.with_name(self.output_csv.name + ".legacy_TESTMOVE.csv")
        self.output_csv.rename(legacy_name)
        return "READY_NEW"

    def migrate_legacy_csv_if_needed_old_buggy(self, force_migration_failure: bool = False, force_read_failure: bool = False) -> str:
        """The ORIGINAL (pre-seventh-review) migration check: compares
        ONLY the decoded header text, via a LENIENT decode
        (`errors="replace"`) that tolerates invalid UTF-8 bytes
        elsewhere in the file -- an honest stand-in for whatever
        lenient behavior the real .mq5 `CharArrayToString(...,
        UTF8_CODEPAGE)` has on malformed input (that specific lenient
        behavior was not itself empirically forced; what IS verified is
        the defect's observable consequence below: a pure-ASCII header
        is accepted regardless of whether the rest of the file is valid
        UTF-8). Used ONLY to demonstrate the confirmed defect -- not the
        production behavior."""
        if not self.output_csv.exists():
            return "READY_NEW"
        if force_read_failure:
            return "ERROR"
        with self._handles.acquire(self.output_csv, "read"):
            raw = self.output_csv.read_bytes()
        content = raw.decode("utf-8", errors="replace")
        existing_header = content.splitlines()[0] if content else ""
        if existing_header == CSV_HEADER_LINE:
            return "READY_EXISTING"  # THE BUG: accepted regardless of whether `raw` is valid UTF-8
        if force_migration_failure:
            return "ERROR"
        legacy_name = self.output_csv.with_name(self.output_csv.name + ".legacy_TESTMOVE.csv")
        self.output_csv.rename(legacy_name)
        return "READY_NEW"

    # -- WindowAlreadyOk mirror ------------------------------------------
    def _window_already_ok(self, window_start: dt.date, window_end: dt.date) -> bool:
        if not self.output_csv.exists():
            return False
        with self._handles.acquire(self.log_path, "read"):
            log = read_integrity_window_log(self.log_path, self.country, self.currency)
        key = _window_key(window_start, window_end)
        entry = log.get(key)
        if entry is None or entry.status != "OK":
            return False
        rows_in_window = self._rows_in_window(window_start, window_end)  # acquires output_csv itself
        count, digest = window_content_digest(rows_in_window)
        return count == entry.canonical_row_count and digest == entry.content_digest

    def _rows_in_window(self, window_start: dt.date, window_end: dt.date) -> List[Dict[str, str]]:
        if not self.output_csv.exists():
            return []
        with self._handles.acquire(self.output_csv, "read"):
            lines = self.output_csv.read_text(encoding="utf-8").splitlines()
        if len(lines) <= 1:
            return []
        header = lines[0].split(",")
        out = []
        for line in lines[1:]:
            if not line.strip():
                continue
            values = line.split(",")
            row = dict(zip(header, values))
            try:
                d = _parse_event_date(row["event_time"])
            except (KeyError, ValueError):
                continue
            if window_start <= d <= window_end:
                out.append(row)
        return out

    def _append_rows(self, rows: List[Dict[str, str]]) -> None:
        lines = [",".join(row.get(col, "") for col in CSV_COLUMNS) for row in rows]
        with self._handles.acquire(self.output_csv, "write"):
            with self.output_csv.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
                if lines:
                    fh.write("\n")

    def _log_window_status(self, window_start: dt.date, window_end: dt.date, status: str, row_count: int, digest: str) -> bool:
        """FIXED design (seventh review, defect A): a genuine, durable
        append -- Python's "a" mode never truncates and always writes
        at EOF regardless of prior position, matching the outcome of
        the real .mq5 fix (FileOpen(FILE_READ|FILE_WRITE|FILE_BIN),
        never bare FILE_WRITE, plus an explicit FileSeek to FileSize()
        checked before writing -- see LogWindowStatus's own docstring
        and mql5_exporter/FileModeProbe.mq5's empirically-confirmed
        Q2/Q3 findings for why the explicit seek is required in MQL5
        even though Python's "a" mode handles this internally). Returns
        True/False so callers can propagate a persistence failure
        rather than silently treating it as success."""
        win_start_str, win_end_str = _window_key(window_start, window_end)
        line = ",".join([
            win_start_str, win_end_str, self.country, self.currency, INTEGRITY_SCHEMA_VERSION,
            status, str(row_count), "1", "2024.01.01 00:00:00", digest,
        ])
        with self._handles.acquire(self.log_path, "write"):
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True

    def _log_window_status_old_buggy(
        self, window_start: dt.date, window_end: dt.date, status: str, row_count: int, digest: str,
        simulate_interruption_after_truncate: bool = False,
    ) -> bool:
        """The ORIGINAL (pre-seventh-review) LogWindowStatus: reads the
        entire existing log (a read failure is treated identically to
        "file absent" -- the original bug), then opens with a
        TRUNCATING write (Python's "wb" mode truncates unconditionally
        on open, mirroring MQL5's bare FILE_WRITE -- empirically
        confirmed via mql5_exporter/FileModeProbe.mq5's Q3), then
        rewrites old+new content. `simulate_interruption_after_truncate
        =True` mirrors a process death between the truncating open and
        the rewrite -- the file is left EMPTY and every prior
        checkpoint record is destroyed, even though nothing was wrong
        with those earlier checkpoints. Used ONLY to demonstrate the
        confirmed defect -- not the production behavior."""
        existing_content = ""
        if self.log_path.exists():
            try:
                existing_content = self.log_path.read_text(encoding="utf-8")
            except OSError:
                existing_content = ""  # the original bug: a read failure == "absent"

        win_start_str, win_end_str = _window_key(window_start, window_end)
        line = ",".join([
            win_start_str, win_end_str, self.country, self.currency, INTEGRITY_SCHEMA_VERSION,
            status, str(row_count), "1", "2024.01.01 00:00:00", digest,
        ])

        with self.log_path.open("wb"):
            pass  # TRUNCATES immediately on open -- the defect's mechanism
        if simulate_interruption_after_truncate:
            return False  # "process died" here -- file is now empty, old history gone

        with self.log_path.open("a", encoding="utf-8") as fh:
            if existing_content:
                fh.write(existing_content)
            fh.write(line + "\n")
        return True

    # -- OnStart mirror ----------------------------------------------------
    def run(
        self,
        windows: List[Tuple[dt.date, dt.date]],
        fetch_fn,
        force_migration_failure: bool = False,
        force_read_failure: bool = False,
        simulate_write_failure_for: Optional[set] = None,
        simulate_checkpoint_failure_for: Optional[set] = None,
    ) -> dict:
        """`fetch_fn(window_start, window_end) -> List[row_dict]` mirrors
        CalendarValueHistory + the per-row CSV line construction --
        raises to simulate a fetch failure. `simulate_write_failure_for`
        is a set of (window_start, window_end) tuples for which the
        write/flush step itself is treated as failed (nothing appended,
        nothing logged) -- mirrors a disk-full/flush failure.
        `simulate_checkpoint_failure_for` is a set of windows for which
        data is fetched/written/verified successfully but the FINAL
        "OK" checkpoint write itself fails (seventh review, defect A's
        propagation requirement) -- the window must be reported as
        failed, not silently announced durable, even though its CSV
        rows are genuinely present."""
        outcome = self.migrate_legacy_csv_if_needed(force_migration_failure, force_read_failure)
        if outcome == "ERROR":
            return {"aborted": True, "results": []}

        if outcome == "READY_NEW":
            self.output_csv.write_text(CSV_HEADER_LINE + "\n", encoding="utf-8")

        # INTERRUPTED-WRITE TAIL RECOVERY (eighth review, P1): run BEFORE
        # anything else reads or writes either file this run -- mirrors
        # OnStart's upfront RecoverFileTail calls. A no-op for a freshly
        # created file (nothing dangling yet) or one that already ends
        # cleanly; see recover_file_tail/compute_safe_append_boundary
        # above for the full recovery procedure.
        recover_file_tail(self.output_csv, True)
        recover_file_tail(self.log_path, False)

        simulate_checkpoint_failure_for = simulate_checkpoint_failure_for or set()
        results = []
        for window_start, window_end in windows:
            if self._window_already_ok(window_start, window_end):
                results.append({"window": (window_start, window_end), "status": "SKIPPED"})
                continue

            simulate_write_failure_for = simulate_write_failure_for or set()
            if (window_start, window_end) in simulate_write_failure_for:
                results.append({"window": (window_start, window_end), "status": "WRITE_FAILED"})
                continue

            try:
                new_rows = fetch_fn(window_start, window_end)
            except Exception:  # noqa: BLE001
                self._log_window_status(window_start, window_end, "FAILED", 0, "")
                results.append({"window": (window_start, window_end), "status": "FETCH_FAILED"})
                continue

            self._append_rows(new_rows)
            # Rescan the CURRENT full file content for this window (not
            # just the rows just appended) -- overlapping windows may
            # have already written some of this window's rows earlier.
            rows_in_window = self._rows_in_window(window_start, window_end)
            count, digest = window_content_digest(rows_in_window)

            # FETCHED-VS-PERSISTED VERIFICATION (eighth review, P1): the
            # digest above is only ever INTERNALLY self-consistent -- it
            # describes whatever canonical rows currently parse, which
            # is exactly what let the confirmed defect through (an
            # interrupted write's retry merging onto a dangling
            # fragment garbles ONE record into an unparseable line that
            # is silently dropped, while everything else still produces
            # a perfectly self-consistent digest for the REDUCED set).
            # Cross-check every record `fetch_fn` actually returned this
            # call against what the rescan found, by id and content.
            expected_map = _canonical_hash_map(new_rows)
            persisted_map = _canonical_hash_map(rows_in_window)
            missing_or_altered = sorted(
                vid for vid, h in expected_map.items()
                if vid not in persisted_map or persisted_map[vid] != h
            )
            if missing_or_altered:
                results.append({
                    "window": (window_start, window_end), "status": "VERIFICATION_FAILED",
                    "count": count, "digest": digest, "missing_or_altered": missing_or_altered,
                })
                self._log_window_status(window_start, window_end, "FAILED", 0, "")
                continue

            if (window_start, window_end) in simulate_checkpoint_failure_for:
                # Data is genuinely fetched/written/verified (count/digest
                # are real), but the checkpoint evidence fails to persist
                # -- must NOT be announced as durable success.
                results.append({"window": (window_start, window_end), "status": "CHECKPOINT_FAILED", "count": count, "digest": digest})
                continue

            logged = self._log_window_status(window_start, window_end, "OK", count, digest)
            if not logged:
                results.append({"window": (window_start, window_end), "status": "CHECKPOINT_FAILED", "count": count, "digest": digest})
                continue

            results.append({"window": (window_start, window_end), "status": "OK", "count": count, "digest": digest})

        return {"aborted": False, "results": results}

    # -- ORIGINAL (pre-sixth-review) OnStart mirror, for comparison -------
    def run_with_old_buggy_handle_ordering(
        self,
        windows: List[Tuple[dt.date, dt.date]],
        fetch_fn,
    ) -> dict:
        """Reproduces the ORIGINAL OnStart() structure (issue #1, sixth
        review): the writer handle to `output_csv` is opened ONCE,
        BEFORE the per-window loop, and held open (via the SAME
        ExclusiveFileHandleRegistry every other method here uses) for
        the loop's entire duration -- including every
        `_window_already_ok` call, which independently tries to open a
        READ handle on that SAME path for its rescan.

        Raises FileHandleConflictError the first time a window actually
        needs verifying against non-empty content (an empty/nonexistent
        file has nothing to verify, so the conflict cannot manifest
        until there is real data and a real window-log entry to check)
        -- this is the concrete, structural reproduction of the
        confirmed defect, not a hand-wavy description of it.
        """
        outcome = self.migrate_legacy_csv_if_needed()
        if outcome == "ERROR":
            return {"aborted": True, "results": []}
        if outcome == "READY_NEW":
            self.output_csv.write_text(CSV_HEADER_LINE + "\n", encoding="utf-8")

        mode = "write" if outcome == "READY_NEW" else "append"
        with self._handles.acquire(self.output_csv, mode):  # <-- opened BEFORE the loop, held for its entirety
            results = []
            for window_start, window_end in windows:
                # This call's OWN internal handle acquisitions on
                # `self.output_csv` (via _rows_in_window) will conflict
                # with the writer already held above -- raises
                # FileHandleConflictError instead of silently
                # succeeding, exactly reproducing the defect.
                already_ok = self._window_already_ok(window_start, window_end)
                if already_ok:
                    results.append({"window": (window_start, window_end), "status": "SKIPPED"})
                    continue

                new_rows = fetch_fn(window_start, window_end)
                lines = [",".join(row.get(col, "") for col in CSV_COLUMNS) for row in new_rows]
                with self.output_csv.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                    if lines:
                        fh.write("\n")
                results.append({"window": (window_start, window_end), "status": "OK"})

        return {"aborted": False, "results": results}


def simulate_old_buggy_onstart_control_flow(
    output_csv: Path, force_migration_failure: bool
) -> str:
    """A literal, unmodified transliteration of the ORIGINAL (pre-fix)
    MigrateLegacyCsvIfNeeded()/OnStart() control flow -- used ONLY to
    demonstrate the confirmed defect (issue #2) actually reproduces
    under simulation before the fix: `MigrateLegacyCsvIfNeeded` returns
    `false` for BOTH "compatible header, append" and "migration
    failed", and OnStart's guard
    `if(!needFreshFile && !FileIsExist(OutputFile)) return;` does not
    catch the case where an incompatible file still exists because the
    move failed -- execution falls through into APPEND mode on the
    still-incompatible file.

    Returns "ABORTED", "APPENDED_COMPATIBLE", or "APPENDED_INCOMPATIBLE"
    (the last one is the bug).
    """
    if not output_csv.exists():
        need_fresh_file = True
    else:
        existing_header = output_csv.read_text(encoding="utf-8").splitlines()[0] if output_csv.stat().st_size > 0 else ""
        if existing_header == CSV_HEADER_LINE:
            need_fresh_file = False  # "false" -- append, correctly
        elif force_migration_failure:
            need_fresh_file = False  # BUG: FileMove failure ALSO returns "false"
        else:
            legacy_name = output_csv.with_name(output_csv.name + ".legacy_TESTMOVE.csv")
            output_csv.rename(legacy_name)
            need_fresh_file = True

    csv_still_exists = output_csv.exists()
    if (not need_fresh_file) and (not csv_still_exists):
        return "ABORTED"  # MigrateLegacyCsvIfNeeded already printed the error

    if not need_fresh_file:
        # Append mode -- opened regardless of WHY needFreshFile is false.
        header_now = output_csv.read_text(encoding="utf-8").splitlines()[0] if output_csv.stat().st_size > 0 else ""
        if header_now == CSV_HEADER_LINE:
            return "APPENDED_COMPATIBLE"
        return "APPENDED_INCOMPATIBLE"  # the confirmed bug
    return "CREATED_FRESH"


# ---------------------------------------------------------------------------
# Defect B simulation (seventh review): "a failed seek leaves a usable but
# incorrectly positioned writer". The class-based SimulatedMql5Exporter above
# does not model a PERSISTENT write handle shared across windows (each of its
# operations is a self-contained open/close) -- the real .mq5 bug is
# specifically about a HANDLE, reused via the `int &fileHandle` reference
# parameter, surviving a failed reposition across FetchMonthWindow calls. The
# functions below model exactly that persistent-handle control flow using a
# real Python file object with real byte positions, mirroring the .mq5
# structure line-for-line (assign-then-check vs. check-then-invalidate).
# ---------------------------------------------------------------------------

def simulate_seek_bug_old(path: Path, first_write: bytes, second_write: bytes) -> bytes:
    """Literal transliteration of the ORIGINAL buggy control flow, with
    the FileSeek OUTCOME injected directly (a real OS-level seek
    failure is not something this Python harness can organically force
    on a normal file -- only the CONTROL FLOW's response to a `false`
    result is under test here, not what makes a real MQL5 FileSeek
    return `false`):

        int reopened = FileOpen(...);
        bool seekOk = FileSeek(reopened, endPos, SEEK_SET);  // INJECTED: always false here
        fileHandle = reopened;                    // <-- assigned regardless of seekOk
        if(!seekOk) { ...report FAILED...; return 0; }   // caller's fileHandle is left USABLE

    The caller only checks `fileHandle == INVALID_HANDLE` before the NEXT
    window -- since `reopened` was a real, valid handle, the check passes and
    the next window writes through it at whatever position the failed seek
    left it at (position 0, matching the real .mq5 empirical finding in
    FileModeProbe.mq5's Q2/Q5 that a fresh reopen without an explicit seek
    starts at position 0, not EOF) -- silently overwriting existing bytes.

    Returns the FINAL file content after both writes, so a test can assert it
    was corrupted.
    """
    path.write_bytes(first_write)

    # Window 1: reopen; the seek is injected as FAILING, but the handle is
    # kept "valid" regardless (the bug) and left open across windows.
    fh = open(path, "r+b")
    seek_ok = False  # injected failure
    file_handle_valid = True  # THE BUG: assigned before checking seek_ok
    # (real code: `if(!seekOk) { report FAILED; return 0; }` -- note it never
    # closes `reopened` on this path either)

    # Window 2: caller only checks "is the handle valid" -- it is (by the
    # bug's own logic), so it writes through the SAME handle, at whatever
    # position it was left at after open (position 0 -- see docstring).
    if file_handle_valid:
        fh.write(second_write)
    fh.close()

    return path.read_bytes()


def simulate_seek_bug_fixed(path: Path, first_write: bytes, second_write: bytes) -> bytes:
    """The FIXED control flow: a failed seek closes and invalidates the
    handle immediately, so the caller's validity check correctly blocks the
    next window from writing through it at all. The seek outcome is
    injected as failing, for the same reason as `simulate_seek_bug_old`."""
    path.write_bytes(first_write)

    fh = open(path, "r+b")
    seek_ok = False  # injected failure
    if seek_ok:
        pass  # would seek here
    else:
        fh.close()
        fh = None  # invalidated -- mirrors `reopened = INVALID_HANDLE`

    # Window 2: caller checks "is the handle valid" -- it is None/invalid,
    # so the write is correctly skipped, never touching the file.
    if fh is not None:
        fh.write(second_write)
        fh.close()

    return path.read_bytes()


def exporter_windows(
    start_date_inclusive: dt.date, end_date_inclusive: dt.date
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Mirrors the fixed OnStart(): StartDate/EndDate are both INCLUSIVE
    user-facing calendar dates. EndDate is converted, once, to an
    exclusive midnight boundary (the day after) before the monthly
    chunking loop runs -- the loop itself never special-cases whether a
    window happens to land on a natural calendar-month boundary."""
    start = dt.datetime(start_date_inclusive.year, start_date_inclusive.month, start_date_inclusive.day)
    end_exclusive = next_day_start(day_start(
        dt.datetime(end_date_inclusive.year, end_date_inclusive.month, end_date_inclusive.day)
    ))
    windows: List[Tuple[dt.datetime, dt.datetime]] = []
    cursor = month_start(start)
    while cursor < end_exclusive:
        window_end = next_month_start(cursor)
        if window_end > end_exclusive:
            window_end = end_exclusive
        window_start = start if cursor < start else cursor
        windows.append((window_start, window_end))
        cursor = next_month_start(cursor)
    return windows
