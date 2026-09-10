from pathlib import Path

from src.data.event_mapping import load_event_mapping
from src.data.normalize.forex_factory import (
    parse_forex_factory_html,
    normalize_forex_factory_rows,
    _parse_numeric,
    _parse_time_of_day,
)
import datetime as dt

FIXTURE = Path(__file__).parent / "fixtures" / "forex_factory_sample.html"


def test_parse_forex_factory_html_row_count_and_dates():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    assert len(rows) == 5
    assert rows[0].date == dt.date(2024, 1, 1)
    assert rows[-1].date == dt.date(2024, 1, 5)


def test_time_carries_forward_to_next_row_when_blank():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    cpi_row = next(r for r in rows if r.event_name == "CPI m/m")
    core_cpi_row = next(r for r in rows if r.event_name == "Core CPI m/m")
    assert cpi_row.time_raw == "8:30am"
    assert core_cpi_row.time_raw == "8:30am"  # inherited, blank in markup


def test_impact_classification():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    cpi_row = next(r for r in rows if r.event_name == "CPI m/m")
    speech_row = next(r for r in rows if "Buba" in r.event_name)
    assert cpi_row.impact == "HIGH"
    assert speech_row.impact == "LOW"


def test_normalize_filters_by_currency_and_mapping():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, currency_filter="USD")

    families = {e.event_family for e in events}
    # EUR speech row filtered by currency; unmapped USD speech dropped by mapping
    assert families == {"CPI_MOM", "CORE_CPI_MOM", "NFP"}


def test_normalize_missing_actual_value_is_none():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, currency_filter="USD")

    core_cpi = next(e for e in events if e.event_family == "CORE_CPI_MOM")
    assert core_cpi.actual is None
    assert core_cpi.provider_forecast == 0.3
    assert core_cpi.forecast_source == "FOREX_FACTORY"


def test_normalize_uses_provider_forecast_not_economist_consensus():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, currency_filter="USD")
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.forecast_source == "FOREX_FACTORY"
    assert nfp.provider_forecast == 170.0
    assert nfp.actual == 216.0


def test_parse_numeric_handles_suffixes_and_missing():
    assert _parse_numeric("0.3%") == 0.3
    assert _parse_numeric("216K") == 216.0
    assert _parse_numeric("-0.2%") == -0.2
    assert _parse_numeric("") is None
    assert _parse_numeric("-") is None


def test_parse_time_of_day_am_pm_and_special_values():
    d = dt.date(2024, 1, 1)
    assert _parse_time_of_day(d, "8:30am") == dt.datetime(2024, 1, 1, 8, 30)
    assert _parse_time_of_day(d, "1:15pm") == dt.datetime(2024, 1, 1, 13, 15)
    assert _parse_time_of_day(d, "12:00am") == dt.datetime(2024, 1, 1, 0, 0)
    assert _parse_time_of_day(d, "12:00pm") == dt.datetime(2024, 1, 1, 12, 0)
    assert _parse_time_of_day(d, "All Day") is None
    assert _parse_time_of_day(d, "Tentative") is None


def test_december_to_january_wraparound_year_resolution():
    # Requesting Dec 2024 page, trailing calendar days spill into Jan -> next year.
    html = """
    <tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>Wed Jan 1</span></td></tr>
    <tr class="calendar__row">
      <td class="calendar__date"></td><td class="calendar__time">8:30am</td>
      <td class="calendar__currency">USD</td>
      <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
      <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
      <td class="calendar__actual">0.1%</td><td class="calendar__forecast">0.1%</td><td class="calendar__previous">0.1%</td>
    </tr>
    """
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=12)
    assert rows[0].date == dt.date(2025, 1, 1)
