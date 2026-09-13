"""MQL5 Economic Calendar ingestion.

WINDOW INTEGRITY CONTRACT (fifth review): a window-log "OK" entry
claiming a window was successfully exported is NOT, by itself, proof
that the CSV still actually contains that data -- the log and the CSV
are two separate files, and a naive "log says OK and at least one row
is present" check (the fourth-review fix) proved only that SOME data
exists, not that the exported window's data is intact. Concretely, this
could not detect: the CSV being truncated back down to just its header,
a partial truncation that still leaves >0 rows, or an in-place edit
that changes a value without changing the row count.

The fix: the exporter records, per window, a CONTENT DIGEST over the
canonical row set it exported -- not just a row count, a file
creation-date, or a whole-file checksum (which would be invalidated by
appending an unrelated later window). Ingestion recomputes the SAME
digest from the CSV's CURRENT contents for that exact window and only
certifies completion when it matches exactly. See `window_content_digest`
below and the mirrored design in mql5_exporter/EconomicCalendarExporter.mq5.

Design decisions (stated explicitly, per the review's requirement):
  - Digests/counts apply to CANONICAL records, not physical exported
    rows: deduplicated by `value_id`, keeping the LAST occurrence in
    file order. An overlapping re-fetch (e.g. Sept 1-10 then Sept 1-11)
    re-writes the same value_ids for the overlapping days -- that is
    NOT "extra"/corrupted data, and a genuine revision (same value_id,
    updated fields, appended later) is correctly reflected by its
    newest occurrence.
  - The digest is a small, from-scratch 64-bit FNV-1a hash over each
    canonical row's RAW STRING field values (as read from the CSV,
    never reformatted/reparsed) -- deliberately NOT a cryptographic
    hash and NOT reliant on any MQL5 crypto library call, so it can be
    implemented identically and independently on both sides without a
    dependency whose exact behavior cannot be verified without a
    compiler. Per-row hashes are XOR-combined (order-independent), so
    the result never depends on file/dict iteration order.
  - Evidence describes the data AT SUCCESSFUL EXPORT TIME. Ingestion
    (this module) only ever COMPARES against that recorded evidence --
    it never writes new "OK" evidence back to the window log, so
    damaged data can never be "blessed" into new evidence during a
    read-only ingestion pass.
  - A malformed, incomplete, wrong-schema-version, or otherwise
    unparseable log row simply does not exist in the returned map (see
    `read_window_log`) -- it can never certify completion.
  - EXPORT_SCHEMA_VERSION bumped 3 -> 4: the window-log row format
    changed (canonical_row_count + content_digest replace the previous
    rows-count + csv_create_date fields), and the previous
    (creation-date-based) generation-binding heuristic is superseded by
    this strictly stronger per-window content check -- an old v3 log is
    simply invisible to this version (a different filename), never
    misread, exactly like the v2 -> v3 bump before it.
  - EXPORT_SCHEMA_VERSION bumped 4 -> 5 (sixth review): CSV/log text is
    now explicitly UTF-8 on both sides. The exporter previously used
    FILE_ANSI (the OS system codepage) for all CSV/log file I/O, and
    its digest hashed `StringGetCharacter()` values -- UTF-16 CODE
    UNITS, not UTF-8 bytes. For pure-ASCII content the two byte/encoding
    schemes happen to coincide, but for any non-ASCII text (e.g. an
    accented character) they produce genuinely different bytes and
    therefore different digests -- an intact, unmodified export could
    fail integrity verification (or fail to even decode as UTF-8)
    purely from this mismatch. See `window_content_digest`'s docstring
    and mql5_exporter/EconomicCalendarExporter.mq5's header comment for
    the full contract. Old v4 evidence is never silently re-certified
    under the new byte-accurate algorithm -- this bump makes ALL prior
    evidence unverified (a new window-log filename), regardless of
    whether a given v4 file happened to be pure ASCII (and thus
    byte-identical either way) -- re-export is required either way.

IMPORTANT LIMITATION: the MQL5 Calendar API (CalendarValueHistory) is
only callable from code running inside the MetaTrader 5 terminal --
there is no HTTP endpoint. `mql5_exporter/EconomicCalendarExporter.mq5`
is the piece that must be run manually inside MetaTrader; it produces a
CSV under MQL5\\Files\\ that the user copies (or symlinks) to
`data/raw/mql5/us_macro_calendar.csv`, plus a sidecar
`us_macro_calendar.csv.windows.v5.csv` durable per-window status log,
schema-versioned so an older-format log is never misread (see the .mq5
file's own comments).

This module does NOT talk to MetaTrader. It:
  1. locates that CSV (and its sidecar window log, if present),
  2. parses it,
  3. determines per-month coverage using the sidecar log when available
     -- matching on EXACT window boundaries + country/currency + schema
     version, not just (year, month), so a partial-month export (e.g.
     Sept 1-10) can never be mistaken for full-month coverage when a
     later export widens the range -- falling back to a row-presence
     heuristic ONLY when no sidecar log exists, and NEVER finalizing
     coverage from that fallback (see `coverage_inferred` below),
  4. records manifest entries per covered month,
  5. reports any requested months that are missing so the user knows
     exactly what to re-export.

IMPORTANT INVARIANT: for the window log to match, the StartDate/EndDate
given to the .mq5 exporter must produce the SAME per-month clipped
window boundaries as the (start_date, end_date) given to
`ingest_mql5_calendar` here -- both sides compute "clip to calendar
month, then clip to the overall requested range" identically, so
running the exporter and calling this with the same overall range
always lines up.

DATE CONTRACT: both StartDate/EndDate (.mq5 input) and start_date/
end_date (Python) are INCLUSIVE user-facing calendar dates -- "give me
data through this day". Internally each side converts this to a
half-open [start midnight, day-after-end midnight) representation
exactly ONCE (the exporter in OnStart(), Python in
`_window_key_for_chunk`), with NO further special-casing for whether a
particular window happens to land on a natural calendar-month boundary.
A prior version of `_window_key_for_chunk` special-cased "is chunk_end
a natural month-end?" to decide whether to add a day -- correct only by
coincidence for full-month windows, and wrong for any partial/clipped
window (a single-day range, or the last chunk of a multi-month range).
See tests/_mql5_exporter_sim.py for the cross-check proving Python's
window-key reconstruction agrees with the exporter's own logic.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..manifest import Manifest, ManifestEntry, checksum_file

logger = logging.getLogger(__name__)

# Must match EXPORT_SCHEMA_VERSION in mql5_exporter/EconomicCalendarExporter.mq5.
# Bumped 3 -> 4: the window log row now carries (canonical_row_count,
# content_digest) evidence instead of (rows, csv_create_date). Bumped
# 4 -> 5: CSV/log text and digest hashing are now explicitly UTF-8 on
# both sides (see the module docstring's "WINDOW INTEGRITY CONTRACT").
MQL5_EXPORT_SCHEMA_VERSION = "5"

CSV_COLUMNS = [
    "value_id", "event_id", "event_name", "country_code", "currency_code", "importance",
    "event_time", "period", "unit", "multiplier", "actual_value", "forecast_value",
    "prev_value", "revised_prev_value", "revision", "source_timezone",
]

_FNV_OFFSET_BASIS_64 = 0xCBF29CE484222325
_FNV_PRIME_64 = 0x100000001B3
_MASK_64 = 0xFFFFFFFFFFFFFFFF


def _fnv1a_64(data: bytes) -> int:
    """See the module docstring's WINDOW INTEGRITY CONTRACT for why this
    is a small from-scratch hash rather than hashlib.sha256: it must be
    independently, identically implementable in MQL5 using only
    64-bit-unsigned multiply/xor (no crypto library call whose exact
    behavior cannot be verified without a compiler)."""
    h = _FNV_OFFSET_BASIS_64
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME_64) & _MASK_64
    return h


def _canonical_row_string(raw_row: Dict[str, str]) -> str:
    """Deterministic serialization of one CSV row's RAW string field
    values (never reformatted/reparsed -- avoids any float
    round-tripping ambiguity) in CSV_COLUMNS order, joined by the ASCII
    unit separator (0x1F)."""
    return "\x1f".join(raw_row.get(col, "") or "" for col in CSV_COLUMNS)


def window_content_digest(raw_rows: List[Dict[str, str]]) -> Tuple[int, str]:
    """(canonical_row_count, digest_hex) for a set of raw CSV rows
    already filtered to one window's date range. CANONICAL records --
    deduplicated by `value_id`, keeping the LAST occurrence in file
    order (see module docstring) -- not physical row count."""
    canonical: Dict[str, Dict[str, str]] = {}
    for row in raw_rows:
        canonical[row["value_id"]] = row  # last occurrence wins
    combined = 0
    for row in canonical.values():
        combined ^= _fnv1a_64(_canonical_row_string(row).encode("utf-8"))
    return len(canonical), f"{combined:016x}"


def _read_raw_csv_rows(csv_path: Path) -> List[Dict[str, str]]:
    """Raw (never type-coerced) CSV rows, keyed by column name -- used
    ONLY for window-integrity digest computation, which must operate on
    the exact on-disk text (see `window_content_digest`). Applies the
    same malformed-row (field-count-mismatch) rejection as
    `parse_mql5_csv`, for the same reason: never let a misaligned row
    silently contribute wrong-named field values to the digest."""
    rows: List[Dict[str, str]] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            if None in r or any(v is None for v in r.values()):
                continue
            rows.append(r)
    return rows


class Mql5Row(NamedTuple):
    value_id: str  # unique per (event, occurrence) -- stable dedup/identity key
    event_id: str
    event_name: str
    country_code: str
    currency_code: str
    importance: str
    event_time_raw: str  # as written by the exporter, e.g. "2020.01.15 13:30:00"
    period_raw: Optional[str]  # "YYYY.MM.DD", the calendar period this value describes (real MqlCalendarValue.period)
    unit: str
    multiplier: str
    actual_value: Optional[float]
    forecast_value: Optional[float]
    prev_value: Optional[float]
    revised_prev_value: Optional[float]
    revision: Optional[int]
    source_timezone: str


def _parse_float(raw: str) -> Optional[float]:
    raw = raw.strip()
    return float(raw) if raw else None


def _parse_int(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw else None


# Legacy CSV (pre-fix exporter) lacked value_id/multiplier/revision/period
# columns. Rejecting it outright would strand anyone who already ran an
# older exporter; instead we parse what's there and fill the rest with
# clearly missing markers so it's obvious the export predates the fix.
_REQUIRED_COLUMNS = {"event_id", "event_name", "country_code", "currency_code", "importance", "event_time"}


def parse_mql5_csv(path: Path) -> List[Mql5Row]:
    rows: List[Mql5Row] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = set(reader.fieldnames or [])
        missing = _REQUIRED_COLUMNS - fieldnames
        if missing:
            raise ValueError(f"MQL5 CSV {path} missing required columns: {sorted(missing)}")
        is_legacy = "value_id" not in fieldnames
        has_period = "period" in fieldnames
        expected_field_count = len(reader.fieldnames or [])

        for r in reader:
            # `csv.DictReader` maps a row's values to header names
            # POSITIONALLY -- a row with more fields than the header
            # stashes the extras under the special `None` key, and one
            # with fewer leaves the missing declared keys set to `None`.
            # Either way, every value from the mismatch point onward is
            # silently misassigned to the WRONG field name rather than
            # raising -- exactly what appending new-schema rows under an
            # old-schema header (the .mq5 exporter bug this guards
            # against) would produce. Reject such rows outright instead
            # of trusting misaligned data.
            if None in r or any(v is None for v in r.values()):
                logger.warning(
                    "[MQL5] %s: skipping malformed row (expected %d fields, got a mismatched "
                    "count) -- the file may mix rows from different export schema versions; "
                    "re-export with a single, current-schema run. Row starts with: %r",
                    path, expected_field_count, next(iter(r.values()), None),
                )
                continue
            rows.append(
                Mql5Row(
                    value_id=r.get("value_id") or f"legacy:{r['event_id']}:{r['event_time']}",
                    event_id=r["event_id"],
                    event_name=r["event_name"],
                    country_code=r["country_code"],
                    currency_code=r["currency_code"],
                    importance=r["importance"],
                    event_time_raw=r["event_time"],
                    period_raw=(r.get("period") or None) if has_period else None,
                    unit=r.get("unit", ""),
                    multiplier=r.get("multiplier", "UNKNOWN") or "UNKNOWN",
                    actual_value=_parse_float(r["actual_value"]),
                    forecast_value=_parse_float(r["forecast_value"]),
                    prev_value=_parse_float(r["prev_value"]),
                    revised_prev_value=_parse_float(r["revised_prev_value"]),
                    revision=_parse_int(r.get("revision", "")),
                    source_timezone=r.get("source_timezone", "UNKNOWN") or "UNKNOWN",
                )
            )
    if is_legacy and rows:
        logger.warning(
            "[MQL5] %s is a legacy export (no value_id/multiplier/revision columns) -- "
            "re-run the current mql5_exporter/EconomicCalendarExporter.mq5 to get durable "
            "per-window status and correct 1,000,000-scale values.",
            path,
        )
    elif not has_period and rows:
        logger.warning(
            "[MQL5] %s predates the `period` column -- reference-period alignment will "
            "fall back to the inferred prior-month heuristic for these rows instead of "
            "MqlCalendarValue.period. Re-run the current exporter to get real period data.",
            path,
        )
    return rows


def _row_date(row: Mql5Row) -> dt.date:
    # exporter writes "YYYY.MM.DD HH:MM:SS" via MQL5 TimeToString
    return dt.datetime.strptime(row.event_time_raw, "%Y.%m.%d %H:%M:%S").date()


def _row_date_from_raw(raw_row: Dict[str, str]) -> Optional[dt.date]:
    """Like `_row_date`, but for a raw (never type-coerced) CSV row dict
    -- used by the window-integrity digest recomputation. Returns None
    (never raises) for an unparseable event_time so an unparseable row
    simply doesn't match any window, rather than crashing ingestion."""
    try:
        return dt.datetime.strptime(raw_row.get("event_time", ""), "%Y.%m.%d %H:%M:%S").date()
    except ValueError:
        return None


