"""Parse raw Forex Factory calendar HTML and normalize into MacroEvent.

Parser is kept separate from the downloader (fetch/forex_factory.py) in
the sense that all HTML structural parsing lives HERE; fetch.py imports
`parse_forex_factory_html` only to validate a download before recording
it as a checkpoint -- it does not duplicate parsing logic or reach into
currency filtering / event mapping / MacroEvent construction, which stay
in this module.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import List, NamedTuple, Optional

from bs4 import BeautifulSoup

from ..event_mapping import EventMapping
from ..reference_period import infer_prior_month_reference_period
from ..schemas import MacroEvent, MacroSource, TimestampQuality, ValueUnit
from ..timeutil import normalize_timestamp

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
_SUFFIX_MULTIPLIER = {"K": 1e3, "M": 1e6, "B": 1e9}


class ParsedValue(NamedTuple):
    value: Optional[float]
    unit: ValueUnit
    raw_text: str


def parse_value_with_unit(raw: str) -> ParsedValue:
    """Forex Factory renders values like '3.1%', '150K', '2.5M', '-0.2%'.

    K/M/B suffixes are converted into a single canonical unit
    (THOUSANDS) so equal real-world quantities compare equal regardless
    of which suffix the page happened to render -- "2.5M" and "2500K"
    both normalize to 2500.0 THOUSANDS, not 2.5 and 2500 as two
    unrelated numbers. Percentages keep their literal percent value
    (0.3 for "0.3%"). A bare number with no suffix/percent is a LEVEL.
    Ambiguous/unparseable text is UNKNOWN with value=None -- we do not
    guess.
    """
    raw = (raw or "").strip()
    if not raw or raw in {"-", "—"}:
        return ParsedValue(None, ValueUnit.UNKNOWN, raw)

    is_percent = raw.endswith("%")
    text = raw[:-1].strip() if is_percent else raw

    suffix = None
    if text and text[-1].upper() in _SUFFIX_MULTIPLIER:
        suffix = text[-1].upper()
        text = text[:-1]

    match = _NUMERIC_RE.search(text.replace(",", ""))
    if not match:
        return ParsedValue(None, ValueUnit.UNKNOWN, raw)
    number = float(match.group(0))

    if is_percent:
        return ParsedValue(number, ValueUnit.PERCENT, raw)
    if suffix:
        thousands = number * _SUFFIX_MULTIPLIER[suffix] / 1000.0
        return ParsedValue(thousands, ValueUnit.THOUSANDS, raw)
    return ParsedValue(number, ValueUnit.LEVEL, raw)


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
    acquisition_timestamp_utc: dt.datetime,
    currency_filter: Optional[str] = None,
    source_url: Optional[str] = None,
    display_timezone: Optional[str] = "America/New_York",
    display_timezone_verified: bool = False,
    raw_artifact_checksum: Optional[str] = None,
) -> List[MacroEvent]:
    """`display_timezone` is the assumed Forex Factory viewer timezone
    (see config/data_sources.yaml `providers.forex_factory.display_timezone`
    for why "America/New_York" is the documented default, and why it is
    an ASSUMPTION rather than a CONFIRMED fact). Pass None to disable the
    assumption entirely and quarantine all timestamps instead.

    `display_timezone_verified` (see the same config's
    `display_timezone_verified` comment) is an explicit operator
    attestation: only set True once you've independently confirmed
    `display_timezone` is correct for this deployment. It's what
    upgrades a row from ASSUMED to CONFIRMED quality -- and
    CONFIRMED-only is what precision-sensitive minute-level joins (e.g.
    macro-release-window checks) should require, never ASSUMED.
    """
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
            # "All Day" / "Tentative" releases have no precise instant to
            # convert, regardless of timezone -- kept distinct from a
            # genuinely resolved timestamp rather than defaulted to
            # midnight and passed off as precise.
            release_ts = None
            quality = TimestampQuality.TENTATIVE
        elif display_timezone:
            result = normalize_timestamp(naive_dt, display_timezone)
            release_ts = result.utc
            # CONFIRMED only if the operator has explicitly attested to
            # verifying display_timezone; otherwise ASSUMED -- a
            # documented default for an anonymous scraping session is
            # not the same thing as an independently verified fact.
            if not result.trusted:
                quality = TimestampQuality.UNRESOLVED
            elif display_timezone_verified:
                quality = TimestampQuality.CONFIRMED
            else:
                quality = TimestampQuality.ASSUMED
        else:
            release_ts = None
            quality = TimestampQuality.UNRESOLVED

        actual = parse_value_with_unit(row.actual_raw)
        forecast = parse_value_with_unit(row.forecast_raw)
        previous = parse_value_with_unit(row.previous_raw)

        events.append(
            MacroEvent(
                event_id=f"ff:{row.date.isoformat()}:{row.time_raw}:{row.event_name}",
                event_family=mapping.event_family,
                indicator=mapping.indicator,
                release_bundle=mapping.release_bundle,
                reference_period=infer_prior_month_reference_period(row.date),
                release_timestamp_utc=release_ts,
                release_timestamp_ny=None,
                timestamp_quality=quality,
                actual=actual.value,
                actual_unit=actual.unit,
                actual_raw_text=actual.raw_text or None,
                provider_forecast=forecast.value,
                provider_forecast_unit=forecast.unit,
                provider_forecast_raw_text=forecast.raw_text or None,
                forecast_source="FOREX_FACTORY",
                previous=previous.value,
                previous_unit=previous.unit,
                previous_raw_text=previous.raw_text or None,
                importance=row.impact,
                source=MacroSource.FOREX_FACTORY,
                source_event_id=None,
                source_timestamp=naive_dt,
                source_timezone=display_timezone if (naive_dt is not None and display_timezone) else "UNKNOWN",
                source_url=source_url,
                raw_artifact_checksum=raw_artifact_checksum,
                retrieval_timestamp_utc=acquisition_timestamp_utc,
            )
        )

    return events


def normalize_forex_factory_file(
    path: Path,
    year: int,
    month: int,
    event_mapping: EventMapping,
    acquisition_timestamp_utc: dt.datetime,
    currency_filter: Optional[str] = None,
    display_timezone: Optional[str] = "America/New_York",
    display_timezone_verified: bool = False,
) -> List[MacroEvent]:
    from ..manifest import checksum_file

    html = path.read_text(encoding="utf-8")
    raw_rows = parse_forex_factory_html(html, year, month)
    return normalize_forex_factory_rows(
        raw_rows,
        event_mapping,
        acquisition_timestamp_utc,
        raw_artifact_checksum=checksum_file(path),
        currency_filter=currency_filter,
        source_url=str(path),
        display_timezone=display_timezone,
        display_timezone_verified=display_timezone_verified,
    )
