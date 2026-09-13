#!/usr/bin/env python3
"""Import manually-downloaded Forex Factory calendar HTML into the pipeline.

    python scripts/import_forex_factory.py data/raw/forex_factory_downloads
    python scripts/import_forex_factory.py data/raw/forex_factory_downloads --start 2016-01-01 --end 2026-09-13

WHY THIS EXISTS: forexfactory.com serves an active Cloudflare managed
challenge (`cf-mitigated: challenge`, a "Just a moment..." JS-verification
page) against automated requests to /calendar -- confirmed by reproducing
the exact 403 this pipeline's own downloader (fetch/forex_factory.py)
gets, with the same request, and inspecting the response. This project
does NOT attempt to defeat that challenge -- no headless-browser
automation, no CAPTCHA handling, no proxy logic, no TLS-fingerprint
spoofing. The historical acquisition mechanism is, deliberately and
permanently:

    manual browser download -> saved HTML -> this importer -> canonical
    Forex Factory macro events

`fetch_historical_data.py`/`update_data.py` no longer attempt a live
Forex Factory request for historical data at all (see
`run_forex_factory` in fetch_historical_data.py) -- they only verify and
report what THIS script has already imported. Live/current-week updates
are a separate, later problem (Forex Factory's own public weekly JSON
export), out of scope here.

STRUCTURED DATA IS PRIMARY: every real Forex Factory calendar page also
embeds its own event data as a JS object (`window.calendarComponentStates
[N] = { days: [...] }`) -- richer than the rendered HTML table (Forex
Factory's own event/template ids, a precise per-event Unix timestamp,
the revised-previous value, the page's own declared display timezone)
and immune to table-markup changes. See normalize/forex_factory.py's
`parse_forex_factory_structured`/`parse_forex_factory_day_dates`/
`parse_forex_factory_timezone_name`. The pre-existing HTML-table parser
(`parse_forex_factory_html`) is NOT removed and is still used as a
fallback for a page that lacks structured data -- see `_table_fallback`
for exactly how, and its honesty limits (it needs a year to disambiguate
a bare "Jan 20" table cell; this script uses the file's containing
directory name as a best-effort HINT for that disambiguation ONLY, never
as a claim about what the file covers -- the actual recorded coverage
always still comes from whatever dates get parsed, exactly as for the
structured-data path).

COVERAGE FROM CONTENT, NEVER FROM METADATA: filenames, directory names,
`og:url`-style meta tags, and the page's own `internal_referrer_uri`
(which literally contains the originally-REQUESTED range, e.g.
"/calendar?range=jan1.2016-mar2.2016") are ALL untrustworthy signals for
what a page actually covers. Confirmed directly: a real captured file
named for "Q1" (implying Jan-Mar) actually stopped rendering on March
2nd. This script computes coverage exclusively from the page's own
rendered CALENDAR DAYS (parse_forex_factory_day_dates -- including
zero-event days like weekends, not just days with a tracked release,
so a legitimately quiet stretch is never mistaken for missing data).

A month is recorded as "complete" using the FULL calendar month's
[1st, last day] boundary ONLY when the page genuinely rendered every
single day of it. A month the page only partially covers is instead
recorded using its own precisely-covered sub-range -- true and useful,
never a false completion claim. A month whose rendered days have an
INTERNAL gap (not just an incomplete tail) is refused outright for that
month rather than guessed at.

DEDUPLICATION: Forex Factory's own per-release `id` (when the structured
-data path supplied one) is the canonical event_id's basis -- stable
across overlapping/duplicate imports of the same underlying release, so
re-importing the same file, or importing two files whose date ranges
overlap, converges to one canonical row per real event via the existing
event_id-keyed merge (normalize/io.merge_write_events), never producing
duplicate releases. This script ALSO tracks a checksum-keyed import log
(data/manifests/forex_factory_import_log.json) so an unmodified file is
skipped on a re-run (reported separately from newly-imported files)
unless --force is passed.

A file whose content looks like a bot-block/challenge page itself (the
same cheap sanity check the live downloader used to run) is refused
outright -- accidentally importing a saved Cloudflare challenge page
must never become "verified coverage" for any month.

EXIT CODE: 0 only if every discovered file was either imported or
already-unchanged (never refused), AND zero coverage gaps remain within
the requested [--start, --end] (default: the full imported range).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.config import load_config
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest, ManifestEntry, checksum_bytes
from src.data.fetch.forex_factory import (
    MANIFEST_KEY,
    _attempt_path,
    _cheap_sanity_check,
    _month_dir,
    _write_pointer,
)
from src.data.normalize.forex_factory import (
    ForexFactoryRawRow,
    normalize_forex_factory_rows,
    parse_forex_factory_day_dates,
    parse_forex_factory_html,
    parse_forex_factory_structured,
    parse_forex_factory_timezone_name,
)
from src.data.normalize.io import merge_write_events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("import_forex_factory")

SUPPORTED_SUFFIXES = {".html", ".htm"}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, help="directory to recursively scan for saved .html/.htm files")
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD -- only record/normalize coverage on or after this date")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD -- only record/normalize coverage on or before this date")
    parser.add_argument("--force", action="store_true", help="re-process a file even if its checksum was already imported")
    parser.add_argument(
        "--retrieved-at", type=str, default=None,
        help="ISO-8601 UTC timestamp override for when saved pages were acquired (default: each file's own mtime)",
    )
    return parser.parse_args(argv)


def discover_files(input_dir: Path) -> List[Path]:
    """Recursively finds every supported HTML file under `input_dir`,
    regardless of how it's organized into subdirectories -- directory
    names (e.g. a per-year layout) are never required and never trusted
    for coverage, only used (optionally, best-effort) as a disambiguation
    hint for the DOM-table fallback -- see _directory_year_hint."""
    return sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)


def _directory_year_hint(path: Path) -> Optional[int]:
    for part in path.parts:
        if len(part) == 4 and part.isdigit() and 2000 <= int(part) <= 2100:
            return int(part)
    return None


# ---------------------------------------------------------------------------
# Import log: per-file provenance + checksum-based skip-unchanged. Keyed by
# checksum (not path) so a renamed/moved/re-saved-identical file is still
# recognized as already imported.
# ---------------------------------------------------------------------------

def _import_log_path(config) -> Path:
    return config.manifest_path.parent / "forex_factory_import_log.json"


def _load_import_log(config) -> Dict[str, dict]:
    path = _import_log_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("files", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_import_log(config, files: Dict[str, dict]) -> None:
    path = _import_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"files": files}, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Coverage: bucket by (year, month), detect full-vs-partial, require
# contiguity within a month.
# ---------------------------------------------------------------------------

def _touched_months(rows: List[ForexFactoryRawRow]) -> Dict[Tuple[int, int], List[ForexFactoryRawRow]]:
    buckets: Dict[Tuple[int, int], List[ForexFactoryRawRow]] = defaultdict(list)
    for row in rows:
        buckets[(row.date.year, row.date.month)].append(row)
    return buckets


def _coverage_ranges_per_month(day_dates: List[dt.date]) -> Dict[Tuple[int, int], Tuple[dt.date, dt.date, bool]]:
    """For each (year, month) touched by `day_dates` (the page's TRUE
    rendered-day universe, NOT just event dates), returns
    (covered_start, covered_end, is_full_month). A partial month is
    recorded using its own precisely-covered sub-range rather than ever
    overclaiming the full month -- see this module's docstring."""
    by_month: Dict[Tuple[int, int], List[dt.date]] = defaultdict(list)
    for d in day_dates:
        by_month[(d.year, d.month)].append(d)

    ranges: Dict[Tuple[int, int], Tuple[dt.date, dt.date, bool]] = {}
    for (year, month), days in by_month.items():
        days = sorted(set(days))
        covered_start, covered_end = days[0], days[-1]
        last_day_of_month = monthrange(year, month)[1]
        expected_full_month = [dt.date(year, month, d) for d in range(1, last_day_of_month + 1)]
        is_full_month = days == expected_full_month
        ranges[(year, month)] = (covered_start, covered_end, is_full_month)
    return ranges


