"""Regression tests for Forex Factory's embedded structured-data parser
and the provenance it feeds into MacroEvent.

BACKGROUND: forexfactory.com serves an active Cloudflare managed
challenge against automated HTTP requests to /calendar (confirmed live:
a plain `requests` GET gets a 403 with `cf-mitigated: challenge` and a
"Just a moment..." JS-verification body -- reproduced with the exact
same request this pipeline's downloader sends). This project does not
attempt to defeat that challenge. Historical data is instead collected
by a human saving the calendar page from their own regular browser, then
imported via `scripts/import_forex_factory.py`.

Every real Forex Factory calendar page embeds the exact data backing its
table as a JS object literal: `window.calendarComponentStates[N] = {
days: [...] }`. This is PREFERRED over the HTML `<table>` (the pre-
existing `parse_forex_factory_html`) because it carries fields the table
never exposes: Forex Factory's own event/template ids, a precise
per-event Unix timestamp (`dateline`), and the revised-previous value --
and because it is immune to the table's markup changing shape.

The exact byte-level structure below (double-quoted JSON `days` array
nested inside a JS object literal with OTHER, non-JSON sibling keys like
`time: '7:45am'` alongside it) and the specific `dateline` UTC/timezone
math were verified against a REAL captured page (a user-saved
~3.2MB Forex Factory quarter export) before being distilled into the
compact synthetic fixtures used here.
"""
from __future__ import annotations

import datetime as dt
import json

from src.data.event_mapping import load_event_mapping
from src.data.normalize.forex_factory import (
    ForexFactoryRawRow,
    normalize_forex_factory_rows,
    parse_forex_factory_day_dates,
    parse_forex_factory_structured,
)
from src.data.schemas import TimestampQuality


def _event(
    event_id, ebase_id, name, currency, dateline, time_label, date_str,
    actual="", forecast="", previous="", revision="", impact_class="icon--ff-impact-red",
    time_masked=False,
):
    return {
        "id": event_id, "ebaseId": ebase_id, "name": name, "prefixedName": f"XX {name}",
        "notice": "", "dateline": dateline, "country": "US", "currency": currency,
        "impactClass": impact_class, "impactTitle": "High Impact Expected",
        "timeLabel": time_label, "timeMasked": time_masked,
        "actual": actual, "forecast": forecast, "previous": previous, "revision": revision,
        "date": date_str,
    }


def _day(date_label, dateline, events):
    return {"date": date_label, "dateline": dateline, "add": "", "events": events}


