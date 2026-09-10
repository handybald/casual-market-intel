#!/usr/bin/env python3
"""One-command historical data bootstrap.

    python scripts/fetch_historical_data.py --start 2016-01-01 --end 2026-09-10

The user specifies ONE date range. Each provider internally determines
its own safe chunk size (monthly pages for Forex Factory, monthly
requests -> yearly parquet for Massive, monthly manifest checkpoints for
MQL5, a single request for FRED), paginates, retries, rate-limits,
resumes from data/manifests/fetch_manifest.json, and deduplicates.
Nothing here loops over months and re-invokes anything manually.

MQL5 is the one source this script cannot fetch itself: the Calendar API
only runs inside MetaTrader. This script detects and ingests whatever
mql5_exporter/EconomicCalendarExporter.mq5 has already produced; if that
CSV is missing or incomplete for the requested range, it reports exactly
which months are missing instead of failing silently or pretending to
have fetched them.
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
from src.data.manifest import Manifest
from src.data.fetch.mql5 import ingest_mql5_calendar
from src.data.fetch.forex_factory import fetch_forex_factory
from src.data.fetch.massive import fetch_massive_market_data, MissingCredentialsError as MassiveMissingCreds
from src.data.fetch.fred import fetch_fred_series_map, MissingCredentialsError as FredMissingCreds
from src.data.normalize.mql5 import normalize_mql5_rows
from src.data.normalize.forex_factory import normalize_forex_factory_file
from src.data.normalize.io import merge_write_events
from src.data.normalize.market import normalize_market_symbol
from src.data.fetch.mql5 import parse_mql5_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_historical_data")

ALL_SOURCES = ["mql5", "forex_factory", "massive", "fred"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD, defaults to config historical.start_date")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, defaults to config historical.end_date (or today)")
    parser.add_argument(
        "--sources", type=str, default=",".join(ALL_SOURCES),
        help=f"comma-separated subset of {ALL_SOURCES}",
    )
    parser.add_argument("--symbols", type=str, default=None, help="comma-separated symbols override for Massive")
    parser.add_argument("--force", action="store_true", help="re-fetch even if manifest says complete")
    return parser.parse_args()


def run_mql5(config, manifest, event_mapping, start, end) -> dict:
    report = ingest_mql5_calendar(config, manifest, start, end)
    events = []
    if report.csv_path.exists():
        rows = parse_mql5_csv(report.csv_path)
        events = normalize_mql5_rows(rows, event_mapping)
        merge_write_events(events, config.interim_root / "macro" / "mql5_events.parquet")
    return {
        "covered_months": len(report.covered_months),
        "missing_months": report.missing_months,
        "raw_rows": report.total_rows,
        "normalized_events": len(events),
    }


def run_forex_factory(config, manifest, event_mapping, start, end, force) -> dict:
    results = fetch_forex_factory(config, manifest, start, end, force=force)
    total_events = 0
    for r in results:
        if r.status not in ("complete", "skipped_cached"):
            continue
        if not r.path.exists():
            continue
        events = normalize_forex_factory_file(
            r.path, r.year, r.month, event_mapping, currency_filter=config.macro_currency
        )
        merge_write_events(events, config.interim_root / "macro" / "forex_factory_events.parquet")
        total_events += len(events)
    failed = [r for r in results if r.status == "failed"]
    return {"months_processed": len(results), "failed_months": len(failed), "normalized_events": total_events}


def run_massive(config, manifest, symbols, start, end, force) -> dict:
    per_symbol = fetch_massive_market_data(
        config, manifest, symbols, start, end, config.market_timeframe, force=force
    )
    summary = {}
    for symbol, results in per_symbol.items():
        bars = sum(r.rows for r in results)
        failed = [r for r in results if r.status == "failed"]
        summary[symbol] = {"bars_fetched": bars, "failed_months": len(failed)}
        try:
            normalize_market_symbol(config, symbol, start, end)
        except FileNotFoundError as exc:
            logger.warning("[Massive][%s] normalize skipped: %s", symbol, exc)
    return summary


def run_fred(config, manifest, start, end, force) -> dict:
    paths = fetch_fred_series_map(config, manifest, start, end, force=force)
    return {"series_fetched": list(paths.keys())}


def main() -> int:
    load_dotenv_if_present()
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)

    args = parse_args()
    start = dt.date.fromisoformat(args.start) if args.start else config.start_date
    end = dt.date.fromisoformat(args.end) if args.end else config.end_date
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.market_symbols

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
            summary["massive"] = {"error": str(exc)}

    if "fred" in sources:
        logger.info("=== FRED (official validation series) ===")
        try:
            summary["fred"] = run_fred(config, manifest, start, end, args.force)
        except FredMissingCreds as exc:
            logger.error("[FRED] skipped: %s", exc)
            summary["fred"] = {"error": str(exc)}

    failed_chunks = len(manifest.failed_entries())

    print("\n" + "=" * 60)
    print("HISTORICAL BOOTSTRAP SUMMARY")
    print("=" * 60)
    for source, result in summary.items():
        print(f"[{source}] {result}")
    print(f"Failed chunks (manifest): {failed_chunks}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
