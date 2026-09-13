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
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

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
    # Populated only when parsed from the page's embedded structured
    # data (parse_forex_factory_structured) -- the HTML-table parser
    # (parse_forex_factory_html) has no source for any of these, so they
    # stay at their defaults (None/""/False) on that path. See
    # normalize_forex_factory_rows for how each is used.
    event_instance_id: Optional[str] = None  # Forex Factory's own per-release id -- canonical event_id
    event_template_id: Optional[str] = None  # Forex Factory's own recurring-event id ("ebaseId") -- source_event_id
    revision_raw: str = ""  # the page's own revised-previous text, distinct from `previous_raw`
    release_dateline_utc: Optional[int] = None  # precise Unix timestamp (seconds) for this specific release
    time_masked: bool = False  # True for "All Day"/tentative releases -- release_dateline_utc is a day anchor, not a precise instant, when this is True


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


# ---------------------------------------------------------------------------
# Structured-data parser (primary source when available)
#
# Every Forex Factory calendar page ALSO embeds the exact data backing its
# table as a JS object literal: `window.calendarComponentStates[N] = {
# days: [...] }`. The `days` array's own contents (each day, and every
# event nested inside it) are plain, fully double-quoted JSON -- only the
# OUTER wrapping object (siblings of `days` like `time`, `upNext`,
# `defaultSearchSuggestions`) uses non-JSON JS literal syntax (unquoted
# keys, single-quoted strings) that we never need to touch, since we only
# ever bracket-match and `json.loads()` the `days` array itself.
#
# PREFERRED over the HTML table (parse_forex_factory_html) when present:
# it carries fields the table's DOM never exposes at all -- Forex
# Factory's own event/template ids, a precise per-event Unix timestamp
# (`dateline`), and the revised-previous value -- and it is immune to the
# table's markup changing shape. Multiple identical state blocks are
# common on one page (observed: two, byte-identical, on a real captured
# page); every block found is parsed and events are deduplicated by id
# (last occurrence wins), so this stays safe even if the duplication
# count/reason ever changes.
# ---------------------------------------------------------------------------

_DAYS_ARRAY_RE = re.compile(r"calendarComponentStates\[\d+\]\s*=\s*\{\s*days:\s*\[")


