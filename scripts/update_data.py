#!/usr/bin/env python3
"""Incremental delta update.

    python scripts/update_data.py

For each provider/key (MQL5 country+currency, Forex Factory, each
Massive symbol, each FRED series), inspects data/manifests/fetch_manifest.json
for the latest successfully-completed end date and fetches only the
missing period since then -- never a full historical re-download.

Historical bootstrap (scripts/fetch_historical_data.py) happens once;
this is what runs on a schedule afterwards.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.config import load_config, load_dotenv_if_present
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest
from src.data.fetch.massive import fetch_massive_symbol, MissingCredentialsError as MassiveMissingCreds
from src.data.fetch.fred import fetch_fred_series, MissingCredentialsError as FredMissingCreds
from src.data.normalize.market import normalize_market_symbol

import fetch_historical_data as bootstrap  # reuses run_mql5 / run_forex_factory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("update_data")

ALL_SOURCES = ["mql5", "forex_factory", "massive", "fred"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--sources", type=str, default=",".join(ALL_SOURCES))
    parser.add_argument("--symbols", type=str, default=None)
    return parser.parse_args()


def resolve_delta_start(manifest: Manifest, provider: str, key: str, fallback_start: dt.date) -> dt.date:
    last_end = manifest.latest_complete_end(provider, key)
    if last_end is None:
        return fallback_start
    return last_end + dt.timedelta(days=1)


def main() -> int:
    load_dotenv_if_present()
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)

    args = parse_args()
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.market_symbols

    summary = {}

    if "mql5" in sources:
        key = f"{config.macro_country}:{config.macro_currency}"
        start = resolve_delta_start(manifest, "mql5", key, config.start_date)
        if start > end:
            logger.info("[MQL5] up to date (latest complete end >= %s)", end)
            summary["mql5"] = {"status": "up_to_date"}
        else:
            logger.info("[MQL5] delta range: %s -> %s", start, end)
            summary["mql5"] = bootstrap.run_mql5(config, manifest, event_mapping, start, end)

    if "forex_factory" in sources:
        start = resolve_delta_start(manifest, "forex_factory", "US", config.start_date)
        if start > end:
            logger.info("[ForexFactory] up to date (latest complete end >= %s)", end)
            summary["forex_factory"] = {"status": "up_to_date"}
        else:
            logger.info("[ForexFactory] delta range: %s -> %s", start, end)
            summary["forex_factory"] = bootstrap.run_forex_factory(
                config, manifest, event_mapping, start, end, force=False
            )

    if "massive" in sources:
        massive_summary = {}
        try:
            for symbol in symbols:
                start = resolve_delta_start(manifest, "massive", symbol, config.start_date)
                if start > end:
                    logger.info("[Massive][%s] up to date", symbol)
                    massive_summary[symbol] = {"status": "up_to_date"}
                    continue
                logger.info("[Massive][%s] delta range: %s -> %s", symbol, start, end)
                results = fetch_massive_symbol(
                    config, manifest, symbol, start, end, config.market_timeframe, force=False
                )
                massive_summary[symbol] = {"bars_fetched": sum(r.rows for r in results)}
                try:
                    normalize_market_symbol(config, symbol, start, end)
                except FileNotFoundError as exc:
                    logger.warning("[Massive][%s] normalize skipped: %s", symbol, exc)
        except MassiveMissingCreds as exc:
            logger.error("[Massive] skipped: %s", exc)
            massive_summary["error"] = str(exc)
        summary["massive"] = massive_summary

    if "fred" in sources:
        provider_cfg = config.provider("fred")
        fred_summary = {}
        try:
            for series_id in provider_cfg.get("series", {}):
                start = resolve_delta_start(manifest, "fred", series_id, config.start_date)
                if start > end:
                    fred_summary[series_id] = {"status": "up_to_date"}
                    continue
                logger.info("[FRED][%s] delta range: %s -> %s", series_id, start, end)
                fetch_fred_series(config, manifest, series_id, start, end, force=False)
                fred_summary[series_id] = {"status": "fetched"}
        except FredMissingCreds as exc:
            logger.error("[FRED] skipped: %s", exc)
            fred_summary["error"] = str(exc)
        summary["fred"] = fred_summary

    print("\n" + "=" * 60)
    print("INCREMENTAL UPDATE SUMMARY")
    print("=" * 60)
    for source, result in summary.items():
        print(f"[{source}] {result}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