def _window_log_path(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.name + f".windows.v{MQL5_EXPORT_SCHEMA_VERSION}.csv")


class WindowLogEntry(NamedTuple):
    status: str  # "OK" | "FAILED"
    canonical_row_count: int
    content_digest: str


def read_window_log(csv_path: Path, country: str, currency: str) -> Dict[Tuple[str, str], WindowLogEntry]:
    """Read the exporter's durable per-window status log, if present.

    Returns {(window_start_raw, window_end_raw): WindowLogEntry}, for
    entries matching `country`/`currency`/the current schema version
    AND with a fully well-formed row (exact field count, a parseable
    integer row count, a non-empty digest) -- an entry for a different
    country/currency, a wrong/incompatible schema version, or a
    malformed/incomplete/truncated row is simply not present in the
    returned map (never partially trusted -- see the module docstring's
    "WINDOW INTEGRITY CONTRACT"). A window can appear multiple times
    (retries) -- the LAST matching well-formed row wins, mirroring how
    the .mq5 exporter itself decides whether to skip a rerun.
    """
    log_path = _window_log_path(csv_path)
    if not log_path.exists():
        return {}
    entries: Dict[Tuple[str, str], WindowLogEntry] = {}
    with open(log_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) != 10:
                continue  # malformed/incomplete/old-schema row -- never certifies completion
            (
                win_start, win_end, log_country, log_currency, schema_version,
                status, row_count_raw, _attempts, _logged_at, digest,
            ) = row
            if log_country != country or log_currency != currency:
                continue
            if schema_version != MQL5_EXPORT_SCHEMA_VERSION:
                continue
            try:
                row_count = int(row_count_raw)
            except ValueError:
                continue
            normalized_status = status.strip().upper()
            # A FAILED attempt legitimately has no digest (the fetch
            # never produced data to hash) -- only an "OK" row's digest
            # is ever used as certifying evidence, so only IT must be
            # non-empty to be considered well-formed.
            if normalized_status == "OK" and not digest:
                continue
            entries[(win_start, win_end)] = WindowLogEntry(normalized_status, row_count, digest)
    return entries


