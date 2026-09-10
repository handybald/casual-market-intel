"""Parse raw Forex Factory calendar HTML and normalize into MacroEvent.

Parser is kept separate from the downloader (fetch/forex_factory.py) so
raw HTML can be re-parsed without re-fetching if the parser needs a fix.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import List, NamedTuple, Optional

from bs4 import BeautifulSoup

from ..event_mapping import EventMapping
from ..schemas import MacroEvent, MacroSource
from ..timeutil import now_utc

logger = logging.getLogger(__name__)

_MONTH_NAME_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_IMPACT_MAP = {
    "red": "HIGH",
    "ora": "MEDIUM",
    "orange": "MEDIUM",
    "yel": "LOW",
    "yellow": "LOW",
    "gra": "NONE",
    "gray": "NONE",
    "grey": "NONE",
}

# Assumed display timezone for an anonymous Forex Factory session.
# NOT independently verified live in this environment -- treated as an
# unconfirmed assumption (see schemas.MacroEvent.timestamp_is_trustworthy).
ASSUMED_DISPLAY_TIMEZONE = "SERVER"


class ForexFactoryRawRow(NamedTuple):
    date: dt.date
    time_raw: str
    currency: str
    impact: str  # HIGH/MEDIUM/LOW/NONE
    event_name: str
    actual_raw: str
    forecast_raw: str
    previous_raw: str


def _clean(text: Optional[str]) -> str:
    return (text or "").strip()


def _impact_from_classes(classes: List[str]) -> str:
    for cls in classes:
        for key, label in _IMPACT_MAP.items():
            if key in cls:
                return label
    return "NONE"


def _resolve_day_year(month_abbr: str, requested_year: int, requested_month: int) -> int:
    month_num = _MONTH_NAME_TO_NUM.get(month_abbr.lower())
    if month_num is None:
        return requested_year
    if month_num == requested_month:
        return requested_year
    if requested_month == 1 and month_num == 12:
        return requested_year - 1
    if requested_month == 12 and month_num == 1:
        return requested_year + 1
    return requested_year


def parse_forex_factory_html(
    html: str, requested_year: int, requested_month: int
) -> List[ForexFactoryRawRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: List[ForexFactoryRawRow] = []

    current_date: Optional[dt.date] = None
    last_time_raw = ""

    table_rows = soup.select("tr.calendar__row")
    for tr in table_rows:
        classes = tr.get("class", [])

        date_cell = tr.select_one(".calendar__date")
        if date_cell is not None:
            date_text = _clean(date_cell.get_text(" "))
            match = re.search(r"([A-Za-z]{3})\s+(\d{1,2})", date_text)
            if match:
                month_abbr, day = match.group(1), int(match.group(2))
                year = _resolve_day_year(month_abbr, requested_year, requested_month)
                month_num = _MONTH_NAME_TO_NUM.get(month_abbr.lower())
                if month_num:
                    try:
                        current_date = dt.date(year, month_num, day)
                    except ValueError:
                        logger.warning("[ForexFactory parse] bad date %s %s %s", year, month_num, day)

        event_cell = tr.select_one(".calendar__event")
        if event_cell is None:
            continue  # header/spacer row with only a date cell
        event_name = _clean(event_cell.get_text(" "))
        if not event_name:
            continue

        time_cell = tr.select_one(".calendar__time")
        time_raw = _clean(time_cell.get_text(" ")) if time_cell else ""
        if time_raw:
            last_time_raw = time_raw
        else:
            time_raw = last_time_raw

        currency_cell = tr.select_one(".calendar__currency")
        currency = _clean(currency_cell.get_text(" ")) if currency_cell else ""

        impact_cell = tr.select_one(".calendar__impact span")
        impact_classes = impact_cell.get("class", []) if impact_cell else []
        impact = _impact_from_classes(impact_classes)

        actual_cell = tr.select_one(".calendar__actual")
        forecast_cell = tr.select_one(".calendar__forecast")
        previous_cell = tr.select_one(".calendar__previous")

        if current_date is None:
            logger.debug("[ForexFactory parse] event row before any date header, skipping: %r", event_name)
            continue

        rows.append(
            ForexFactoryRawRow(
                date=current_date,
                time_raw=time_raw,
                currency=currency,
                impact=impact,
                event_name=event_name,
                actual_raw=_clean(actual_cell.get_text(" ")) if actual_cell else "",
                forecast_raw=_clean(forecast_cell.get_text(" ")) if forecast_cell else "",
                previous_raw=_clean(previous_cell.get_text(" ")) if previous_cell else "",
            )
        )

    return rows


_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_numeric(raw: str) -> Optional[float]:
    """Forex Factory renders values like '3.1%', '150K', '1.2M', '-0.2%'.
    Extract the numeric magnitude; unit suffix (%, K, M, B) is not applied
    as a multiplier here -- we store the literal printed number and keep
    `unit` separate, since silently guessing a K/M/B multiplier for a
    field we don't have a strong unit contract for risks corrupting values.
    """
    raw = raw.strip()
    if not raw or raw in {"-", "—"}:
        return None
    match = _NUMERIC_RE.search(raw.replace(",", ""))
    if not match:
        return None
    return float(match.group(0))


def _parse_time_of_day(date: dt.date, time_raw: str) -> Optional[dt.datetime]:
    time_raw = time_raw.strip().lower()
    if not time_raw or time_raw in {"all day", "tentative", "day 1", "day 2"}:
        return None
    match = re.match(r"(\d{1,2}):(\d{2})(am|pm)?", time_raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    try:
        return dt.datetime(date.year, date.month, date.day, hour, minute)
    except ValueError:
        return None


def normalize_forex_factory_rows(
    rows: List[ForexFactoryRawRow],
    event_mapping: EventMapping,
    currency_filter: Optional[str] = None,
    source_url: Optional[str] = None,
) -> List[MacroEvent]:
    retrieved_at = now_utc()
    events: List[MacroEvent] = []

    for row in rows:
        if currency_filter and row.currency and row.currency.upper() != currency_filter.upper():
            continue

        mapping = event_mapping.resolve_forex_factory(row.event_name)
        if mapping is None:
            logger.debug("[ForexFactory normalize] unmapped event name: %r", row.event_name)
            continue

        naive_dt = _parse_time_of_day(row.date, row.time_raw)
        if naive_dt is None:
            # "All Day" / "Tentative" releases: anchor to midnight of the
            # release date, flagged as untrustworthy via ASSUMED_DISPLAY_TIMEZONE.
            naive_dt = dt.datetime(row.date.year, row.date.month, row.date.day)

        events.append(
            MacroEvent(
                event_id=f"ff:{row.date.isoformat()}:{row.time_raw}:{row.event_name}",
                event_family=mapping.event_family,
                indicator=mapping.indicator,
                release_bundle=mapping.release_bundle,
                release_timestamp_utc=naive_dt.replace(tzinfo=dt.timezone.utc),
                release_timestamp_ny=None,
                actual=_parse_numeric(row.actual_raw),
                provider_forecast=_parse_numeric(row.forecast_raw),
                forecast_source="FOREX_FACTORY",
                previous=_parse_numeric(row.previous_raw),
                importance=row.impact,
                source=MacroSource.FOREX_FACTORY,
                source_event_id=None,
                source_timestamp=naive_dt,
                source_timezone=ASSUMED_DISPLAY_TIMEZONE,
                source_url=source_url,
                retrieval_timestamp_utc=retrieved_at,
            )
        )

    return events


def normalize_forex_factory_file(
    path: Path,
    year: int,
    month: int,
    event_mapping: EventMapping,
    currency_filter: Optional[str] = None,
) -> List[MacroEvent]:
    html = path.read_text(encoding="utf-8")
    raw_rows = parse_forex_factory_html(html, year, month)
    return normalize_forex_factory_rows(
        raw_rows, event_mapping, currency_filter=currency_filter, source_url=str(path)
    )
