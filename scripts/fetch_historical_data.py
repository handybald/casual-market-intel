#!/usr/bin/env python3
"""One-command historical data bootstrap.

    python scripts/fetch_historical_data.py --start 2016-01-01 --end 2026-09-10

The user specifies ONE date range. Each provider internally determines
its own safe chunk size (monthly requests -> yearly parquet for
Massive, monthly manifest checkpoints for MQL5, a full-range refetch for
FRED), paginates, retries, rate-limits, resumes from
data/manifests/fetch_manifest.json, and deduplicates. Nothing here loops
over months and re-invokes anything manually.

MQL5 and Forex Factory are the two sources this script cannot fetch
itself. MQL5's Calendar API only runs inside MetaTrader -- this script
detects and ingests whatever mql5_exporter/EconomicCalendarExporter.mq5
has already produced. Forex Factory serves an active Cloudflare managed
challenge against automated requests (confirmed via a real 403 with
`cf-mitigated: challenge`) -- historical coverage instead comes from
`scripts/import_forex_factory.py` importing manually-saved browser
pages; see `run_forex_factory` below. Both sources report exactly what
is missing rather than pretending to have fetched it.

EXIT CODE: 0 only if every REQUESTED source completed with no failures
recorded during THIS run (a source with missing credentials, an
unimplemented source, or any failed chunk makes the run non-zero).
Pre-existing failures from earlier runs that this invocation didn't even
attempt to touch are reported separately and do not, by themselves, fail
the run -- see the summary output.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.config import load_config, load_dotenv_if_present
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest, utcnow_iso
from src.data.fetch.mql5 import ingest_mql5_calendar, parse_mql5_csv, _row_date
from src.data.fetch.massive import fetch_massive_market_data, MissingCredentialsError as MassiveMissingCreds
from src.data.validation.market import UnsupportedTimeframeError
from src.data.fetch.fred import fetch_all_fred_series, MissingCredentialsError as FredMissingCreds
from src.data.normalize.mql5 import normalize_mql5_rows
from src.data.normalize.fred import normalize_fred_official_events
from src.data.normalize.io import merge_write_events
from src.data.normalize.market import normalize_market_symbol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_historical_data")

ALL_SOURCES = ["mql5", "forex_factory", "massive", "fred", "bls"]
DEFAULT_SOURCES = ["mql5", "forex_factory", "massive", "fred"]  # bls excluded: not implemented (see fetch/bls.py)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD, defaults to config historical.start_date")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, defaults to config historical.end_date (or today)")
    parser.add_argument(
        "--sources", type=str, default=",".join(DEFAULT_SOURCES),
        help=f"comma-separated subset of {ALL_SOURCES} (bls is a documented stub, not fetched by default)",
    )
    parser.add_argument("--symbols", type=str, default=None, help="comma-separated symbols override for Massive")
    parser.add_argument("--force", action="store_true", help="re-fetch even if manifest says complete")
    parser.add_argument("--mql5-input-csv", help="MQL5 export CSV to ingest")
    parser.add_argument("--macro-country", help="override macro country for this run")
    parser.add_argument("--macro-currency", help="override macro currency for this run")
    return parser.parse_args(argv)


def validate_sources(requested: list) -> list:
    unknown = [s for s in requested if s not in ALL_SOURCES]
    if unknown:
        raise SystemExit(
            f"error: unsupported source(s) {unknown} -- valid sources are {ALL_SOURCES}"
        )
    return requested


def run_mql5(config, manifest, event_mapping, start, end) -> dict:
    report = ingest_mql5_calendar(config, manifest, start, end)
    events = []
    if report.csv_path.exists():
        all_rows = parse_mql5_csv(report.csv_path)
        # The exporter CSV may contain far more history than THIS run's
        # requested [start, end] (e.g. it was exported once for the full
        # 2016-2026 range, but this invocation only asked for January
        # 2024) -- normalized output must respect the requested range,
        # not silently ingest everything the CSV happens to contain.
        in_range_rows = []
        for row in all_rows:
            try:
                d = _row_date(row)
            except ValueError:
                continue
            if start <= d <= end:
                in_range_rows.append(row)
        acquisition_ts = dt.datetime.fromtimestamp(report.csv_path.stat().st_mtime, tz=dt.timezone.utc)
        broker_timezone = config.provider("mql5").get("broker_timezone")
        events = normalize_mql5_rows(in_range_rows, event_mapping, acquisition_ts, broker_timezone=broker_timezone)
        merge_write_events(events, config.interim_root / "macro" / "mql5_events.parquet")
    return {
        "covered_months": len(report.covered_months),
        "missing_months": report.missing_months,
        "coverage_inferred": report.coverage_inferred,
        "raw_rows": report.total_rows,
        "normalized_events": len(events),
        "ok": len(report.missing_months) == 0,
    }


def run_forex_factory(config, manifest, event_mapping, start, end, force) -> dict:
    """Historical Forex Factory data is NOT fetched live here anymore.
    forexfactory.com serves an active Cloudflare managed challenge
    against automated requests (confirmed: a plain `requests` GET to
    /calendar gets a 403 with `cf-mitigated: challenge`, a "Just a
    moment..." JS-verification page -- reproduced with the exact same
    request this pipeline used to send) -- retrying that on every
    bootstrap/update run would just record an identical, deterministic
    403 forever, never actually collecting anything.

    Historical coverage instead comes entirely from
    `scripts/import_forex_factory.py`, which a human runs after manually
    saving calendar pages from their own browser into
    data/raw/forex_factory_downloads/ -- that importer ALREADY writes
    normalized events directly into forex_factory_events.parquet at
    import time, so this function's only job is to VERIFY what the
    manifest (populated by that importer) says is covered for
    [start, end] and clearly report any gap, never to fetch or
    re-normalize anything itself. `force` is accepted for call-site
    compatibility with the other `run_*` functions but has no effect --
    there is nothing here a flag could force a retry of.
    """
    from src.data.fetch.forex_factory import MANIFEST_KEY as FF_MANIFEST_KEY

    gaps = manifest.coverage_gaps("forex_factory", FF_MANIFEST_KEY, start, end)
    if gaps:
        for gap_start, gap_end in gaps:
            logger.warning(
                "[ForexFactory] historical coverage MISSING for %s -> %s -- Forex Factory is no "
                "longer fetched live (forexfactory.com blocks automated requests). Save the "
                "calendar page(s) for this range in your own browser, add them under "
                "data/raw/forex_factory_downloads/<year>/, then run: "
                "python scripts/import_forex_factory.py data/raw/forex_factory_downloads",
                gap_start, gap_end,
            )
    watermark = manifest.completion_watermark("forex_factory", FF_MANIFEST_KEY, start)
    return {
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "coverage_gaps": [f"{g[0]} -> {g[1]}" for g in gaps],
        "watermark": watermark.isoformat() if watermark else None,
        "ok": len(gaps) == 0,
    }


def run_massive(config, manifest, symbols, start, end, force) -> dict:
    per_symbol = fetch_massive_market_data(config, manifest, symbols, start, end, config.market_timeframe, force=force)
    summary = {}
    any_failed = False
    for symbol, results in per_symbol.items():
        bars = sum(r.rows for r in results)
        failed = [r for r in results if r.status == "failed"]
        any_failed = any_failed or bool(failed)
        summary[symbol] = {"bars_fetched": bars, "failed_months": len(failed)}
        try:
            normalize_market_symbol(config, symbol, start, end, manifest=manifest)
        except FileNotFoundError as exc:
            logger.warning("[Massive][%s] normalize skipped: %s", symbol, exc)
    summary["ok"] = not any_failed
    return summary


def run_fred(config, manifest, event_mapping, start, end) -> dict:
    paths = fetch_all_fred_series(config, manifest, start, end)
    events = normalize_fred_official_events(config, manifest, event_mapping, start)
    merge_write_events(events, config.interim_root / "macro" / "fred_events.parquet")
    series_ids = [item["series_id"] for item in config.provider("fred").get("official_series", [])]
    unique_series = sorted(set(series_ids))
    fetched_ok = [sid for sid in unique_series if sid in paths]
    failed = [sid for sid in unique_series if sid not in paths]
    return {
        "series_fetched": fetched_ok,
        "series_failed": failed,
        "normalized_official_events": len(events),
        "ok": len(failed) == 0,
    }


def run_bls(config, manifest, start, end) -> dict:
    from src.data.fetch.bls import configured_series_ids, fetch_all_bls_series

    series_ids = configured_series_ids(config)
    if not series_ids:
        # Real adapter, deliberately configured with 0 series by default
        # (FRED already covers the currently-mapped indicators -- see
        # fetch/bls.py). Requesting --sources bls with nothing configured
        # must still be a VISIBLE, truthful non-success, not a silent
        # no-op that looks identical to a successful run.
        msg = "0 series configured in providers.bls.series -- nothing to fetch (see config/data_sources.yaml)"
        logger.error("[BLS] %s", msg)
        return {"ok": False, "series_fetched": [], "error": msg}

    results = fetch_all_bls_series(config, manifest, start, end)
    failed = [sid for sid in series_ids if sid not in results]
    return {
        "series_fetched": list(results.keys()),
        "series_failed": failed,
        "ok": len(failed) == 0,
    }


def main(argv=None) -> int:
    load_dotenv_if_present()
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)

    args = parse_args(argv)
    config = config.with_mql5_overrides(args.mql5_input_csv, args.macro_country, args.macro_currency)
    sources = validate_sources([s.strip() for s in args.sources.split(",") if s.strip()])

    start = dt.date.fromisoformat(args.start) if args.start else config.start_date
    end = dt.date.fromisoformat(args.end) if args.end else config.end_date
    if start > end:
        raise SystemExit(f"error: --start {start} is after --end {end}")

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.market_symbols

    run_started_at = utcnow_iso()
    logger.info("Historical bootstrap: %s -> %s | sources=%s", start, end, sources)

    summary = {}

    if "mql5" in sources:
        logger.info("=== MQL5 Economic Calendar ===")
        summary["mql5"] = run_mql5(config, manifest, event_mapping, start, end)

    if "forex_factory" in sources:
        logger.info("=== Forex Factory ===")
        summary["forex_factory"] = run_forex_factory(config, manifest, event_mapping, start, end, args.force)

    if "massive" in sources:
        logger.info("=== Massive market data ===")
        try:
            summary["massive"] = run_massive(config, manifest, symbols, start, end, args.force)
        except MassiveMissingCreds as exc:
            logger.error("[Massive] skipped: %s", exc)
            summary["massive"] = {"error": str(exc), "ok": False}
        except UnsupportedTimeframeError as exc:
            logger.error("[Massive] rejected before fetching: %s", exc)
            summary["massive"] = {"error": str(exc), "ok": False}

    if "fred" in sources:
        logger.info("=== FRED (official validation series) ===")
        try:
            summary["fred"] = run_fred(config, manifest, event_mapping, start, end)
        except FredMissingCreds as exc:
            logger.error("[FRED] skipped: %s", exc)
            summary["fred"] = {"error": str(exc), "ok": False}

    if "bls" in sources:
        logger.info("=== BLS (not implemented -- see fetch/bls.py) ===")
        summary["bls"] = run_bls(config, manifest, start, end)

    this_run_failures = [e for e in manifest.failed_entries() if e.retrieved_at >= run_started_at]
    preexisting_failures = [e for e in manifest.failed_entries() if e.retrieved_at < run_started_at]

    print("\n" + "=" * 60)
    print("HISTORICAL BOOTSTRAP SUMMARY")
    print("=" * 60)
    for source, result in summary.items():
        print(f"[{source}] {result}")
    print(f"Failed chunks recorded THIS RUN: {len(this_run_failures)}")
    if preexisting_failures:
        print(
            f"Pre-existing unresolved failures from earlier runs "
            f"(not necessarily touched this run): {len(preexisting_failures)}"
        )
    print("=" * 60)

    provider_level_failure = any(isinstance(r, dict) and r.get("ok") is False for r in summary.values())
    success = not provider_level_failure and not this_run_failures
    if not success:
        logger.error("Bootstrap completed WITH FAILURES -- see summary above and the manifest for details.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