def _window_key_for_chunk(chunk_start: dt.date, chunk_end: dt.date) -> Tuple[str, str]:
    """Reproduce the exact strings the .mq5 exporter writes for a window's
    start/end via TimeToString(t, TIME_DATE | TIME_SECONDS), so Python-side
    chunk boundaries can be matched against the log's raw text keys.

    ONE contract, no month-end special-casing: `chunk_end` (DateChunk.end,
    always inclusive) is converted to an exclusive midnight boundary by
    adding exactly one day -- uniformly, whether or not this chunk
    happens to land on a natural calendar-month end. This matches the
    exporter's own (fixed) OnStart(): EndDate is an INCLUSIVE user-facing
    calendar date, converted ONCE to an exclusive midnight boundary
    before its monthly chunking loop runs, so every window -- full month
    or clipped/partial -- is exclusive-end the same way. See
    tests/_mql5_exporter_sim.py for the behavioral cross-check (no
    MetaTrader compiler/runtime is available to verify this by actually
    compiling the .mq5 file).
    """
    start_str = f"{chunk_start:%Y.%m.%d} 00:00:00"
    end_boundary = chunk_end + dt.timedelta(days=1)
    end_str = f"{end_boundary:%Y.%m.%d} 00:00:00"
    return start_str, end_str


