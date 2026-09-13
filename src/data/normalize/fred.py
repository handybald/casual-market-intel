"""Normalize FRED raw snapshots into canonical official-validation MacroEvent rows.

Applies the transform declared per series in
config/data_sources.yaml `providers.fred.official_series` (index-level
FRED series are NOT the same thing as the MoM/YoY percentages or the
payrolls *change* that calendars report -- see that config's comments
and the module-level warning in fetch/fred.py). Never treats a FRED
observation `date` as an intraday release timestamp: these rows are
grouped for validation by `reference_period`, not by a release time,
which is intentionally left unresolved (None).

TWO DISTINCT KINDS OF "OFFICIAL" ROW, never conflated (see
MacroEvent.official_vintage_kind):

  LATEST_REVISED (normalize_fred_official_events, below): built from
  the default (no realtime_start/realtime_end) observations query,
  i.e. TODAY'S best-known, possibly-revised value for each period. Its
  `official_vintage_date` is when that CURRENT revision became
  official -- NOT when the period was first published. A prior version
  of this module's docstring incorrectly called this a "first-published
  date"; it is not, and comparing it against a historical calendar
  `actual` conflates "what's true now" with "what was known then".

  AS_OF (normalize_fred_asof_events, below): built from a real ALFRED
  vintage snapshot -- values for a period range exactly as they stood
  on one specific `realtime_start=realtime_end=<as_of_date>` query.
  This is the historically-honest comparison against a calendar's
  `actual`, but is NOT wired into the default full-historical bootstrap
  (one extra API request per as-of date needed -- doing this for a
  decade of monthly releases would be hundreds of extra requests for
  each series). It is implemented and tested, intended for scoped/
  smoke-test use against a small number of specific release dates --
  see the README's vintage-alignment smoke test section.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import List, NamedTuple, Optional

from ..config import AppConfig
from ..event_mapping import EventMapping
from ..manifest import Manifest, checksum_file
from ..schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit
from ..fetch.fred import latest_verified_snapshot_path

logger = logging.getLogger(__name__)


class Observation(NamedTuple):
    date: dt.date
    value: Optional[float]
    realtime_start: Optional[str]


def _parse_observations(raw_payload: dict) -> List[Observation]:
    out = []
    for o in raw_payload.get("observations", []):
        try:
            d = dt.date.fromisoformat(o["date"])
        except (KeyError, ValueError):
            continue
        raw_v = o.get("value")
        v = None if raw_v in (None, ".", "") else float(raw_v)
        out.append(Observation(d, v, o.get("realtime_start")))
    out.sort(key=lambda ob: ob.date)
    return out


def apply_transform(observations: List[Observation], transform: str) -> List[Observation]:
    """Returns one Observation per computed output, `.date` set to the
    reference period the computed value DESCRIBES (the later of the two
    input periods), `.realtime_start` carried through as whatever vintage
    tag the input observations carried (meaning depends on how they were
    fetched -- see the two normalize_fred_*_events functions below)."""
    if transform == "identity":
        return [o for o in observations if o.value is not None]

    if transform == "diff_1":
        out = []
        for i in range(1, len(observations)):
            prev, cur = observations[i - 1], observations[i]
            if prev.value is None or cur.value is None:
                continue
            out.append(Observation(cur.date, cur.value - prev.value, cur.realtime_start))
        return out

    if transform == "pct_change_1":
        out = []
        for i in range(1, len(observations)):
            prev, cur = observations[i - 1], observations[i]
            if prev.value in (None, 0) or cur.value is None:
                continue
            out.append(Observation(cur.date, (cur.value - prev.value) / prev.value * 100.0, cur.realtime_start))
        return out

    if transform == "pct_change_12":
        out = []
        for i in range(12, len(observations)):
            prev, cur = observations[i - 12], observations[i]
            if prev.value in (None, 0) or cur.value is None:
                continue
            out.append(Observation(cur.date, (cur.value - prev.value) / prev.value * 100.0, cur.realtime_start))
        return out

    raise ValueError(f"unknown FRED transform: {transform!r}")


def normalize_fred_official_events(
    config: AppConfig,
    manifest: Manifest,
    event_mapping: EventMapping,
    start_date: dt.date,
) -> List[MacroEvent]:
    provider_cfg = config.provider("fred")
    events: List[MacroEvent] = []

    for entry_cfg in provider_cfg.get("official_series", []):
        series_id = entry_cfg["series_id"]
        event_family = entry_cfg["event_family"]
        transform = entry_cfg["transform"]
        result_unit = ValueUnit(entry_cfg.get("result_unit", ValueUnit.UNKNOWN.value))

        snapshot_path = latest_verified_snapshot_path(config, series_id)
        if snapshot_path is None or not snapshot_path.exists():
            logger.warning("[FRED normalize] no verified snapshot for %s -- fetch it first", series_id)
            continue

        raw_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        observations = _parse_observations(raw_payload)
        transformed = apply_transform(observations, transform)

        mapping_entry = event_mapping.by_family(event_family)
        indicator = mapping_entry.indicator if mapping_entry else event_family
        release_bundle = mapping_entry.release_bundle if mapping_entry else None

        try:
            acquisition_ts = dt.datetime.fromisoformat(raw_payload["retrieved_at"])
        except (KeyError, ValueError):
            acquisition_ts = dt.datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=dt.timezone.utc)
        checksum = checksum_file(snapshot_path)

        for obs in transformed:
            if obs.date < start_date:
                continue  # part of the lookback buffer, not the requested range
            # This is the LATEST-REVISED vintage's own as-of date (when
            # the current revision became official), not a first-publication
            # date -- see the module docstring.
            vintage_date = dt.date.fromisoformat(obs.realtime_start) if obs.realtime_start else None

            events.append(
                MacroEvent(
                    event_id=f"fred:{series_id}:{event_family}:{obs.date.isoformat()}:latest_revised",
                    event_family=event_family,
                    indicator=indicator,
                    release_bundle=release_bundle,
                    reference_period=obs.date,
                    release_timestamp_utc=None,
                    timestamp_quality=TimestampQuality.UNRESOLVED,
                    official_actual=obs.value,
                    official_actual_unit=result_unit,
                    official_source=f"FRED:{series_id}",
                    official_vintage_date=vintage_date,
                    official_vintage_kind="LATEST_REVISED",
                    source=MacroSource.FRED,
                    source_event_id=series_id,
                    raw_artifact_checksum=checksum,
                    retrieval_timestamp_utc=acquisition_ts,
                )
            )

    return events


def normalize_fred_asof_events(
    event_mapping: EventMapping,
    series_id: str,
    event_family: str,
    transform: str,
    result_unit: ValueUnit,
    as_of_date: dt.date,
    snapshot_path,
) -> List[MacroEvent]:
    """Build AS_OF-vintage MacroEvent rows from one
    `fetch.fred.fetch_observations_as_of` snapshot -- values exactly as
    they stood on `as_of_date`, the historically-honest counterpart to
    `normalize_fred_official_events`'s LATEST_REVISED rows. Event ids
    carry the as-of date so these never collide with (or silently
    overwrite, via normalize/io.py's event_id-keyed dedup) a
    LATEST_REVISED row for the same period.
    """
    raw_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    observations = _parse_observations(raw_payload)
    transformed = apply_transform(observations, transform)

    mapping_entry = event_mapping.by_family(event_family)
    indicator = mapping_entry.indicator if mapping_entry else event_family
    release_bundle = mapping_entry.release_bundle if mapping_entry else None

    try:
        acquisition_ts = dt.datetime.fromisoformat(raw_payload["retrieved_at"])
    except (KeyError, ValueError):
        acquisition_ts = dt.datetime.now(dt.timezone.utc)
    checksum = checksum_file(snapshot_path)

    events: List[MacroEvent] = []
    for obs in transformed:
        events.append(
            MacroEvent(
                event_id=f"fred:{series_id}:{event_family}:{obs.date.isoformat()}:asof:{as_of_date.isoformat()}",
                event_family=event_family,
                indicator=indicator,
                release_bundle=release_bundle,
                reference_period=obs.date,
                release_timestamp_utc=None,
                timestamp_quality=TimestampQuality.UNRESOLVED,
                official_actual=obs.value,
                official_actual_unit=result_unit,
                official_source=f"FRED:{series_id}",
                official_vintage_date=as_of_date,
                official_vintage_kind="AS_OF",
                source=MacroSource.FRED,
                source_event_id=series_id,
                raw_artifact_checksum=checksum,
                retrieval_timestamp_utc=acquisition_ts,
            )
        )
    return events


def fetch_and_normalize_fred_asof(
    config: AppConfig,
    manifest: Manifest,
    event_mapping: EventMapping,
    series_id: str,
    event_family: str,
    transform: str,
    result_unit: ValueUnit,
    observation_start: dt.date,
    observation_end: dt.date,
    as_of_date: dt.date,
    session=None,
) -> List[MacroEvent]:
    """Convenience: fetch one ALFRED as-of snapshot and normalize it in
    one call. See fetch.fred.fetch_observations_as_of for why this is
    one-request-per-as-of-date and not part of the default bootstrap."""
    from ..fetch.fred import fetch_observations_as_of

    snapshot_path = fetch_observations_as_of(
        config, manifest, series_id, observation_start, observation_end, as_of_date, session=session
    )
    return normalize_fred_asof_events(
        event_mapping, series_id, event_family, transform, result_unit, as_of_date, snapshot_path
    )