def _extract_days_arrays(html: str) -> List[str]:
    """Bracket-matches each `days: [...]` array following a
    `calendarComponentStates[N] = {` assignment, returning each array's
    raw text (still needing json.loads). Never touches anything outside
    that bracket-matched span, so the surrounding non-JSON JS syntax
    elsewhere in the same object is never a concern."""
    arrays: List[str] = []
    for m in _DAYS_ARRAY_RE.finditer(html):
        start = m.end() - 1  # position of the opening '['
        depth = 0
        i = start
        n = len(html)
        while i < n:
            ch = html[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arrays.append(html[start : i + 1])
                    break
            i += 1
    return arrays


def _parse_structured_day_date(date_text: str) -> Optional[dt.date]:
    """A day object's own "date" field, e.g. "Jan 20, 2016" -- distinct
    from the day object's OTHER "date" field ("Fri <span>Jan 1</span>",
    only present at the top of each day entry, never on an individual
    event); every EVENT's own "date" is always the plain "Mon D, YYYY"
    form, which is what this parses."""
    try:
        return dt.datetime.strptime(date_text.strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def parse_forex_factory_structured(html: str) -> List[ForexFactoryRawRow]:
    """Extracts every `calendarComponentStates[N] = { days: [...] }` block
    embedded in `html` and returns one ForexFactoryRawRow per event,
    deduplicated by Forex Factory's own event id (last occurrence wins
    across blocks). Returns an EMPTY list (never raises) if the page has
    no such block, or if none of the blocks found parse as valid JSON --
    callers must treat that as "structured data unavailable" and fall
    back to parse_forex_factory_html, never as an error on its own.
    """
    by_id: Dict[str, ForexFactoryRawRow] = {}

    for array_text in _extract_days_arrays(html):
        try:
            days = json.loads(array_text)
        except json.JSONDecodeError:
            logger.warning(
                "[ForexFactory parse] found a calendarComponentStates days array that is not valid JSON -- skipping this block"
            )
            continue

        for day in days:
            event_date = _parse_structured_day_date(str(day.get("date", "")))
            for event in day.get("events", []):
                event_id = event.get("id")
                if event_id is None:
                    continue
                # An event's OWN "date" (e.g. "Jan 20, 2016") is
                # authoritative when present; the day entry's date is
                # only a fallback for the rare event missing its own.
                row_date = _parse_structured_day_date(str(event.get("date", ""))) or event_date
                if row_date is None:
                    continue
                ebase_id = event.get("ebaseId")
                dateline = event.get("dateline")
                by_id[str(event_id)] = ForexFactoryRawRow(
                    date=row_date,
                    time_raw=str(event.get("timeLabel", "")),
                    currency=str(event.get("currency", "")),
                    impact=_impact_from_classes([str(event.get("impactClass", ""))]),
                    event_name=str(event.get("name", "")),
                    actual_raw=str(event.get("actual", "")),
                    forecast_raw=str(event.get("forecast", "")),
                    previous_raw=str(event.get("previous", "")),
                    event_instance_id=str(event_id),
                    event_template_id=(str(ebase_id) if ebase_id is not None else None),
                    revision_raw=str(event.get("revision", "")),
                    release_dateline_utc=(int(dateline) if dateline is not None else None),
                    time_masked=bool(event.get("timeMasked", False)),
                )

    return list(by_id.values())


def parse_forex_factory_day_dates(html: str, display_timezone: str = "America/New_York") -> List[dt.date]:
    """The TRUE set of calendar days a page's structured data actually
    rendered -- including zero-event days (weekends, holidays with no
    tracked release) that `parse_forex_factory_structured` never
    produces a row for at all. Used to tell "this month has at least one
    event" (which parse_forex_factory_structured alone can only answer)
    apart from "this month's coverage is actually complete" -- a real
    captured page can legitimately stop mid-month, and the caller must
    not conflate the two.

    Each day object carries its OWN `dateline` (a Unix timestamp
    anchoring that calendar day at local midnight) IN ADDITION to a
    human-readable, year-LESS "date" string ("Fri <span>Jan 1</span>") --
    `dateline` is used here specifically because it is unambiguous and
    year-inclusive; verified against a real captured page: dateline
    1451624400 decodes to 2016-01-01 00:00:00 in America/New_York,
    exactly "Fri Jan 1" 2016 (see normalize_forex_factory_rows for the
    equivalent event-level verification).
    `display_timezone` is the NAMED zone (e.g. "America/New_York") used
    to convert each day's `dateline` into a calendar date -- prefer
    passing the page's own `timezone_name` (parse_forex_factory_
    timezone_name) over the "America/New_York" default when available.
    Deliberately a named zone, never a raw numeric UTC offset: a real
    captured page separately exposes `timezone: '-4'.replace(...)` (the
    CURRENT session's offset at save time, e.g. EDT in September) right
    next to `timezone_name: 'America/New_York'` -- using the numeric
    offset for a January date would apply the wrong (summer, not
    winter) DST rule. `zoneinfo.ZoneInfo` resolves DST correctly for
    ANY historical date given the named zone, which is why only the
    name is ever used here.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(display_timezone)
    dates: set[dt.date] = set()
    for array_text in _extract_days_arrays(html):
        try:
            days = json.loads(array_text)
        except json.JSONDecodeError:
            continue
        for day in days:
            dateline = day.get("dateline")
            if dateline is None:
                continue
            try:
                utc_dt = dt.datetime.fromtimestamp(int(dateline), tz=dt.timezone.utc)
                dates.add(utc_dt.astimezone(tz).date())
            except (OSError, OverflowError, ValueError):
                continue
    return sorted(dates)


_TIMEZONE_NAME_RE = re.compile(r"timezone_name:\s*'([^']+)'")


def parse_forex_factory_timezone_name(html: str) -> Optional[str]:
    """Extracts the page's own declared display timezone, e.g.
    `timezone_name: 'America/New_York'` from `window.FF = {...}`.
    Deliberately does NOT read the neighboring `timezone: '-4'...`
    field -- that is a raw numeric UTC offset for whatever moment the
    page happened to be SAVED, not a fact about any of the historical
    dates the page describes (see parse_forex_factory_day_dates for the
    concrete DST trap this avoids). Returns None if not found -- callers
    should fall back to the configured default, never guess a value."""
    match = _TIMEZONE_NAME_RE.search(html)
    return match.group(1) if match else None


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

        # PRECISE PROVENANCE PATH: when this row came from the embedded
        # structured data (parse_forex_factory_structured), Forex
        # Factory's own `dateline` is a genuine Unix timestamp -- UTC by
        # definition, no timezone assumption involved at all. Verified
        # against a real captured page: dateline 1453282200 decodes to
        # 2016-01-20 09:30:00 UTC, which is exactly 2016-01-20 04:30:00
        # America/New_York -- matching that event's own displayed
        # "4:30am" timeLabel precisely (also independently confirming
        # America/New_York as this page's real display timezone). This
        # is strictly better evidence than parsing "4:30am" text and
        # ASSUMING a timezone, so it earns CONFIRMED quality outright --
        # never gated behind `display_timezone_verified` the way the
        # text-parsing fallback below is. `time_masked` (Forex Factory's
        # own flag for "All Day"/tentative releases) excludes this path:
        # for those, `dateline` anchors the DAY, not a precise instant.
        if row.release_dateline_utc is not None and not row.time_masked:
            release_ts = dt.datetime.fromtimestamp(row.release_dateline_utc, tz=dt.timezone.utc)
            quality = TimestampQuality.CONFIRMED
        elif naive_dt is None:
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
        revision = parse_value_with_unit(row.revision_raw)

        # Canonical event_id prefers Forex Factory's OWN per-release id
        # (stable, globally unique, immune to text-formatting quirks)
        # when the structured-data parser provided one; the HTML-table
        # parser has no such id, so it keeps a composite fallback
        # identity instead: date + currency + displayed time + name
        # (currency included so two DIFFERENT countries' same-named/
        # same-time events, e.g. a shared "Rate Decision" release label,
        # can never collide).
        event_id = (
            f"ff:{row.event_instance_id}"
            if row.event_instance_id
            else f"ff:{row.date.isoformat()}:{row.currency}:{row.time_raw}:{row.event_name}"
        )

        events.append(
            MacroEvent(
                event_id=event_id,
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
                revised_previous=revision.value,
                importance=row.impact,
                source=MacroSource.FOREX_FACTORY,
                source_event_id=row.event_template_id,
                source_timestamp=naive_dt,
                source_timezone=display_timezone if (naive_dt is not None and display_timezone) else "UNKNOWN",
                source_url=source_url,
                raw_artifact_checksum=raw_artifact_checksum,
                retrieval_timestamp_utc=acquisition_timestamp_utc,
            )
        )

    return events


def parse_forex_factory_page(html: str, year: int, month: int) -> List[ForexFactoryRawRow]:
    """Structured data first, HTML-table fallback -- the one entry point
    both the checkpoint-validating downloader (fetch/forex_factory.py)
    and normalize_forex_factory_file should use, so neither has to
    duplicate the "prefer structured, fall back to table" decision.
    `year`/`month` are only used by the table-parser fallback (to
    disambiguate a bare "Jan 20" date cell); the structured-data path
    ignores them -- every event there already carries its own full date.
    """
    structured_rows = parse_forex_factory_structured(html)
    if structured_rows:
        return structured_rows
    return parse_forex_factory_html(html, year, month)


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
    raw_rows = parse_forex_factory_page(html, year, month)
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
