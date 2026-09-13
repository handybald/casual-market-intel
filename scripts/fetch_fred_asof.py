#!/usr/bin/env python3
"""Fetch and normalize one FRED ALFRED "as of" historical vintage.

    python scripts/fetch_fred_asof.py --event-family CPI_MOM \
        --observation-start 2024-01-01 --observation-end 2024-01-31 \
        --as-of 2024-02-05

Deliberately NOT part of the default `fetch_historical_data.py`/
`update_data.py` bootstrap/update pipeline: an ALFRED vintage query is
one HTTP request PER as-of date (see
`src/data/fetch/fred.fetch_observations_as_of`'s docstring), so looping
it over every period in a full historical backfill would be one request
per period rather than one per series. This command is the deliberate,
targeted alternative -- run it for the specific historical release
dates you want to validate a calendar's real-time `actual` against
(see `src/data/validation/macro.py`'s `actual_vs_official_asof:{date}`
comparison), not for wholesale backfill.

`series_id`/`transform`/`result_unit` are looked up from
config/data_sources.yaml's `providers.fred.official_series` by
`--event-family` -- the AS_OF workflow reuses the exact same series
configuration as the default LATEST_REVISED fetch, so there is no
separate config surface to keep in sync.

Writes the AS_OF snapshot under `data/raw/fred/<series_id>/asof/`
(distinct from the default latest-revised snapshot directory) and
merges the normalized AS_OF-vintage event(s) into
`data/interim/macro/fred_events.parquet` alongside (never overwriting)
any LATEST_REVISED rows for the same period -- see normalize/io.py's
event_id-keyed dedup and normalize/fred.py's `normalize_fred_asof_events`.

EXIT CODE: 0 only if the as-of snapshot was fetched AND at least one
event was normalized from it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data.config import load_config, load_dotenv_if_present
from src.data.event_mapping import load_event_mapping
from src.data.manifest import Manifest
from src.data.schemas import ValueUnit
from src.data.fetch.fred import fetch_observations_as_of, MissingCredentialsError, FredResponseError
from src.data.normalize.fred import normalize_fred_asof_events
from src.data.normalize.io import merge_write_events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("fetch_fred_asof")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event-family", required=True, help="must match a providers.fred.official_series[].event_family entry")
    parser.add_argument("--observation-start", required=True, type=str, help="YYYY-MM-DD")
    parser.add_argument("--observation-end", required=True, type=str, help="YYYY-MM-DD")
    parser.add_argument("--as-of", required=True, type=str, help="YYYY-MM-DD -- the historical ALFRED vintage date to query")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    load_dotenv_if_present()
    config = load_config()
    event_mapping = load_event_mapping()
    manifest = Manifest(config.manifest_path)

    args = parse_args(argv)
    entry_cfg = next(
        (e for e in config.provider("fred").get("official_series", []) if e["event_family"] == args.event_family),
        None,
    )
    if entry_cfg is None:
        logger.error(
            "no providers.fred.official_series entry configured for event_family %r -- add one to "
            "config/data_sources.yaml first (the AS_OF workflow reuses that same configuration)",
            args.event_family,
        )
        return 1

    series_id = entry_cfg["series_id"]
    transform = entry_cfg["transform"]
    result_unit = ValueUnit(entry_cfg.get("result_unit", ValueUnit.UNKNOWN.value))
    observation_start = dt.date.fromisoformat(args.observation_start)
    observation_end = dt.date.fromisoformat(args.observation_end)
    as_of_date = dt.date.fromisoformat(args.as_of)

    try:
        snapshot_path = fetch_observations_as_of(
            config, manifest, series_id, observation_start, observation_end, as_of_date,
        )
    except (MissingCredentialsError, FredResponseError) as exc:
        logger.error("[FRED AS_OF] %s", exc)
        return 1

    events = normalize_fred_asof_events(
        event_mapping, series_id, args.event_family, transform, result_unit, as_of_date, snapshot_path,
    )
    merge_write_events(events, config.interim_root / "macro" / "fred_events.parquet")

    logger.info(
        "[FRED AS_OF] %s @ %s: fetched %s, normalized %d event(s) into data/interim/macro/fred_events.parquet",
        args.event_family, as_of_date, snapshot_path, len(events),
    )
    if not events:
        logger.warning(
            "0 events normalized -- check --observation-start/--observation-end actually cover a "
            "real release for this series, and that %s has a valid transform for it", series_id,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
