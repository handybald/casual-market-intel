#!/usr/bin/env python3
"""Validate ingested data.

    python scripts/validate_data.py

Runs cross-source macro validation (MQL5 vs Forex Factory vs official)
and per-symbol/year OHLCV sanity checks over whatever has already been
fetched. Read-only: never modifies raw or interim data, never fills
gaps -- it only reports.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from src.data.config import load_config
from src.data.normalize.io import read_events
from src.data.validation.macro import compare_macro_sources, summarize
from src.data.validation.market import validate_market_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("validate_data")


def validate_macro(config) -> None:
    print("\n--- Macro cross-source validation ---")
    events = []
    for name in ("mql5_events.parquet", "forex_factory_events.parquet"):
        path = config.interim_root / "macro" / name
        events.extend(read_events(path))

    if not events:
        print("No normalized macro events found in data/interim/macro/. Run fetch_historical_data.py first.")
        return

    results = compare_macro_sources(events)
    counts = summarize(results)
    print(f"Compared {len(results)} event/date groups across sources.")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    mismatches = [r for r in results if str(r.status) == "MISMATCH" or getattr(r.status, "value", None) == "MISMATCH"]
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH events (showing up to 10):")
        for r in mismatches[:10]:
            print(f"  {r.event_family} {r.release_timestamp_utc.date()}: {r.values}")


def validate_market(config) -> None:
    print("\n--- Market OHLCV validation ---")
    raw_root = config.provider_raw_dir("massive")
    if not raw_root.exists():
        print("No Massive raw data found in data/raw/massive/. Run fetch_historical_data.py first.")
        return

    for symbol_dir in sorted(raw_root.iterdir()):
        if not symbol_dir.is_dir():
            continue
        symbol = symbol_dir.name
        for parquet_path in sorted(symbol_dir.glob("*.parquet")):
            year = int(parquet_path.stem)
            df = pd.read_parquet(parquet_path)
            report = validate_market_bars(df, symbol, year)
            status = "CLEAN" if report.is_clean else "ISSUES"
            print(
                f"[{symbol}][{year}] {status} rows={report.total_rows} "
                f"gaps={report.gap_count} largest_gap_min={report.largest_gap_minutes:.1f} "
                f"regular_hours_coverage={report.regular_hours_coverage_ratio:.1%}"
            )
            for issue in report.issues:
                print(f"    - {issue}")


def main() -> int:
    config = load_config()
    validate_macro(config)
    validate_market(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
