#!/usr/bin/env python3
"""Write (or verify) the versioned descriptor of the canonical market dataset.

    python scripts/build_dataset_descriptor.py            # write metadata/datasets/<name>.json
    python scripts/build_dataset_descriptor.py --check    # verify local data still matches it

Reads only local, already-stored canonical data (default market provider and
feed from config); never fetches. The descriptor is committed; the parquet
data it describes is not. See src/data/dataset_descriptor.py for exactly what
enters the fingerprint.

EXIT CODE: 0 on success / match; 1 if --check finds a mismatch; 2 on usage errors.
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

from src.data.config import load_config  # noqa: E402
from src.data.dataset_descriptor import build_descriptor, verify_against, write_descriptor  # noqa: E402
from src.data.fetch.market_provider import get_market_provider  # noqa: E402
from src.data.market_exceptions import load_registry_for_config  # noqa: E402

DEFAULT_NAME = "core_market_alpaca_sip_1min_raw_qqq_spy"
DEFAULT_DIR = REPO_ROOT / "metadata" / "datasets"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("--symbols", default="QQQ,SPY")
    p.add_argument("--start", default=None, help="requested benchmark start (default: config historical.start_date)")
    p.add_argument("--end", default="2026-10-02", help="evaluation end date (inclusive)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--check", action="store_true", help="verify instead of write")
    args = p.parse_args(argv)

    config = load_config()
    provider = get_market_provider(config.primary_market_provider, config)
    registry = load_registry_for_config(config)
    start = dt.date.fromisoformat(args.start) if args.start else config.start_date
    end = dt.date.fromisoformat(args.end)
    out = args.out or (DEFAULT_DIR / f"{args.name}.json")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    current = build_descriptor(config, provider, registry, args.name, symbols, start, end)
    if args.check:
        if not out.exists():
            print(f"ERROR: no committed descriptor at {out}", file=sys.stderr)
            return 2
        problems = verify_against(json.loads(out.read_text(encoding="utf-8")), current)
        if problems:
            print("DESCRIPTOR MISMATCH:")
            for prob in problems:
                print(f"  - {prob}")
            return 1
        print(f"OK: local data matches {out.name} ({current['fingerprint']})")
        return 0
    write_descriptor(current, out)
    print(f"wrote {out} -- fingerprint {current['fingerprint']}")
    for sym, s in current["symbols"].items():
        print(f"  {sym}: {s['first_timestamp_utc']} .. {s['last_timestamp_utc']} rows={s['total_rows']:,} "
              f"sessions {s['represented_sessions']}/{s['expected_sessions']} provisional={s['provisional_windows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
