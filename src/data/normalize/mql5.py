"""Normalize parsed MQL5 calendar rows into canonical MacroEvent objects."""
from __future__ import annotations

import datetime as dt
import logging
from typing import List, Optional

from ..event_mapping import EventMapping
from ..fetch.mql5 import Mql5Row
from ..reference_period import infer_prior_month_reference_period
from ..schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit
from ..timeutil import normalize_timestamp

logger = logging.getLogger(__name__)

# Only the PERCENT MQL5 unit (ev.unit, NOT ev.multiplier -- see the .mq5
# exporter's own fix note about these being distinct enum types) maps to
# a ValueUnit we're confident enough in to compare numerically across
# sources. There is no MOM/YOY/QOQ member of ENUM_CALENDAR_EVENT_UNIT --
# an earlier revision of this set included those three, but they were
# never real values this normalizer could receive (confirmed by
# compiling the exporter's UnitToString() against the real, documented
# enum, which caught the same fictional names as compiler errors:
# CALENDAR_UNIT_MOM/YOY/QOQ do not exist). CURRENCY/USD/HOUR/JOB/RIG/
# PEOPLE/MORTGAGE/VOTE/BARREL/CUBICFEET/POSITION/BUILDING/NONE are
# genuinely ambiguous without confirmed documentation of MQL5's exact
# scaling convention for each (and, separately, whether `multiplier` --
# THOUSANDS/MILLIONS/BILLIONS/TRILLIONS -- is already baked into the raw
# value or would need to be applied on top of it is not something we can
# verify without live MT5 access). Guessing either risks silently
# corrupting a value or comparing unrelated quantities (job counts vs.
# dollar amounts vs. hours) as if interchangeable, which is worse than
# declining to compare at all -- so anything outside this set is
# ValueUnit.UNKNOWN and excluded from numerical cross-source comparison
# (see validation/macro.py).
_PERCENT_UNITS = {"PERCENT"}


def _unit_for(mql5_unit: str) -> ValueUnit:
    return ValueUnit.PERCENT if mql5_unit in _PERCENT_UNITS else ValueUnit.UNKNOWN


def _parse_period(period_raw: Optional[str]) -> Optional[dt.date]:
    if not period_raw:
        return None
    try:
        return dt.datetime.strptime(period_raw, "%Y.%m.%d").date()
    except ValueError:
        logger.warning("[MQL5 normalize] unparseable period %r", period_raw)
        return None


def normalize_mql5_rows(
    rows: List[Mql5Row],
    event_mapping: EventMapping,
    acquisition_timestamp_utc: dt.datetime,
    broker_timezone: Optional[str] = None,
    raw_artifact_checksum: Optional[str] = None,
) -> List[MacroEvent]:
    """`broker_timezone` is an operator-confirmed IANA zone name for the
    MetaTrader server/broker (e.g. "Europe/Helsinki") -- see
    config/data_sources.yaml `providers.mql5.broker_timezone`. When unset
    (the honest default: we have no way to know this from Python), rows
    are quarantined (`release_timestamp_utc=None`,
    `timestamp_quality=UNRESOLVED`) rather than silently treated as UTC.
    """
    normalized_events: List[MacroEvent] = []

    for row in rows:
        mapping = event_mapping.resolve_mql5(row.event_name)
        if mapping is None:
            # Unmapped events are diagnostically useful but shouldn't silently
            # vanish -- log once per unique name at debug level and skip.
            logger.debug("[MQL5 normalize] unmapped event name: %r", row.event_name)
            continue

        try:
            naive_dt = dt.datetime.strptime(row.event_time_raw, "%Y.%m.%d %H:%M:%S")
        except ValueError:
            logger.warning("[MQL5 normalize] bad timestamp %r, skipping", row.event_time_raw)
            continue

        if broker_timezone:
            result = normalize_timestamp(naive_dt, broker_timezone)
            release_ts = result.utc
            quality = TimestampQuality.CONFIRMED if result.trusted else TimestampQuality.UNRESOLVED
        else:
            # No confirmed broker timezone configured -- quarantine rather
            # than guess. See docstring above and timeutil.py.
            release_ts = None
            quality = TimestampQuality.UNRESOLVED

        unit = _unit_for(row.unit)

        # Prefer MqlCalendarValue.period (the real reference period the
        # exporter now preserves) over the inferred prior-month heuristic;
        # fall back only for CSVs exported before the `period` column existed.
        reference_period = _parse_period(row.period_raw) or infer_prior_month_reference_period(naive_dt.date())

        normalized_events.append(
            MacroEvent(
                event_id=f"mql5:{row.value_id}",
                event_family=mapping.event_family,
                indicator=mapping.indicator,
                release_bundle=mapping.release_bundle,
                reference_period=reference_period,
                release_timestamp_utc=release_ts,
                release_timestamp_ny=None,
                timestamp_quality=quality,
                actual=row.actual_value,
                actual_unit=unit,
                provider_forecast=row.forecast_value,
                provider_forecast_unit=unit,
                forecast_source="MQL5",  # diagnostics only, per architecture doc
                previous=row.prev_value,
                previous_unit=unit,
                revised_previous=row.revised_prev_value,
                unit=row.unit or None,
                importance=row.importance or None,
                source=MacroSource.MQL5,
                source_event_id=row.event_id,
                source_timestamp=naive_dt,
                source_timezone=broker_timezone or "UNKNOWN",
                raw_artifact_checksum=raw_artifact_checksum,
                retrieval_timestamp_utc=acquisition_timestamp_utc,
            )
        )

    return normalized_events
