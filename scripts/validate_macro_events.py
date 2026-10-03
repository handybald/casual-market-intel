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
from src.data.validation.macro_release_pipeline import run_macro_validation  # noqa: E402
from src.data.validation.macro_events import (  # noqa: E402
    MATCH, OFFICIAL_VINTAGE_UNAVAILABLE, ROW_FIELDS, SCOPE_FAMILIES, ValidationConfig, summarize,
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
    ("ff_fcst", "ff_provider_forecast", 7), ("actual", "actual_value", 7), ("off_vint", "official_release_vintage_value", 8),
    ("actual_st", "release_actual_match_status", 14), ("prev", "mql5_previous", 6),
    ("off_prev", "previous_official_release_vintage", 8), ("prev_st", "previous_validation_status", 14),
    ("rev_prev", "mql5_revised_previous", 8), ("off_rev", "revised_previous_official_release_vintage", 8),
    ("rev_st", "revised_previous_validation_status", 14), ("ts", "timestamp_status", 12), ("tdelta", "timestamp_delta_seconds", 6),
    ("unit", "unit_status", 10), ("status", "source_match_status", 28),
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


def print_timezone_block(tz: Dict[str, Any]) -> None:
    cand = tz.get("candidate_broker_timezone")
    print(f"\nBroker timezone validation  candidate={cand or '(none supplied)'}  status={tz.get('status')}")
    if not tz.get("per_release"):
        print("  no evidence: needs a supplied --mql5-broker-timezone, MQL5 rows, and CONFIRMED Forex Factory times")
        return
    print(f"  distinct trusted releases: {tz['distinct_trusted_releases']} (need >= {tz['min_evidence_releases']}), "
          f"matching={tz['releases_matching']}, mismatching={tz['releases_mismatching']}, "
          f"max |delta|={tz['max_abs_delta_seconds']:.0f}s, observed UTC offsets={tz['observed_utc_offsets_hours']}, "
          f"evidence sufficient={tz['evidence_sufficient']}, MQL5 times trusted: {tz['mql5_timestamps_trusted']}")
    if not tz["dst_regimes_covered"]:
        print("  NOTE: candidate zone observes DST but the evidence has a single UTC offset -> cannot be told apart from a fixed offset")
    for r in tz["per_release"][:12]:
        print(f"  {r['status']:8s} mql5_raw={r['mql5_raw_timestamp']}  -> {cand} -> {r['mql5_converted_utc']}  "
              f"ff_utc={r['ff_confirmed_utc']}  delta={r['delta_seconds']:+.0f}s  ({len(r['events'])} event(s))")
    if len(tz["per_release"]) > 12:
        print(f"  ... {len(tz['per_release']) - 12} more release(s) in the report JSON")


def _month_shift(d: dt.date, months: int) -> dt.date:
    m = d.year * 12 + d.month - 1 + months
    return dt.date(m // 12, m % 12 + 1, 1)


def _asof_command(family: str, ref: dt.date, as_of: str) -> str:
    """Observation window: 13 months before `ref` (12-month transforms need it) through the end of `ref`."""
    end = _month_shift(ref, 1) - dt.timedelta(days=1)
    return (f"python3 scripts/fetch_fred_asof.py --event-family {family} "
            f"--observation-start {_month_shift(ref, -13)} --observation-end {end} --as-of {as_of}")


def missing_vintage_commands(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """One scripts/fetch_fred_asof.py command per distinct (family, reference period, as-of date) lacking a
    release vintage: the release's own vintage, the previous release's vintage, and the vintage of the prior
    period at the current release. This script never fetches; these are for the operator to run."""
    out, seen = [], set()

    def add(family, ref_iso, as_of):
        if not (ref_iso and as_of) or (family, ref_iso, as_of) in seen:
            return
        seen.add((family, ref_iso, as_of))
        out.append(_asof_command(family, dt.date.fromisoformat(ref_iso), as_of))

    for r in rows:
        f = r["event_family"]
        if r["release_actual_match_status"] == OFFICIAL_VINTAGE_UNAVAILABLE:
            add(f, r["reference_period"], r["release_date"])
        if r["previous_validation_status"] == OFFICIAL_VINTAGE_UNAVAILABLE:
            add(f, r["previous_reference_period"], r["previous_prior_release_date"])
        if r["revised_previous_validation_status"] == OFFICIAL_VINTAGE_UNAVAILABLE:
            add(f, r["previous_reference_period"], r["release_date"])
    return out


def write_reports(rows: List[Dict[str, Any]], meta: Dict[str, Any], out_dir: Path, stem: str,
                  tz: Optional[Dict[str, Any]] = None, tz_evidence: Optional[Dict[str, Any]] = None) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.json"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in ROW_FIELDS})
    json_path.write_text(json.dumps({"meta": meta, "summary": summarize(rows), "timezone_validation": tz or {}, "timezone_evidence": tz_evidence, "rows": rows}, indent=2,
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
    p.add_argument("--timezone-evidence-start", help="YYYY-MM-DD: separate, wider window used to decide whether MQL5 "
                   "times are trustworthy under --mql5-broker-timezone (default: the validation window itself)")
    p.add_argument("--timezone-evidence-end", help="YYYY-MM-DD")
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
    cfg = ValidationConfig(timestamp_tolerance_seconds=args.timestamp_tolerance_seconds,
                           display_decimals={"PERCENT": args.percent_display_decimals,
                                             "THOUSANDS": args.thousands_display_decimals},
                           max_vintage_lag_days=args.vintage_max_lag_days,
                           mql5_broker_timezone=args.mql5_broker_timezone, families=families)
    ev_window = None
    if args.timezone_evidence_start or args.timezone_evidence_end:
        try:
            ev_window = (dt.date.fromisoformat(args.timezone_evidence_start), dt.date.fromisoformat(args.timezone_evidence_end))
        except (TypeError, ValueError):
            print("ERROR: --timezone-evidence-start and --timezone-evidence-end must both be YYYY-MM-DD", file=sys.stderr)
            return 2
    result, paths, counts = run_macro_validation(
        start, end, config=config, mapping=mapping, cfg=cfg,
        paths={"forex_factory": args.ff_events, "mql5": args.mql5_events, "fred": args.fred_events},
        timezone_evidence_window=ev_window)
    for name, path in paths.items():
        print(f"[macro validation] {name}: {counts[name]} normalized rows from {path}"
              + ("" if Path(path).exists() else "  (FILE NOT FOUND)"))
    for w in result.warnings:
        print(f"[macro validation] WARNING: {w}")

    print(f"\nMacro validation {start} -> {end}  ({len(result.rows)} canonical events)\n")
    print(format_table(result.rows))
    counts = summarize(result.rows)
    print("\nOverall status:        " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for label, key in (("previous vs official prior vintage", "previous_validation_status"),
                       ("revised_previous vs official revised prior vintage", "revised_previous_validation_status")):
        print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in sorted(summarize(result.rows, key).items(), key=lambda kv: str(kv[0]))))
    print("Release actual vs official release vintage: "
          + ", ".join(f"{k}={v}" for k, v in sorted(summarize(result.rows, "release_actual_match_status").items(), key=lambda kv: str(kv[0]))))
    print_timezone_block(result.timezone_validation)
    if result.timezone_evidence is not None:
        print(f"\nTimezone EVIDENCE window {ev_window[0]} -> {ev_window[1]} (decides MQL5 trust):")
        print_timezone_block(result.timezone_evidence)
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
    written = write_reports(result.rows, meta, Path(report_dir), f"macro_validation_{start}_{end}", result.timezone_validation, result.timezone_evidence)
    print("\nReports:\n  " + "\n  ".join(str(p) for p in written))
    if args.strict and any(r["source_match_status"] != MATCH for r in result.rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
