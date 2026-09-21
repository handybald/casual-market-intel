#!/usr/bin/env python3
"""Calendar-anchored, vintage-aware validation of macro releases (Forex Factory / MQL5 / FRED-ALFRED).

    python3 scripts/validate_macro_events.py --start 2025-09-01 --end 2025-09-30

Reads the normalized interim parquet files only (no network, nothing is fetched or modified) and
writes data/reports/macro_validation_<start>_<end>.{csv,json}. Exit code 0 means the report was
produced, NOT that everything matched: read the status column. Use --strict to exit 1 unless
every row is MATCH.

Rows are the calendar RELEASES (Forex Factory / MQL5) whose release date is in the window; FRED never
creates rows. The calendar `actual` is validated only against the official ALFRED RELEASE-VINTAGE value
(the value as published at the release); today's latest-revised FRED value is shown for diagnostics
only, because it contains later revisions (look-ahead). Missing vintages are reported as
OFFICIAL_VINTAGE_UNAVAILABLE together with the exact scripts/fetch_fred_asof.py commands that would
supply them (this script never fetches). Release date and reference period are separate columns.
Roles: Forex Factory = pre-release provider forecast (not a consensus); MQL5 = primary
actual/previous/revised_previous/release time. See src/data/validation/macro_events.py.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.config import load_config  # noqa: E402
from src.data.event_mapping import load_event_mapping  # noqa: E402
from src.data.normalize.io import read_events  # noqa: E402
from src.data.validation.macro_events import (  # noqa: E402
    MATCH, OFFICIAL_VINTAGE_UNAVAILABLE, ROW_FIELDS, SCOPE_FAMILIES, ValidationConfig, fred_profiles_from_config, reconcile, summarize,
)


def _fmt(v: Any, width: int = 0) -> str:
    if v is None:
        s = "-"
    elif isinstance(v, float):
        s = f"{v:.4g}"
    else:
        s = str(v)
    return s.ljust(width)


TABLE_COLUMNS = [
    ("event", "canonical_event_id", 31), ("release", "release_date", 10), ("ref", "reference_period", 10),
    ("ff_fcst", "ff_provider_forecast", 7), ("actual", "actual_value", 7), ("prev", "mql5_previous", 6),
    ("rev_prev", "mql5_revised_previous", 8), ("off_vintage", "official_release_vintage_value", 11),
    ("off_latest", "official_latest_value", 10), ("actual_vs_official", "release_actual_match_status", 28),
    ("ts", "timestamp_status", 12), ("unit", "unit_status", 10), ("status", "source_match_status", 28),
]


def format_table(rows: Sequence[Dict[str, Any]]) -> str:
    def cell(r, key):
        v = r.get(key)
        return v

    lines = ["  ".join(h.ljust(w) for h, _, w in TABLE_COLUMNS)]
    lines.append("  ".join("-" * w for _, _, w in TABLE_COLUMNS))
    for r in rows:
        lines.append("  ".join(_fmt(cell(r, k), w)[:w].ljust(w) for _, k, w in TABLE_COLUMNS))
    return "\n".join(lines)


def missing_vintage_commands(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """One scripts/fetch_fred_asof.py command per (family, release date) lacking a release vintage. The
    observation window starts 13 months before the reference period so 12-month transforms can be computed."""
    out, seen = [], set()
    for r in rows:
        if r["release_actual_match_status"] != OFFICIAL_VINTAGE_UNAVAILABLE or not r["reference_period"] or not r["release_date"]:
            continue
        key = (r["event_family"], r["release_date"])
        if key in seen:
            continue
        seen.add(key)
        ref = dt.date.fromisoformat(r["reference_period"])
        months = ref.year * 12 + ref.month - 1 - 13
        obs_start = dt.date(months // 12, months % 12 + 1, 1)
        obs_end = (dt.date(ref.year + (ref.month == 12), ref.month % 12 + 1, 1) - dt.timedelta(days=1))
        out.append(f"python3 scripts/fetch_fred_asof.py --event-family {r['event_family']} "
                   f"--observation-start {obs_start} --observation-end {obs_end} --as-of {r['release_date']}")
    return out


def write_reports(rows: List[Dict[str, Any]], meta: Dict[str, Any], out_dir: Path, stem: str) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.json"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in ROW_FIELDS})
    json_path.write_text(json.dumps({"meta": meta, "summary": summarize(rows), "rows": rows}, indent=2,
                                    default=str, allow_nan=False) + "\n", encoding="utf-8")
    return [csv_path, json_path]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", required=True, help="inclusive YYYY-MM-DD (release date range)")
    p.add_argument("--end", required=True, help="inclusive YYYY-MM-DD")
    p.add_argument("--families", help="comma-separated event_family subset (default: employment + CPI scope)")
    p.add_argument("--ff-events", type=Path, help="override forex_factory_events.parquet path")
    p.add_argument("--mql5-events", type=Path, help="override mql5_events.parquet path")
    p.add_argument("--fred-events", type=Path, help="override fred_events.parquet path")
    p.add_argument("--report-dir", type=Path, help="default: <data root>/reports")
    p.add_argument("--timestamp-tolerance-seconds", type=float, default=60.0)
    p.add_argument("--percent-display-decimals", type=int, default=1,
                   help="decimals calendars publish for PERCENT values; the official release-vintage value is "
                        "rounded half-up to this precision before comparison (default 1)")
    p.add_argument("--thousands-display-decimals", type=int, default=0)
    p.add_argument("--vintage-max-lag-days", type=int, default=3,
                   help="an ALFRED vintage is accepted only in [release_date, release_date + N days]; later "
                        "vintages are rejected as look-ahead (default 3)")
    p.add_argument("--mql5-broker-timezone", help="IANA tz of the MQL5 server clock (e.g. Europe/Helsinki); "
                   "used ONLY when MQL5 UTC timestamps are unresolved. Omit unless confirmed.")
    p.add_argument("--strict", action="store_true", help="exit 1 unless every row is MATCH")
    return p


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
    families = tuple(f.strip() for f in args.families.split(",")) if args.families else SCOPE_FAMILIES

    config = load_config()
    mapping = load_event_mapping()
    unknown = [f for f in families if mapping.by_family(f) is None]
    if unknown:
        print(f"ERROR: unknown event_family: {unknown}", file=sys.stderr)
        return 2
    macro_dir = config.interim_root / "macro"
    paths = {"forex_factory": args.ff_events or macro_dir / "forex_factory_events.parquet",
             "mql5": args.mql5_events or macro_dir / "mql5_events.parquet",
             "fred": args.fred_events or macro_dir / "fred_events.parquet"}
    events = {}
    for name, path in paths.items():
        events[name] = read_events(path)
        print(f"[macro validation] {name}: {len(events[name])} normalized rows from {path}"
              + ("" if Path(path).exists() else "  (FILE NOT FOUND)"))

    cfg = ValidationConfig(timestamp_tolerance_seconds=args.timestamp_tolerance_seconds,
                           display_decimals={"PERCENT": args.percent_display_decimals,
                                             "THOUSANDS": args.thousands_display_decimals},
                           max_vintage_lag_days=args.vintage_max_lag_days,
                           mql5_broker_timezone=args.mql5_broker_timezone, families=families)
    result = reconcile(events["forex_factory"], events["mql5"], events["fred"], start, end, mapping, cfg,
                       fred_profiles_from_config(config))
    for w in result.warnings:
        print(f"[macro validation] WARNING: {w}")

    print(f"\nMacro validation {start} -> {end}  ({len(result.rows)} canonical events)\n")
    print(format_table(result.rows))
    counts = summarize(result.rows)
    print("\nOverall status:        " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("Release actual vs official release vintage: "
          + ", ".join(f"{k}={v}" for k, v in sorted(summarize(result.rows, "release_actual_match_status").items(), key=lambda kv: str(kv[0]))))
    problems = [r for r in result.rows if r["issues"]]
    if problems:
        print("\nIssues:")
        for r in problems:
            print(f"  {r['canonical_event_id']} [{r['source_match_status']}]")
            for issue in r["issues"].split(" | "):
                print(f"    - {issue}")

    hints = missing_vintage_commands(result.rows)
    if hints:
        print("\nTo obtain the missing official release vintages (network + FRED_API_KEY; not run by this script):")
        for h in hints:
            print("  " + h)

    report_dir = args.report_dir or (config.interim_root.parent / "reports")
    meta = {"start": start.isoformat(), "end": end.isoformat(), "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "inputs": {k: str(v) for k, v in paths.items()}, "families": list(families),
            "config": {"timestamp_tolerance_seconds": cfg.timestamp_tolerance_seconds, "display_decimals": cfg.display_decimals,
                       "max_vintage_lag_days": cfg.max_vintage_lag_days,
                       "mql5_broker_timezone": cfg.mql5_broker_timezone},
            "warnings": result.warnings,
            "roles": {"forex_factory": "pre-release provider_forecast (not economist consensus)",
                      "mql5": "primary actual/previous/revised_previous/release time",
                      "fred": "official-value validation only; joined by reference_period"}}
    written = write_reports(result.rows, meta, Path(report_dir), f"macro_validation_{start}_{end}")
    print("\nReports:\n  " + "\n  ".join(str(p) for p in written))
    if args.strict and any(r["source_match_status"] != MATCH for r in result.rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
