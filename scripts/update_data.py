#!/usr/bin/env python3
"""Incremental delta update.

    python scripts/update_data.py

Historical bootstrap (scripts/fetch_historical_data.py) happens once;
this is what runs on a schedule afterwards. It re-invokes the SAME
per-provider fetch logic as bootstrap, across the FULL configured
historical range through today -- that is not wasteful, because every
fetcher already skips any chunk the manifest can verify is durably
complete (see src/data/manifest.py `is_complete`). What actually gets
(re)fetched is exactly:

  - any interval that previously FAILED (repaired automatically, even
    if a later interval already succeeded -- see
    Manifest.completion_watermark / coverage_gaps),
  - any interval still PROVISIONAL (the current month/trailing
    revision-overlap window for Massive/Forex Factory, always re-synced
    rather than trusted as final),
  - whatever is newly in range since the last run,
  - and, for FRED, everything -- by design (see fetch/fred.py) FRED
    always does a full small refetch to catch revisions/late-arriving
    observations, which is why it does not use the manifest's
    watermark/gap machinery the way Massive/Forex Factory do.

EXIT CODE: 0 only if this run recorded no failed chunks/sources AND the
manifest shows no remaining coverage gaps within the requested range for
any provider/key this run touched.
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
from src.data.manifest import Manifest, utcnow_iso
from src.data.fetch.massive import cache_key as massive_cache_key
from src.data.fetch.forex_factory import MANIFEST_KEY as FF_MANIFEST_KEY

import fetch_historical_data as bootstrap  # reuses run_mql5 / run_forex_factory / run_massive / run_fred / run_bls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("update_data")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--sources", type=str, default=",".join(bootstrap.DEFAULT_SOURCES))
    parser.add_argument("--symbols", type=str, default=None)
    return parser.parse_args(argv)


def _remaining_gaps(manifest: Manifest, provider: str, key: str, dataset_start: dt.date, end: dt.date, today: dt.date):
    return manifest.classify_gaps(provider, key, dataset_start, end, today=today)


def main(argv=None) -> int:
    load_dotenv_if_present()
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)

    args = parse_args(argv)
    sources = bootstrap.validate_sources([s.strip() for s in args.sources.split(",") if s.strip()])
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    start = config.start_date
    if start > end:
        raise SystemExit(f"error: configured historical.start_date {start} is after --end {end}")
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else config.market_symbols

    run_started_at = utcnow_iso()
    logger.info("Incremental update: dataset range %s -> %s | sources=%s", start, end, sources)

    summary = {}

    if "mql5" in sources:
        summary["mql5"] = bootstrap.run_mql5(config, manifest, event_mapping, start, end)

    if "forex_factory" in sources:
        summary["forex_factory"] = bootstrap.run_forex_factory(config, manifest, event_mapping, start, end, force=False)

    if "massive" in sources:
        try:
            summary["massive"] = bootstrap.run_massive(config, manifest, symbols, start, end, force=False)
        except Exception as exc:  # noqa: BLE001 - MissingCredentialsError etc, already the pattern bootstrap uses
            from src.data.fetch.massive import MissingCredentialsError
            from src.data.validation.market import UnsupportedTimeframeError

            if isinstance(exc, MissingCredentialsError):
                logger.error("[Massive] skipped: %s", exc)
                summary["massive"] = {"error": str(exc), "ok": False}
            elif isinstance(exc, UnsupportedTimeframeError):
                logger.error("[Massive] rejected before fetching: %s", exc)
                summary["massive"] = {"error": str(exc), "ok": False}
            else:
                raise

    if "fred" in sources:
        try:
            summary["fred"] = bootstrap.run_fred(config, manifest, event_mapping, start, end)
        except Exception as exc:  # noqa: BLE001
            from src.data.fetch.fred import MissingCredentialsError as FredMissingCreds

            if isinstance(exc, FredMissingCreds):
                logger.error("[FRED] skipped: %s", exc)
                summary["fred"] = {"error": str(exc), "ok": False}
            else:
                raise

    if "bls" in sources:
        summary["bls"] = bootstrap.run_bls(config, manifest, start, end)

    # -- remaining-coverage report (diagnostic + drives exit code) --
    # `classify_gaps` distinguishes a "provisional" gap (successfully
    # fetched this run, e.g. today's data, intentionally not finalized
    # yet -- NOT a failure) from a "failed"/"missing" gap (a real
    # problem). Treating every unverified stretch as a failure was the
    # bug: a clean update through today always has a provisional tail
    # and would always exit 1 even with zero failed downloads.
    gap_report = {}
    if "forex_factory" in sources:
        gap_report["forex_factory"] = _remaining_gaps(manifest, "forex_factory", FF_MANIFEST_KEY, start, end, end)
    if "massive" in sources:
        for symbol in symbols:
            provider_cfg = config.provider("massive")
            key = massive_cache_key(symbol, config.market_timeframe, bool(provider_cfg.get("adjusted", False)))
            gap_report[f"massive:{symbol}"] = _remaining_gaps(manifest, "massive", key, start, end, end)

    this_run_failures = [e for e in manifest.failed_entries() if e.retrieved_at >= run_started_at]

    print("\n" + "=" * 60)
    print("INCREMENTAL UPDATE SUMMARY")
    print("=" * 60)
    for source, result in summary.items():
        print(f"[{source}] {result}")
    print(f"Failed chunks recorded THIS RUN: {len(this_run_failures)}")
    blocking_gaps = {}
    for key, gaps in gap_report.items():
        pending = [g for g in gaps if not g.is_failure]
        blocking = [g for g in gaps if g.is_failure]
        if pending:
            print(f"[{key}] pending finalization (not a failure): {[(g.start, g.end, g.reason) for g in pending]}")
        if blocking:
            print(f"[{key}] UNRESOLVED coverage gaps: {[(g.start, g.end, g.reason) for g in blocking]}")
            blocking_gaps[key] = blocking
    print("=" * 60)

    provider_level_failure = any(isinstance(r, dict) and r.get("ok") is False for r in summary.values())
    success = not provider_level_failure and not this_run_failures and not blocking_gaps
    if not success:
        logger.error("Update completed WITH FAILURES/GAPS -- see summary above.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
