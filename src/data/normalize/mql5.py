"""Normalize parsed MQL5 calendar rows into canonical MacroEvent objects."""
from __future__ import annotations

import datetime as dt
import logging
from typing import List

from ..event_mapping import EventMapping
from ..fetch.mql5 import Mql5Row
from ..schemas import MacroEvent, MacroSource
from ..timeutil import now_utc

logger = logging.getLogger(__name__)


def normalize_mql5_rows(
    rows: List[Mql5Row],
    event_mapping: EventMapping,
) -> List[MacroEvent]:
    retrieved_at = now_utc()
    events: List[MacroEvent] = []

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

        # The exporter marks its own timezone as "SERVER" (broker/terminal
        # time) which is NOT confirmed to be UTC -- see
        # mql5_exporter/EconomicCalendarExporter.mq5 and timeutil.py.
        # release_timestamp_utc below is a best-effort placeholder (the raw
        # value treated as if it were UTC) so rows remain sortable/joinable;
        # `MacroEvent.timestamp_is_trustworthy` is False for these rows and
        # downstream code MUST check it before relying on precise timing.
        # Reconciling MQL5 SERVER offsets against Forex Factory's published
        # (confirmed) event times is future work, not done silently here.
        source_timezone = row.source_timezone

        events.append(
            MacroEvent(
                event_id=f"mql5:{row.event_id}:{row.event_time_raw}",
                event_family=mapping.event_family,
                indicator=mapping.indicator,
                release_bundle=mapping.release_bundle,
                release_timestamp_utc=naive_dt.replace(tzinfo=dt.timezone.utc),
                release_timestamp_ny=None,
                actual=row.actual_value,
                provider_forecast=row.forecast_value,
                forecast_source="MQL5",  # diagnostics only, per architecture doc
                previous=row.prev_value,
                revised_previous=row.revised_prev_value,
                unit=row.unit or None,
                importance=row.importance or None,
                source=MacroSource.MQL5,
                source_event_id=row.event_id,
                source_timestamp=naive_dt,
                source_timezone=source_timezone,
                retrieval_timestamp_utc=retrieved_at,
            )
        )

    return events
