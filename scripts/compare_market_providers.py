#!/usr/bin/env python3
"""Cross-provider market-bar comparison (descriptive discrepancy report).

    python scripts/compare_market_providers.py --providers massive,alpaca \\
        --symbols QQQ --start 2024-01-02 --end 2024-01-31

Reads already-stored raw bars only (nothing is fetched) for exactly two
providers, aligns them on bar-start timestamps, and writes one JSON report
per symbol to data/reports/provider_comparison_<a>_vs_<b>_<SYM>_<timeframe>_<adjustment>_<start>_<end>.json
(see src/data/validation/cross_provider.py for every metric).

Disagreement between providers is MEASURED, not judged: different feeds
legitimately differ (e.g. IEX is one venue). Each side's manifest coverage
for the window is included, so a coverage difference can be traced to a
failed/missing/provisional fetch rather than mistaken for a feed property.

EXIT CODE: 0 if every symbol was compared and neither side has a hard
integrity failure; 1 if a side cannot be compared -- no stored file at
all ("NO STORED FILE") or a file without a single bar inside the
requested window ("NO BARS IN WINDOW"), both sides listed when both are
affected -- or has a hard integrity failure; 2 for invalid arguments or an incompatible pair
(different timeframe / adjustment policy).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from src.data.config import load_config  # noqa: E402
from src.data.fetch.market_provider import MarketDataProvider, get_market_provider, parse_timeframe  # noqa: E402
from src.data.manifest import Manifest  # noqa: E402
from src.data.validation.cross_provider import (  # noqa: E402
    DatasetSide, IncompatibleComparisonError, bars_in_window, compare_market_bars,
)
from src.data.validation.market import timeframe_minutes_from_parts  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--providers", required=True, help="exactly two, comma-separated, e.g. massive,alpaca")
    p.add_argument("--start", required=True, help="inclusive YYYY-MM-DD (NYSE dates)")
    p.add_argument("--end", required=True, help="inclusive YYYY-MM-DD")
    p.add_argument("--symbols", help="comma-separated (default: config market.symbols)")
    p.add_argument("--report-dir", type=Path, help="default: <data>/reports")
    return p


def load_stored_bars(provider: MarketDataProvider, symbol: str, timeframe: str, start: dt.date, end: dt.date) -> Optional[pd.DataFrame]:
    frames = []
    for year in range(start.year, end.year + 1):
        path = provider.year_path(symbol, timeframe, year)
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    # Raw year files are written by this pipeline from tz-aware UTC
    # datetimes; a naive column here is surfaced as an integrity failure
    # by the comparison, never silently localized.
    return df


def side_for(provider: MarketDataProvider, timeframe: str) -> DatasetSide:
    caps = provider.capabilities()
    return DatasetSide(label=provider.dataset_label, source=caps.source_label, feed=caps.feed,
                       feed_scope=caps.feed_scope, adjustment=caps.adjustment, timeframe=timeframe)


def manifest_coverage(manifest: Manifest, provider: MarketDataProvider, symbol: str, timeframe: str,
                      start: dt.date, end: dt.date) -> list:
    gaps = manifest.classify_gaps(provider.name, provider.cache_key(symbol, timeframe), start, end, today=dt.date.today())
    return [{"start": g.start.isoformat(), "end": g.end.isoformat(), "reason": g.reason} for g in gaps]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if end < start:
        print(f"ERROR: --end {end} is before --start {start}", file=sys.stderr)
        return 2
    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    if len(names) != 2 or names[0] == names[1]:
        print("ERROR: --providers needs exactly two different providers", file=sys.stderr)
        return 2

    config = load_config()
    try:
        prov_a, prov_b = (get_market_provider(n, config) for n in names)
    except (KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    timeframe = config.market_timeframe
    tf_minutes = timeframe_minutes_from_parts(*parse_timeframe(timeframe))
    side_a, side_b = side_for(prov_a, timeframe), side_for(prov_b, timeframe)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(config.market_symbols)
    manifest = Manifest(config.manifest_path)
    report_dir = args.report_dir or (config.interim_root.parent / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    for symbol in symbols:
        a_df = load_stored_bars(prov_a, symbol, timeframe, start, end)
        b_df = load_stored_bars(prov_b, symbol, timeframe, start, end)
        problems = []
        for p, df in ((prov_a, a_df), (prov_b, b_df)):
            if df is None:
                problems.append(f"{p.dataset_label}: NO STORED FILE for {start.year}..{end.year}")
            elif bars_in_window(df, start, end) == 0:
                problems.append(f"{p.dataset_label}: NO BARS IN WINDOW {start}..{end} (stored file(s) exist, "
                                f"{len(df)} bar(s) all outside the window)")
        if problems:
            # An inability to compare -- not a provider discrepancy, never a pass.
            print(f"[{symbol}] NOT COMPARED -- " + "; ".join(problems) + ". Fetch the window first, e.g. "
                  f"python scripts/fetch_historical_data.py --sources {','.join(names)} "
                  f"--symbols {symbol} --start {start} --end {end}")
            exit_code = max(exit_code, 1)
            continue
        try:
            report = compare_market_bars(a_df, b_df, side_a, side_b, symbol, start, end, timeframe_minutes=tf_minutes)
        except IncompatibleComparisonError as exc:
            print(f"[{symbol}] NOT COMPARABLE -- {exc}")
            return 2
        report["manifest_coverage_gaps"] = {
            side_a.label: manifest_coverage(manifest, prov_a, symbol, timeframe, start, end),
            side_b.label: manifest_coverage(manifest, prov_b, symbol, timeframe, start, end),
        }
        out = report_dir / (f"provider_comparison_{side_a.label}_vs_{side_b.label}_{symbol}_{timeframe}_"
                            f"{side_a.adjustment}_{start}_{end}.json")
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        _print_summary(report, side_a.label, side_b.label)
        print(f"    report: {out}")
        if not report["integrity_ok"]:
            exit_code = max(exit_code, 1)
    return exit_code


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.6g}"


def _print_summary(report: dict, a: str, b: str) -> None:
    sym = report["symbol"]
    for label, issues in report["hard_integrity_failures"].items():
        for issue in issues:
            print(f"[{sym}] HARD INTEGRITY FAILURE in {label}: {issue}")
    for note in report["compatibility"]["notes"]:
        print(f"[{sym}] note: {note}")
    for label, gaps in report["manifest_coverage_gaps"].items():
        if gaps:
            print(f"[{sym}] {label} manifest gaps in window: {[(g['start'], g['end'], g['reason']) for g in gaps]}")
    d = report["descriptive"]
    if d is None:
        return
    cov = d["coverage"]
    print(f"[{sym}] bars {a}={cov['bars'][a]['total']} {b}={cov['bars'][b]['total']} in_both={cov['in_both']['total']} "
          f"only_{a}={cov[f'only_in_{a}']['total']} only_{b}={cov[f'only_in_{b}']['total']} "
          f"overlap/union={_fmt(cov['overlap_over_union'])}")
    if d["price_differences"]:
        c = d["price_differences"]["close"]
        print(f"[{sym}] close abs diff median={_fmt(c['abs_diff']['all']['median'])} p95={_fmt(c['abs_diff']['all']['p95'])} "
              f"max={_fmt(c['abs_diff']['all']['max'])}; rel diff p95={_fmt(c['rel_diff']['all']['p95'])}")
        v = d["volume_differences"]
        print(f"[{sym}] volume ratio {a}/{b} median={_fmt(v[f'ratio_{a}_over_{b}']['median'])} "
              f"(comparable as full-market: {v['comparable_as_full_market']})")
        r = d["return_correlation"]
        print(f"[{sym}] 1-bar return corr all={_fmt(r['all']['corr'])} (n={r['all']['n']}) "
              f"regular={_fmt(r['regular_session']['corr'])} (n={r['regular_session']['n']})")
    sp = d["session_presence"]
    for key in (f"sessions_with_regular_bars_only_in_{a}", f"sessions_with_regular_bars_only_in_{b}",
                "sessions_with_no_regular_bars_in_either"):
        if sp[key]:
            print(f"[{sym}] {key}: {sp[key]}")


if __name__ == "__main__":
    sys.exit(main())
