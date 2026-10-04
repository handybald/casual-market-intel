#!/usr/bin/env python3
"""Validate ingested data.

    python scripts/validate_data.py

Runs cross-source macro validation (MQL5 vs Forex Factory vs official
FRED-derived values) and, for every registered market provider with
stored data (Massive, Alpaca/<feed>, ...), per-symbol/year OHLCV sanity checks -- including
real NYSE session coverage and macro-release-window coverage -- over
whatever has already been fetched. Read-only: never modifies raw or
interim data, never fills gaps -- it only reports, and persists its
reports under data/manifests/validation_reports/ so results are
inspectable without re-running.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import datetime as dt
from typing import List, Optional, Tuple

import pandas as pd

from src.data.redaction import install_log_redaction
from src.data.config import load_config
from src.data.manifest import Manifest
from src.data.market_exceptions import load_registry_for_config
from src.data.fetch.market_provider import market_provider_class, market_provider_names, parse_timeframe
from src.data.normalize.io import read_events
from src.data.validation.macro import compare_macro_sources, summarize
from src.data.validation.market import UnsupportedTimeframeError, timeframe_minutes_from_parts, validate_market_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
install_log_redaction()
logger = logging.getLogger("validate_data")


def load_all_macro_events(config):
    events = []
    for name in ("mql5_events.parquet", "forex_factory_events.parquet", "fred_events.parquet"):
        path = config.interim_root / "macro" / name
        events.extend(read_events(path))
    return events


def validate_macro(config, report_dir: Path):
    print("\n--- Macro cross-source validation ---")
    events = load_all_macro_events(config)

    if not events:
        print("No normalized macro events found in data/interim/macro/. Run fetch_historical_data.py first.")
        return [], []

    results = compare_macro_sources(events)
    counts = summarize(results)
    print(f"Compared {len(results)} event/reference-period groups across sources.")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    mismatches = [r for r in results if (r.status if isinstance(r.status, str) else r.status.value) == "MISMATCH"]
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH events (showing up to 10):")
        for r in mismatches[:10]:
            print(f"  {r.event_family} {r.reference_key}: {r.values} (units={r.units})")

    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / "macro_validation.json"
    out_path.write_text(
        json.dumps({"counts": counts, "results": [r.model_dump(mode="json") for r in results]}, indent=2),
        encoding="utf-8",
    )
    print(f"Report written to {out_path}")
    return events, results


def _merged_requested_intervals(
    manifest: Manifest, key: str, year: int, provider: str = "massive"
) -> List[Tuple[dt.date, dt.date]]:
    """Union of every manifest entry's [start, end] for this (provider,
    key), clipped to `year`, merging overlapping/adjacent ranges into
    contiguous intervals. This is deliberately based on what was
    actually REQUESTED (every attempted entry, regardless of outcome --
    a "failed" entry is still a real request whose missing sessions
    should be visible), not on the min/max timestamp actually present in
    the stored parquet: inferring from stored bars would hide a
    genuinely missing edge (e.g. the last few days of a fetch that
    silently wrote zero rows) behind an artificially-shrunk "requested"
    range that always matches whatever happens to be on disk."""
    year_start, year_end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    raw_intervals = sorted(
        (e.start_date(), e.end_date()) for e in manifest.entries_for(provider, key)
    )
    clipped = []
    for start, end in raw_intervals:
        cs, ce = max(start, year_start), min(end, year_end)
        if cs <= ce:
            clipped.append((cs, ce))

    merged: List[Tuple[dt.date, dt.date]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1] + dt.timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def validate_market(config, macro_events, report_dir: Path) -> bool:
    """Returns True if any report has a HARD failure (invalid OHLCV,
    missing sessions, etc.) -- the signal `main()` uses for its exit
    code. A macro-window coverage gap or a raw gap/volume-outlier alone
    does not fail the run (see MarketValidationReport.is_hard_failure);
    genuine data-integrity problems do.

    Each stored year-parquet is validated only against the sub-range(s)
    actually requested for its exact (symbol, timeframe, adjustment) key
    per the fetch manifest -- NOT the full Jan1-Dec31 calendar year --
    bounded by elapsed time via the real acquisition instant
    (`validate_market_bars`'s `as_of` default). A partial-year or
    current-year-in-progress dataset must not have its never-requested
    months reported as missing sessions.

    Every registered market provider configured in this config is
    validated separately, against its own manifest entries, with the
    hard-failure policy its declared `bar_density` implies (see
    validation/market.py). Providers are never pooled."""
    print("\n--- Market OHLCV validation ---")
    # Datasets are discovered from STORAGE (every provider x stored variant,
    # e.g. Alpaca iex AND sip), not from the active fetch config, so a
    # corrupted dataset the config does not currently select is still found.
    providers, unknown_dirs = [], []
    for name in market_provider_names():
        if not config.has_provider(name):
            continue
        try:
            found, unknown = market_provider_class(name).stored_datasets(config)
        except ValueError as exc:
            print(f"[{name}] SKIPPED -- invalid provider configuration: {exc}")
            continue
        providers.extend(found)
        unknown_dirs.extend(unknown)
    for d in unknown_dirs:
        print(f"[{d}] HARD FAILURE -- stored market data directory does not map to a known dataset "
              f"(provider/feed); it cannot be validated without guessing its semantics")
    if not providers and not unknown_dirs:
        print("No market raw data found for any configured provider (e.g. data/raw/massive/). "
              "Run fetch_historical_data.py first.")
        return False

    # Macro-release-window coverage is a precision-sensitive, minute-level
    # join -- only CONFIRMED-quality timestamps (an operator-verified
    # broker/display timezone) are trusted for it. ASSUMED (e.g. Forex
    # Factory's unverified default display timezone) or TENTATIVE/
    # UNRESOLVED rows are excluded here: promoting an unverified
    # assumption into a minute-level check would silently overstate how
    # precisely we actually know these release times. See schemas.py
    # MacroEvent.timestamp_is_trustworthy and config
    # providers.forex_factory.display_timezone_verified.
    release_timestamps = [e.release_timestamp_utc for e in macro_events if e.timestamp_is_trustworthy]

    manifest = Manifest(config.manifest_path)
    any_hard_failure = False
    report_dir.mkdir(parents=True, exist_ok=True)
    registry = load_registry_for_config(config)
    if registry.entries:
        print(f"Known market exception registry v{registry.registry_version}: {len(registry.entries)} entries "
              f"({registry.source_path}) -- registered exceptions are classified, never filled.")
    for provider in providers:
        if _validate_provider_market(provider, manifest, release_timestamps, report_dir, registry):
            any_hard_failure = True
    return any_hard_failure or bool(unknown_dirs)


def _validate_provider_market(provider, manifest, release_timestamps, report_dir: Path, registry) -> bool:
    caps = provider.capabilities()
    tag = f"[{provider.dataset_label}]"
    any_hard_failure = False
    for symbol_dir in sorted(provider.raw_dataset_root().iterdir()):
        if not symbol_dir.is_dir():
            continue
        symbol = symbol_dir.name
        for timeframe_dir in sorted(symbol_dir.iterdir()):
            if not timeframe_dir.is_dir():
                continue
            timeframe = timeframe_dir.name
            for adj_dir in sorted(timeframe_dir.iterdir()):
                if not adj_dir.is_dir():
                    continue
                adj_label = adj_dir.name  # adjustment policy label, e.g. "raw"
                key = provider.cache_key(symbol, timeframe, adj_label)

                n, timespan = parse_timeframe(timeframe)
                try:
                    # Shared with fetch-time validation (fetch/massive.py
                    # _validate_window) -- the two entry points must never
                    # diverge on what a given timeframe means. See
                    # validation/market.py's timeframe_minutes_from_parts.
                    timeframe_minutes = timeframe_minutes_from_parts(n, timespan)
                except UnsupportedTimeframeError as exc:
                    print(f"{tag}[{symbol}][{timeframe}][{adj_label}] SKIPPED -- {exc}")
                    continue

                for parquet_path in sorted(adj_dir.glob("*.parquet")):
                    year = int(parquet_path.stem)
                    df = pd.read_parquet(parquet_path)

                    intervals = _merged_requested_intervals(manifest, key, year, provider.name)
                    if not intervals:
                        print(
                            f"{tag}[{symbol}][{timeframe}][{adj_label}][{year}] SKIPPED -- no manifest "
                            f"entries found for this key; cannot determine requested coverage "
                            f"without guessing, so not validating session completeness for this file."
                        )
                        continue

                    reports = [
                        validate_market_bars(
                            df, symbol, start, end,
                            timeframe_minutes=timeframe_minutes,
                            macro_release_timestamps_utc=release_timestamps,
                            bar_density=caps.bar_density,
                            exceptions=registry.for_dataset(symbol, provider.name, caps.feed),
                        )
                        for start, end in intervals
                    ]

                    file_hard_failure = any(r.is_hard_failure for r in reports)
                    if file_hard_failure:
                        any_hard_failure = True

                    for (start, end), report in zip(intervals, reports):
                        status = ("CLEAN" if report.is_clean else "HARD FAILURE" if report.is_hard_failure
                                  else "KNOWN PROVIDER GAPS" if report.known_gap_minutes else "ISSUES")
                        print(
                            f"{tag}[{symbol}][{timeframe}][{adj_label}][{year}] "
                            f"requested={start.isoformat()}..{end.isoformat()} {status} rows={report.total_rows} "
                            f"missing_sessions={len(report.missing_sessions)} gaps={report.gap_count} "
                            f"largest_gap_min={report.largest_gap_minutes:.1f} "
                            f"regular_hours_coverage={report.regular_hours_coverage_ratio:.1%} "
                            f"macro_windows_missing={len(report.macro_windows_missing_coverage)}"
                        )
                        if report.known_gap_minutes or report.excluded_regular_minutes:
                            # Classified separately: a registered provider gap is a known, still-missing
                            # scientific gap; a market-wide halt is not data corruption at all.
                            print(f"    known provider-gap minutes={report.known_gap_minutes} "
                                  f"(sessions {report.known_gap_sessions}); excluded no-trade minutes="
                                  f"{report.excluded_minutes_by_class}; unexplained missing minutes="
                                  f"{report.unexplained_missing_minutes}")
                        for issue in report.issues:
                            print(f"    - {issue}")

                    out_path = report_dir / f"{provider.stored_report_prefix}_{symbol}_{timeframe}_{adj_label}_{year}.json"
                    out_path.write_text(
                        json.dumps(
                            {
                                "symbol": symbol, "timeframe": timeframe, "adjustment": adj_label, "year": year,
                                "provider_capabilities": caps.to_dict(),
                                "requested_intervals": [f"{s.isoformat()}..{e.isoformat()}" for s, e in intervals],
                                "reports": [r.to_dict() for r in reports],
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

    return any_hard_failure


def main() -> int:
    config = load_config()
    report_dir = REPO_ROOT / "data" / "manifests" / "validation_reports"
    macro_events, _ = validate_macro(config, report_dir)
    any_hard_failure = validate_market(config, macro_events, report_dir)
    if any_hard_failure:
        logger.error("Validation found HARD failures in stored market data -- see reports above/in %s", report_dir)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