class Mql5CoverageReport(NamedTuple):
    covered_months: List[str]
    missing_months: List[str]
    total_rows: int
    csv_path: Path
    coverage_inferred: bool  # True if no window log was found (row-presence fallback)


def ingest_mql5_calendar(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
) -> Mql5CoverageReport:
    """Ingest whatever the MQL5 exporter has already produced and report
    coverage against [start_date, end_date]. Does not fetch anything itself.
    """
    provider_cfg = config.provider("mql5")
    csv_path = config.resolve_path(provider_cfg["input_csv"])

    requested_chunks = list(iter_date_chunks(start_date, end_date, "month"))
    manifest_key = f"{config.macro_country}:{config.macro_currency}"

    if not csv_path.exists():
        logger.error(
            "[MQL5] input CSV not found at %s -- run mql5_exporter/"
            "EconomicCalendarExporter.mq5 inside MetaTrader first",
            csv_path,
        )
        for chunk in requested_chunks:
            manifest.record(
                ManifestEntry(
                    provider="mql5",
                    key=manifest_key,
                    start=chunk.start.isoformat(),
                    end=chunk.end.isoformat(),
                    status="failed",
                    error="MQL5 exporter CSV not found; run .mq5 script in MetaTrader",
                )
            )
        return Mql5CoverageReport([], [c.label for c in requested_chunks], 0, csv_path, coverage_inferred=False)

    rows = parse_mql5_csv(csv_path)
    file_checksum = checksum_file(csv_path)
    window_log = read_window_log(csv_path, config.macro_country, config.macro_currency)
    coverage_inferred = not window_log
    # Raw (never type-coerced) rows, read once, used only to recompute
    # each requested window's content digest against the log's recorded
    # evidence -- see the module docstring's "WINDOW INTEGRITY CONTRACT".
    raw_rows = _read_raw_csv_rows(csv_path) if not coverage_inferred else []
    if coverage_inferred:
        logger.warning(
            "[MQL5] no usable window log at %s (for country=%s currency=%s, schema v%s) -- "
            "falling back to a row-presence heuristic, which is recorded as PROVISIONAL, "
            "never finalized/complete, since row presence alone does not prove a window was "
            "fully/successfully exported. Re-run the current exporter to get a durable log.",
            _window_log_path(csv_path), config.macro_country, config.macro_currency, MQL5_EXPORT_SCHEMA_VERSION,
        )

    rows_by_month: Dict[str, int] = {}
    for row in rows:
        try:
            d = _row_date(row)
        except ValueError:
            logger.warning("[MQL5] unparseable event_time %r, skipping", row.event_time_raw)
            continue
        month_key = f"{d:%Y-%m}"
        rows_by_month[month_key] = rows_by_month.get(month_key, 0) + 1

    covered_months = []
    missing_months = []
    for chunk in requested_chunks:
        count = rows_by_month.get(chunk.label, 0)

        if coverage_inferred:
            # Row presence is SUPPORTING evidence at best, never proof --
            # record as "provisional" (never counted complete, always
            # re-checked) rather than finalizing coverage from it.
            if count > 0:
                covered_months.append(chunk.label)
                manifest.record(
                    ManifestEntry(
                        provider="mql5", key=manifest_key,
                        start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                        status="provisional", rows=count, checksum=file_checksum, path=str(csv_path),
                        request_meta={"coverage_inferred": True},
                    )
                )
                logger.info("[MQL5] %s: %d rows present (PROVISIONAL -- no window log to verify)", chunk.label, count)
            else:
                missing_months.append(chunk.label)
                manifest.record(
                    ManifestEntry(
                        provider="mql5", key=manifest_key,
                        start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                        status="failed", error="no rows present for this month",
                        request_meta={"coverage_inferred": True},
                    )
                )
            continue

        window_key = _window_key_for_chunk(chunk.start, chunk.end)
        log_entry = window_log.get(window_key)

        # A window-log "OK" entry is NECESSARY but not SUFFICIENT: it
        # must be corroborated by the CSV's CURRENT content actually
        # matching the digest recorded for it. The log and the CSV are
        # two separate files -- if the CSV was since deleted, truncated
        # (in full or in part), or edited in place (even without
        # changing its row count), a stale "OK" entry would otherwise be
        # trusted even though the data it claims is gone or altered.
        # Recomputed here ONLY to compare -- never written back as new
        # evidence, so damaged data can never be "blessed" by this
        # read-only ingestion pass.
        window_rows = []
        for r in raw_rows:
            d = _row_date_from_raw(r)
            if d is not None and chunk.start <= d <= chunk.end:
                window_rows.append(r)
        current_count, current_digest = window_content_digest(window_rows)

        if log_entry is None:
            missing_months.append(chunk.label)
            manifest.record(
                ManifestEntry(
                    provider="mql5", key=manifest_key,
                    start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                    status="failed",
                    error="window log has no matching well-formed OK entry for this exact "
                          "window/country/currency/schema",
                    request_meta={"coverage_inferred": False},
                )
            )
        elif log_entry.status != "OK":
            missing_months.append(chunk.label)
            manifest.record(
                ManifestEntry(
                    provider="mql5", key=manifest_key,
                    start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                    status="failed",
                    error=f"window log's latest recorded status for this window is {log_entry.status!r}, not OK",
                    request_meta={"coverage_inferred": False},
                )
            )
        elif current_count == log_entry.canonical_row_count and current_digest == log_entry.content_digest:
            covered_months.append(chunk.label)
            manifest.record(
                ManifestEntry(
                    provider="mql5", key=manifest_key,
                    start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                    status="complete", rows=count, checksum=file_checksum, path=str(csv_path),
                    request_meta={"coverage_inferred": False},
                )
            )
            logger.info(
                "[MQL5] %s: %d canonical rows, content digest verified against window log",
                chunk.label, current_count,
            )
        else:
            # Stale/damaged evidence: the log claims success, but the
            # CSV's current content for this exact window no longer
            # matches what was recorded at successful export time.
            missing_months.append(chunk.label)
            if current_count == 0:
                reason = "the CSV currently has zero rows for this window (deleted/truncated)"
            elif current_count != log_entry.canonical_row_count:
                reason = (
                    f"the CSV currently has {current_count} canonical row(s) for this window, "
                    f"expected {log_entry.canonical_row_count} (partial truncation or data loss)"
                )
            else:
                reason = (
                    "the CSV has the expected row count for this window but its content digest "
                    "does not match -- a value was modified in place without changing the row count"
                )
            manifest.record(
                ManifestEntry(
                    provider="mql5", key=manifest_key,
                    start=chunk.start.isoformat(), end=chunk.end.isoformat(),
                    status="failed",
                    error=(
                        f"window log claims this window is OK but its content integrity check "
                        f"failed: {reason}; re-run the exporter to repair it"
                    ),
                    request_meta={"coverage_inferred": False},
                )
            )
            logger.warning(
                "[MQL5] %s: window log says OK but content integrity check failed (%s) -- "
                "treating as a genuine failure, not trusting the stale/damaged evidence",
                chunk.label, reason,
            )

    if missing_months:
        logger.warning(
            "[MQL5] %d month(s) missing from %s: %s",
            len(missing_months),
            csv_path,
            ", ".join(missing_months),
        )

    return Mql5CoverageReport(covered_months, missing_months, len(rows), csv_path, coverage_inferred)