def _dateline(y, m, d, hour=0, minute=0):
    """Real Unix timestamp for `hour:minute` America/New_York on the
    given date -- built the same way the real embedded data is (verified
    against a real captured page: 1453282200 decodes to exactly
    2016-01-20 04:30 America/New_York)."""
    from zoneinfo import ZoneInfo

    local = dt.datetime(y, m, d, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return int(local.timestamp())


def _wrap_state_block(days, block_index=1, extra_js="time: '7:45am', upNext: 'sep13.2026',"):
    """Mirrors the real page's exact non-JSON-JS wrapping -- a `days`
    array of pure JSON alongside sibling keys that are NOT valid JSON
    (unquoted keys, single-quoted string values) -- to confirm the
    parser only ever touches the bracket-matched `days` array itself."""
    return (
        "<script type=\"text/javascript\">if (typeof window.calendarComponentStates === 'undefined') "
        "{ window.calendarComponentStates = {} }\n"
        f"window.calendarComponentStates[{block_index}] = {{\n"
        f"days: {json.dumps(days)},\n"
        f"{extra_js}\n"
        "defaultSearchSuggestions: [{\"solo\":\"x\"}]\n"
        "}</script>"
    )


def _page(days, duplicate_block=True):
    html = "<html><head><title>Calendar | Forex Factory</title></head><body>" + _wrap_state_block(days, 1)
    if duplicate_block:
        html += _wrap_state_block(days, 1)  # real pages observed to embed 2 identical blocks
    html += "</body></html>"
    return html


# ---------------------------------------------------------------------------
# parse_forex_factory_structured
# ---------------------------------------------------------------------------

def test_extracts_events_from_days_array_ignoring_nonjson_sibling_keys():
    dateline = _dateline(2016, 1, 20, 4, 30)
    events = [_event(59855, 180, "Average Earnings Index 3m/y", "GBP", dateline, "4:30am", "Jan 20, 2016",
                      actual="2.0%", forecast="2.1%", previous="2.4%")]
    html = _page([_day("Wed <span>Jan 20</span>", _dateline(2016, 1, 20), events)])

    rows = parse_forex_factory_structured(html)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_instance_id == "59855"
    assert row.event_template_id == "180"
    assert row.event_name == "Average Earnings Index 3m/y"
    assert row.release_dateline_utc == dateline
    assert row.time_masked is False
    assert row.actual_raw == "2.0%"
    assert row.forecast_raw == "2.1%"
    assert row.previous_raw == "2.4%"
    assert row.date == dt.date(2016, 1, 20)


def test_duplicate_state_blocks_deduplicated_by_event_id():
    dateline = _dateline(2016, 1, 20, 4, 30)
    events = [_event(59855, 180, "Average Earnings Index 3m/y", "GBP", dateline, "4:30am", "Jan 20, 2016")]
    html = _page([_day("Wed <span>Jan 20</span>", _dateline(2016, 1, 20), events)], duplicate_block=True)

    rows = parse_forex_factory_structured(html)
    assert len(rows) == 1  # not 2, despite two identical embedded blocks


def test_revision_field_maps_to_revised_previous():
    dateline = _dateline(2016, 2, 5, 8, 30)
    events = [_event(61728, 66, "Non-Farm Employment Change", "USD", dateline, "8:30am", "Feb 5, 2016",
                      actual="151K", forecast="190K", previous="292K", revision="262K")]
    html = _page([_day("Fri <span>Feb 5</span>", _dateline(2016, 2, 5), events)])

    rows = parse_forex_factory_structured(html)
    assert rows[0].revision_raw == "262K"


def test_no_structured_data_returns_empty_list_not_an_error():
    html = "<html><body><table><tr class='calendar__row'></tr></table></body></html>"
    assert parse_forex_factory_structured(html) == []


def test_malformed_days_array_in_one_block_skipped_not_raised():
    html = (
        "<script>window.calendarComponentStates[1] = {\n"
        "days: [{\"date\": \"Jan 1, 2016\", not_valid_json here,\n"
        "}</script>"
    )
    assert parse_forex_factory_structured(html) == []


# ---------------------------------------------------------------------------
# parse_forex_factory_day_dates -- the TRUE rendered-day universe (used for
# month-completeness detection), distinct from event-bearing dates.
# ---------------------------------------------------------------------------

def test_day_dates_includes_zero_event_days():
    days = [
        _day("Fri <span>Jan 1</span>", _dateline(2016, 1, 1), []),  # holiday, zero events
        _day("Sat <span>Jan 2</span>", _dateline(2016, 1, 2), []),  # weekend, zero events
        _day("Wed <span>Jan 20</span>", _dateline(2016, 1, 20), [
            _event(1, 1, "Some Event", "USD", _dateline(2016, 1, 20, 8, 30), "8:30am", "Jan 20, 2016"),
        ]),
    ]
    html = _page(days)
    dates = parse_forex_factory_day_dates(html)
    assert dates == [dt.date(2016, 1, 1), dt.date(2016, 1, 2), dt.date(2016, 1, 20)]


def test_day_level_dateline_decodes_to_correct_calendar_date():
    # Real value from a captured page: 1451624400 -> Fri Jan 1 2016 in
    # America/New_York (verified independently before this test was written).
    html = _page([_day("Fri <span>Jan 1</span>", 1451624400, [])])
    assert parse_forex_factory_day_dates(html) == [dt.date(2016, 1, 1)]


# ---------------------------------------------------------------------------
# normalize_forex_factory_rows: CONFIRMED timestamp from dateline, event_id
# scheme, source_event_id, revised_previous.
# ---------------------------------------------------------------------------

def test_dateline_backed_row_gets_confirmed_quality_and_correct_utc():
    event_mapping = load_event_mapping()
    dateline = _dateline(2016, 1, 20, 4, 30)
    row = ForexFactoryRawRow(
        date=dt.date(2016, 1, 20), time_raw="4:30am", currency="USD", impact="HIGH",
        event_name="Non-Farm Employment Change", actual_raw="292K", forecast_raw="200K", previous_raw="211K",
        event_instance_id="59855", event_template_id="180", revision_raw="",
        release_dateline_utc=dateline, time_masked=False,
    )
    events = normalize_forex_factory_rows([row], event_mapping, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert len(events) == 1
    ev = events[0]
    assert ev.timestamp_quality == TimestampQuality.CONFIRMED.value
    assert ev.release_timestamp_utc == dt.datetime(2016, 1, 20, 9, 30, tzinfo=dt.timezone.utc)
    assert ev.event_id == "ff:59855"
    assert ev.source_event_id == "180"


def test_time_masked_row_does_not_use_dateline_as_precise_instant():
    """An "All Day"/tentative release's dateline anchors the DAY, not a
    precise release instant -- must not be trusted as CONFIRMED."""
    event_mapping = load_event_mapping()
    row = ForexFactoryRawRow(
        date=dt.date(2016, 1, 1), time_raw="All Day", currency="CHF", impact="NONE",
        event_name="Bank Holiday", actual_raw="", forecast_raw="", previous_raw="",
        event_instance_id="61525", event_template_id="392", revision_raw="",
        release_dateline_utc=_dateline(2016, 1, 1), time_masked=True,
    )
    events = normalize_forex_factory_rows([row], event_mapping, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    # No mapping exists for "Bank Holiday" so it's filtered before quality
    # even matters -- use a mapped name instead to actually exercise the
    # quality/timestamp path.
    row2 = row._replace(event_name="Non-Farm Employment Change")
    events2 = normalize_forex_factory_rows([row2], event_mapping, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert len(events2) == 1
    assert events2[0].timestamp_quality == TimestampQuality.TENTATIVE.value
    assert events2[0].release_timestamp_utc is None


def test_fallback_event_id_scheme_used_when_no_structured_id_present():
    """The HTML-table parser path (no native FF ids available) must keep
    the original composite event_id scheme -- unchanged behavior."""
    event_mapping = load_event_mapping()
    row = ForexFactoryRawRow(
        date=dt.date(2016, 1, 20), time_raw="4:30am", currency="USD", impact="HIGH",
        event_name="Non-Farm Employment Change", actual_raw="292K", forecast_raw="200K", previous_raw="211K",
    )  # no event_instance_id/event_template_id/dateline -- defaults
    events = normalize_forex_factory_rows([row], event_mapping, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert len(events) == 1
    assert events[0].event_id == "ff:2016-01-20:USD:4:30am:Non-Farm Employment Change"
    assert events[0].source_event_id is None
    assert events[0].timestamp_quality == TimestampQuality.ASSUMED.value


def test_revised_previous_populated_from_revision_field():
    event_mapping = load_event_mapping()
    row = ForexFactoryRawRow(
        date=dt.date(2016, 2, 5), time_raw="8:30am", currency="USD", impact="HIGH",
        event_name="Non-Farm Employment Change", actual_raw="151K", forecast_raw="190K",
        previous_raw="292K", revision_raw="262K",
        event_instance_id="61728", event_template_id="66",
        release_dateline_utc=_dateline(2016, 2, 5, 8, 30), time_masked=False,
    )
    events = normalize_forex_factory_rows([row], event_mapping, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert len(events) == 1
    assert events[0].revised_previous == 262.0