def _is_contiguous(day_dates: List[dt.date]) -> bool:
    days = sorted(set(day_dates))
    return all((b - a).days == 1 for a, b in zip(days, days[1:]))


def _retrieved_at_for(path: Path, override: Optional[str]) -> dt.datetime:
    if override:
        return dt.datetime.fromisoformat(override)
    mtime = path.stat().st_mtime
    return dt.datetime.fromtimestamp(mtime, tz=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Per-file stats: event/USD/mapped/unmapped/forecast/actual/previous/
# revision counts -- reported per file and rolled up for the final summary.
# ---------------------------------------------------------------------------

def _count_stats(rows: List[ForexFactoryRawRow], event_mapping, macro_currency: str) -> Tuple[dict, List[str]]:
    usd_rows = [r for r in rows if r.currency.upper() == macro_currency.upper()]
    mapped = 0
    unmapped_names: List[str] = []
    forecast = actual = previous = revision = 0
    for r in usd_rows:
        if event_mapping.resolve_forex_factory(r.event_name) is not None:
            mapped += 1
        else:
            unmapped_names.append(r.event_name)
        if r.forecast_raw.strip():
            forecast += 1
        if r.actual_raw.strip():
            actual += 1
        if r.previous_raw.strip():
            previous += 1
        if r.revision_raw.strip():
            revision += 1
    stats = {
        "event_count": len(rows),
        "usd_event_count": len(usd_rows),
        "mapped_usd_event_count": mapped,
        "unmapped_usd_event_count": len(usd_rows) - mapped,
        "forecast_count": forecast,
        "actual_count": actual,
        "previous_count": previous,
        "revision_count": revision,
    }
    return stats, unmapped_names


def _table_fallback(html: str, path: Path) -> Tuple[List[ForexFactoryRawRow], List[dt.date]]:
    """Best-effort HTML-table parse for a page with no structured data.
    Uses the file's containing directory name as a YEAR HINT ONLY (never
    trusted as a coverage claim -- the caller still computes actual
    coverage from whatever dates this returns) to disambiguate the
    table's bare "Jan 20"-style date cells, which have no year of their
    own. A month value of 6 is used as the disambiguation anchor
    (parse_forex_factory_html._resolve_day_year only shifts the
    attributed year for Dec/Jan boundary rows relative to the
    REQUESTED month; anchoring at mid-year is correct for any file that
    doesn't itself straddle a Dec->Jan boundary, and wrong only in that
    specific, rare case -- logged clearly either way). Returns
    ([], []) if no year hint is available at all; guessing a year
    would be worse than refusing.
    """
    year_hint = _directory_year_hint(path)
    if year_hint is None:
        logger.warning(
            "[Import] %s: no structured data AND no 4-digit year directory to disambiguate the "
            "HTML-table fallback -- cannot safely parse this file's bare 'Mon D' dates.", path,
        )
        return [], []
    logger.warning(
        "[Import] %s: no structured data found -- falling back to the HTML-table parser using "
        "directory year hint %d (a PARSING aid only, not a coverage claim: actual coverage below "
        "is still computed from whichever dates this parse actually returns).", path, year_hint,
    )
    rows = parse_forex_factory_html(html, year_hint, 6)
    day_dates = sorted({r.date for r in rows})
    return rows, day_dates


@dataclass
class ImportFileResult:
    path: Path
    status: str  # "imported" | "skipped_unchanged" | "refused"
    reason: str = ""
    parsing_mode: str = ""
    checksum: str = ""
    coverage_start: Optional[dt.date] = None
    coverage_end: Optional[dt.date] = None
    stats: dict = field(default_factory=dict)
    unmapped_names: List[str] = field(default_factory=list)
    months_recorded: List[Tuple[int, int]] = field(default_factory=list)


def import_file(
    config, manifest: Manifest, event_mapping, import_log: Dict[str, dict], path: Path, today: dt.date,
    range_start: Optional[dt.date] = None, range_end: Optional[dt.date] = None,
    retrieved_at_override: Optional[str] = None, force: bool = False,
) -> ImportFileResult:
    if not path.exists():
        return ImportFileResult(path, "refused", reason="file does not exist")

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return ImportFileResult(path, "refused", reason=f"could not read file: {exc}")

    checksum = checksum_bytes(raw_bytes)
    if not force and checksum in import_log:
        return ImportFileResult(
            path, "skipped_unchanged", checksum=checksum,
            reason=f"already imported (checksum matches {import_log[checksum].get('path')!r})",
        )

    try:
        html = raw_bytes.decode("utf-8", errors="replace")

        sanity_error = _cheap_sanity_check(html)
        if sanity_error:
            return ImportFileResult(path, "refused", checksum=checksum, reason=f"{sanity_error} (bot-block/challenge page)")

        timezone_name = parse_forex_factory_timezone_name(html) or config.provider("forex_factory").get("display_timezone", "America/New_York")

        day_dates = parse_forex_factory_day_dates(html, display_timezone=timezone_name)
        parsing_mode = "EMBEDDED_CALENDAR_STATE"
        if day_dates:
            rows = parse_forex_factory_structured(html)
        else:
            rows, day_dates = _table_fallback(html, path)
            parsing_mode = "DOM_TABLE"
            if not day_dates:
                return ImportFileResult(path, "refused", checksum=checksum, reason="no structured data and no usable table fallback")

        if range_start is not None:
            day_dates = [d for d in day_dates if d >= range_start]
            rows = [r for r in rows if r.date >= range_start]
        if range_end is not None:
            day_dates = [d for d in day_dates if d <= range_end]
            rows = [r for r in rows if r.date <= range_end]
        if not day_dates:
            return ImportFileResult(path, "refused", checksum=checksum, parsing_mode=parsing_mode, reason="no coverage within the requested --start/--end range")

        event_buckets = _touched_months(rows)
        coverage = _coverage_ranges_per_month(day_dates)

        retrieved_at = _retrieved_at_for(path, retrieved_at_override)
        raw_dir = config.provider_raw_dir("forex_factory")
        ff_provider_cfg = config.provider("forex_factory")
        months_recorded: List[Tuple[int, int]] = []

        for (year, month), (covered_start, covered_end, is_full_month) in sorted(coverage.items()):
            month_days = sorted(d for d in day_dates if (d.year, d.month) == (year, month))
            if not _is_contiguous(month_days):
                logger.warning(
                    "[Import] %s: %04d-%02d has non-contiguous coverage (%d distinct day(s) spanning "
                    "%s..%s) -- refusing to record ANY coverage for this month to avoid overclaiming a "
                    "gap in the middle.", path.name, year, month, len(month_days), covered_start, covered_end,
                )
                continue

            _month_dir(raw_dir, year, month).mkdir(parents=True, exist_ok=True)
            attempt_path = _attempt_path(raw_dir, year, month, retrieved_at)
            attempt_path.write_text(html, encoding="utf-8")
            month_checksum = checksum_bytes(html.encode("utf-8"))

            if is_full_month:
                start = dt.date(year, month, 1).isoformat()
                end = dt.date(year, month, monthrange(year, month)[1]).isoformat()
            else:
                start = covered_start.isoformat()
                end = covered_end.isoformat()
            status = "provisional" if (covered_start <= today <= covered_end) else "complete"

            month_rows = event_buckets.get((year, month), [])
            manifest.record(
                ManifestEntry(
                    provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status=status,
                    rows=len(month_rows), checksum=month_checksum, path=str(attempt_path),
                    request_meta={
                        "acquisition": "manual_browser_download", "import_source": str(path),
                        "parsing_mode": parsing_mode, "timezone_name": timezone_name,
                    },
                )
            )
            _write_pointer(raw_dir, year, month, attempt_path, retrieved_at, month_checksum, len(month_rows))
            months_recorded.append((year, month))

            events = normalize_forex_factory_rows(
                month_rows, event_mapping, retrieved_at,
                currency_filter=config.macro_currency, source_url=str(attempt_path),
                display_timezone=timezone_name,
                display_timezone_verified=bool(ff_provider_cfg.get("display_timezone_verified", False)),
                raw_artifact_checksum=month_checksum,
            )
            merge_write_events(events, config.interim_root / "macro" / "forex_factory_events.parquet")

            logger.info(
                "[Import] %04d-%02d: %s [%s..%s]%s (%d raw row(s), %d normalized event(s)) <- %s",
                year, month, status, start, end, "" if is_full_month else " PARTIAL",
                len(month_rows), len(events), path.name,
            )

        if not months_recorded:
            return ImportFileResult(path, "refused", checksum=checksum, parsing_mode=parsing_mode, reason="no month had contiguous coverage")

        stats, unmapped_names = _count_stats(rows, event_mapping, config.macro_currency)
        return ImportFileResult(
            path, "imported", checksum=checksum, parsing_mode=parsing_mode,
            coverage_start=min(day_dates), coverage_end=max(day_dates),
            stats=stats, unmapped_names=unmapped_names, months_recorded=months_recorded,
        )
    except Exception as exc:  # noqa: BLE001 -- one corrupted/unexpected file must never abort the whole batch
        logger.exception("[Import] %s: unexpected error while importing", path)
        return ImportFileResult(path, "refused", checksum=checksum, reason=f"unexpected error: {exc}")


def _print_gap_report(manifest: Manifest, results: List[ImportFileResult], report_start: Optional[dt.date], report_end: Optional[dt.date]) -> bool:
    imported = [r for r in results if r.status == "imported"]
    skipped = [r for r in results if r.status == "skipped_unchanged"]
    refused = [r for r in results if r.status == "refused"]

    all_starts = [r.coverage_start for r in imported if r.coverage_start] + [
        dt.date.fromisoformat(e.start) for e in manifest.entries_for("forex_factory", MANIFEST_KEY)
    ]
    all_ends = [r.coverage_end for r in imported if r.coverage_end] + [
        dt.date.fromisoformat(e.end) for e in manifest.entries_for("forex_factory", MANIFEST_KEY)
    ]
    overall_start = report_start or (min(all_starts) if all_starts else None)
    overall_end = report_end or (max(all_ends) if all_ends else None)

    total_events = sum(r.stats.get("event_count", 0) for r in imported)
    total_usd = sum(r.stats.get("usd_event_count", 0) for r in imported)
    total_mapped = sum(r.stats.get("mapped_usd_event_count", 0) for r in imported)
    total_forecast = sum(r.stats.get("forecast_count", 0) for r in imported)
    total_actual = sum(r.stats.get("actual_count", 0) for r in imported)
    total_revision = sum(r.stats.get("revision_count", 0) for r in imported)

    unmapped_all = sorted({name for r in imported for name in r.unmapped_names})

    print("\n" + "=" * 60)
    print("FOREX FACTORY HISTORICAL IMPORT")
    print("=" * 60)
    print(f"Files scanned: {len(results)}")
    print(f"Files imported: {len(imported)}")
    print(f"Files skipped unchanged: {len(skipped)}")
    print(f"Files refused: {len(refused)}")
    for r in refused:
        print(f"  REFUSED: {r.path} -- {r.reason}")
    print()

    gaps: List[Tuple[dt.date, dt.date]] = []
    if overall_start and overall_end:
        print(f"Actual coverage: {overall_start} -> {overall_end}")
        gaps = manifest.coverage_gaps("forex_factory", MANIFEST_KEY, overall_start, overall_end)
    else:
        print("Actual coverage: NONE -- nothing imported and nothing previously recorded")

    print()
    print(f"Events parsed: {total_events}")
    print(f"USD events: {total_usd}")
    print(f"Mapped macro events: {total_mapped}")
    print(f"Forecast observations: {total_forecast}")
    print(f"Actual observations: {total_actual}")
    print(f"Revision observations: {total_revision}")
    if unmapped_all:
        print(f"Unmapped USD event names ({len(unmapped_all)}): see forex_factory_unmapped_events.txt")

    print()
    if gaps:
        print("Coverage gaps:")
        for g_start, g_end in gaps:
            print(f"  {g_start} -> {g_end}")
    else:
        print("Coverage gaps: NONE")
    print("=" * 60)

    return len(refused) == 0 and len(gaps) == 0


def _write_unmapped_report(config, results: List[ImportFileResult]) -> None:
    unmapped_all = sorted({name for r in results for name in r.unmapped_names})
    if not unmapped_all:
        return
    report_path = config.manifest_path.parent / "forex_factory_unmapped_events.txt"
    report_path.write_text(
        "# Forex Factory USD event names with no config/event_mapping.yaml entry.\n"
        "# Add a `forex_factory:` entry under the matching event_family to map these.\n\n"
        + "\n".join(unmapped_all) + "\n",
        encoding="utf-8",
    )
    logger.info("[Import] wrote %d unmapped USD event name(s) to %s", len(unmapped_all), report_path)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)
    import_log = _load_import_log(config)
    today = dt.date.today()

    range_start = dt.date.fromisoformat(args.start) if args.start else None
    range_end = dt.date.fromisoformat(args.end) if args.end else None

    if not args.input_dir.exists():
        logger.error("[Import] %s does not exist", args.input_dir)
        return 1

    paths = discover_files(args.input_dir)
    if not paths:
        logger.error("[Import] no .html/.htm files found under %s", args.input_dir)
        return 1

    results: List[ImportFileResult] = []
    for path in paths:
        result = import_file(
            config, manifest, event_mapping, import_log, path, today,
            range_start=range_start, range_end=range_end,
            retrieved_at_override=args.retrieved_at, force=args.force,
        )
        results.append(result)
        if result.status == "imported" and result.checksum:
            import_log[result.checksum] = {
                "path": str(path),
                "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "parsing_mode": result.parsing_mode,
                "coverage_start": result.coverage_start.isoformat() if result.coverage_start else None,
                "coverage_end": result.coverage_end.isoformat() if result.coverage_end else None,
                "months_recorded": [f"{y:04d}-{m:02d}" for y, m in result.months_recorded],
                **result.stats,
            }
        elif result.status == "refused":
            logger.error("[Import] %s REFUSED: %s", path, result.reason)

    _save_import_log(config, import_log)
    _write_unmapped_report(config, results)

    ok = _print_gap_report(manifest, results, range_start, range_end)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
