#!/usr/bin/env python3
"""Build macro-release -> market-response features (long format: one row per release x symbol).

    python3 scripts/build_macro_market_response.py --start 2025-09-01 --end 2025-09-30 --symbols QQQ,SPY

Events are the calendar-anchored, validated releases from the macro validation layer (never derived from market
data). The event time is the TRUSTED release timestamp only: a CONFIRMED Forex Factory time, or an MQL5 time under
an explicitly validated broker timezone (--mql5-broker-timezone plus enough timezone evidence); otherwise the row is
NO_TRUSTED_EVENT_TIMESTAMP. Reads local normalized files only (macro parquet + Massive 1-minute parquet); nothing is
fetched. Writes data/processed/macro_market_response.parquet and data/reports/macro_market_response_<start>_<end>.csv.
Feature definitions and the leakage rules live in src/features/market_response.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from src.data.config import load_config  # noqa: E402
from src.data.validation.macro_events import SCOPE_FAMILIES, ValidationConfig  # noqa: E402
from src.data.validation.macro_release_pipeline import run_macro_validation  # noqa: E402
from src.features.market_response import (  # noqa: E402
    RESPONSE_COLUMNS, ResponseConfig, build_market_response, load_massive_bars,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", required=True, help="inclusive YYYY-MM-DD (release-date window)")
    p.add_argument("--end", required=True, help="inclusive YYYY-MM-DD")
    p.add_argument("--symbols", help="comma-separated (default: config market.symbols)")
    p.add_argument("--mql5-broker-timezone", help="explicit IANA zone for MQL5 server times (never inferred)")
    p.add_argument("--timezone-evidence-start")
    p.add_argument("--timezone-evidence-end")
    p.add_argument("--ff-events", type=Path)
    p.add_argument("--mql5-events", type=Path)
    p.add_argument("--fred-events", type=Path)
    p.add_argument("--massive-dir", type=Path, help="default: <interim>/massive")
    p.add_argument("--out-parquet", type=Path, help="default: <data>/processed/macro_market_response.parquet")
    p.add_argument("--report-dir", type=Path, help="default: <data>/reports")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
        ev_window = ((dt.date.fromisoformat(args.timezone_evidence_start), dt.date.fromisoformat(args.timezone_evidence_end))
                     if args.timezone_evidence_start or args.timezone_evidence_end else None)
    except (TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if end < start:
        print(f"ERROR: --end {end} is before --start {start}", file=sys.stderr)
        return 2

    config = load_config()
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(config.market_symbols)
    result, _, _ = run_macro_validation(
        start, end, config=config, cfg=ValidationConfig(mql5_broker_timezone=args.mql5_broker_timezone, families=SCOPE_FAMILIES),
        paths={"forex_factory": args.ff_events, "mql5": args.mql5_events, "fred": args.fred_events},
        timezone_evidence_window=ev_window)
    events = result.rows
    print(f"[market response] {len(events)} validated calendar releases in {start} -> {end}; symbols={symbols}")

    massive_dir = args.massive_dir or (config.interim_root / "massive")
    stamps = [dt.datetime.fromisoformat(e["trusted_release_timestamp_utc"]) for e in events if e["trusted_release_timestamp_utc"]]
    bars = {}
    for sym in symbols:
        if stamps:
            lo, hi = min(stamps) - dt.timedelta(hours=1), max(stamps) + dt.timedelta(hours=2)
            bars[sym] = load_massive_bars(sym, lo, hi, massive_dir)
        else:
            bars[sym] = None
        print(f"[market response] {sym}: {'no data file' if bars[sym] is None else str(len(bars[sym])) + ' bars loaded'}")

    rows = build_market_response(events, bars, ResponseConfig())
    df = pd.DataFrame(rows, columns=RESPONSE_COLUMNS)
    int_cols = [c for c in RESPONSE_COLUMNS if c.startswith("direction_")] + [
        "bars_expected", "bars_found", "missing_bar_count", "missing_pre_bars", "missing_post_bars"]
    df[int_cols] = df[int_cols].astype("Int64")          # nullable ints: null stays null, never NaN-as-float

    data_root = config.interim_root.parent
    out_parquet = args.out_parquet or (data_root / "processed" / "macro_market_response.parquet")
    report_dir = args.report_dir or (data_root / "reports")
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_parquet, index=False)
    csv_path = report_dir / f"macro_market_response_{start}_{end}.csv"
    df.to_csv(csv_path, index=False)

    print("\nmarket_window_status:", df["market_window_status"].value_counts().to_dict())
    print("release_timestamp_basis:", df["release_timestamp_basis"].value_counts().to_dict())
    print(f"\nWrote {out_parquet}\nWrote {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
