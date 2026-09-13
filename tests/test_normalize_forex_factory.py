import datetime as dt
from pathlib import Path

from src.data.event_mapping import load_event_mapping
from src.data.normalize.forex_factory import (
    parse_forex_factory_html,
    normalize_forex_factory_rows,
    parse_value_with_unit,
    _parse_time_of_day,
)
from src.data.schemas import TimestampQuality, ValueUnit

FIXTURE = Path(__file__).parent / "fixtures" / "forex_factory_sample.html"
ACQUIRED_AT = dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc)


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
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")

    families = {e.event_family for e in events}
    # EUR speech row filtered by currency; unmapped USD speech dropped by mapping
    assert families == {"CPI_MOM", "CORE_CPI_MOM", "NFP"}


def test_normalize_missing_actual_value_is_none():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")

    core_cpi = next(e for e in events if e.event_family == "CORE_CPI_MOM")
    assert core_cpi.actual is None
    assert core_cpi.provider_forecast == 0.3
    assert core_cpi.forecast_source == "FOREX_FACTORY"


def test_normalize_uses_provider_forecast_not_economist_consensus():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.forecast_source == "FOREX_FACTORY"
    assert nfp.actual is not None


def test_normalize_infers_prior_month_reference_period():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    nfp = next(e for e in events if e.event_family == "NFP")
    # Released 2024-01-05 -> describes December 2023 data.
    assert nfp.reference_period == dt.date(2023, 12, 1)


def test_normalize_acquisition_time_preserved():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    for e in events:
        assert e.retrieval_timestamp_utc == ACQUIRED_AT


# -- unit-aware value parsing (regression: 2.5M vs 2500K must be equal) --

def test_parse_value_with_unit_percent():
    p = parse_value_with_unit("0.3%")
    assert p.value == 0.3
    assert p.unit == ValueUnit.PERCENT
    assert p.raw_text == "0.3%"


def test_parse_value_with_unit_negative_percent():
    p = parse_value_with_unit("-0.2%")
    assert p.value == -0.2
    assert p.unit == ValueUnit.PERCENT


def test_parse_value_with_unit_missing():
    assert parse_value_with_unit("").value is None
    assert parse_value_with_unit("-").value is None
    assert parse_value_with_unit("—").value is None


def test_parse_value_with_unit_thousands_and_millions_normalize_equal():
    """The reproduced bug: '2.5M' and '2500K' represent the same real
    quantity (2,500,000) and must normalize to the same canonical value."""
    m = parse_value_with_unit("2.5M")
    k = parse_value_with_unit("2500K")
    assert m.unit == ValueUnit.THOUSANDS
    assert k.unit == ValueUnit.THOUSANDS
    assert m.value == k.value == 2500.0


def test_parse_value_with_unit_billions():
    p = parse_value_with_unit("1.2B")
    assert p.unit == ValueUnit.THOUSANDS
    assert p.value == 1_200_000.0


def test_parse_value_with_unit_bare_level():
    p = parse_value_with_unit("103.5")
    assert p.unit == ValueUnit.LEVEL
    assert p.value == 103.5


def test_parse_value_with_unit_thousands_separator():
    p = parse_value_with_unit("1,234K")
    assert p.unit == ValueUnit.THOUSANDS
    assert p.value == 1234.0


def test_normalize_preserves_raw_text_and_unit_on_event():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.actual_unit == ValueUnit.THOUSANDS.value
    assert nfp.actual_raw_text == "216K"
    assert nfp.actual == 216.0  # 216K -> 216.0 thousands


# -- timestamp normalization / quality (regression: DST + quarantine) --

def test_parse_time_of_day_am_pm_and_special_values():
    d = dt.date(2024, 1, 1)
    assert _parse_time_of_day(d, "8:30am") == dt.datetime(2024, 1, 1, 8, 30)
    assert _parse_time_of_day(d, "1:15pm") == dt.datetime(2024, 1, 1, 13, 15)
    assert _parse_time_of_day(d, "12:00am") == dt.datetime(2024, 1, 1, 0, 0)
    assert _parse_time_of_day(d, "12:00pm") == dt.datetime(2024, 1, 1, 12, 0)
    assert _parse_time_of_day(d, "All Day") is None
    assert _parse_time_of_day(d, "Tentative") is None


def test_normalize_marks_precise_time_as_assumed_quality_with_dst_conversion():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(
        rows, mapping, ACQUIRED_AT, currency_filter="USD", display_timezone="America/New_York"
    )
    cpi = next(e for e in events if e.event_family == "CPI_MOM")
    assert cpi.timestamp_quality == TimestampQuality.ASSUMED.value
    assert cpi.timestamp_is_trustworthy is False  # ASSUMED != CONFIRMED
    # 2024-01-01 8:30am ET (winter, EST = UTC-5) -> 13:30 UTC
    assert cpi.release_timestamp_utc == dt.datetime(2024, 1, 1, 13, 30, tzinfo=dt.timezone.utc)


def test_normalize_display_timezone_verified_upgrades_to_confirmed():
    """Regression: an operator-attested `display_timezone_verified=True`
    must upgrade quality to CONFIRMED -- without it, ASSUMED must never
    be silently treated as trustworthy."""
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(
        rows, mapping, ACQUIRED_AT, currency_filter="USD", display_timezone="America/New_York",
        display_timezone_verified=True,
    )
    cpi = next(e for e in events if e.event_family == "CPI_MOM")
    assert cpi.timestamp_quality == TimestampQuality.CONFIRMED.value
    assert cpi.timestamp_is_trustworthy is True


def test_normalize_display_timezone_unverified_by_default_stays_assumed():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(
        rows, mapping, ACQUIRED_AT, currency_filter="USD", display_timezone="America/New_York",
    )
    cpi = next(e for e in events if e.event_family == "CPI_MOM")
    assert cpi.timestamp_quality == TimestampQuality.ASSUMED.value


def test_normalize_summer_date_uses_dst_offset():
    html = """
    <tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>Mon Jul 15</span></td></tr>
    <tr class="calendar__row">
      <td class="calendar__date"></td><td class="calendar__time">8:30am</td>
      <td class="calendar__currency">USD</td>
      <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
      <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
      <td class="calendar__actual">0.1%</td><td class="calendar__forecast">0.1%</td><td class="calendar__previous">0.1%</td>
    </tr>
    """
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=7)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    cpi = events[0]
    # 8:30am ET in July (EDT = UTC-4) -> 12:30 UTC (NOT 13:30, which winter DST would give)
    assert cpi.release_timestamp_utc == dt.datetime(2024, 7, 15, 12, 30, tzinfo=dt.timezone.utc)


def test_normalize_tentative_event_is_quarantined_not_midnight():
    html = """
    <tr class="calendar__row calendar__row--day-breaker"><td class="calendar__date"><span>Mon Jan 1</span></td></tr>
    <tr class="calendar__row">
      <td class="calendar__date"></td><td class="calendar__time">Tentative</td>
      <td class="calendar__currency">USD</td>
      <td class="calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
      <td class="calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
      <td class="calendar__actual"></td><td class="calendar__forecast"></td><td class="calendar__previous"></td>
    </tr>
    """
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(rows, mapping, ACQUIRED_AT, currency_filter="USD")
    cpi = events[0]
    assert cpi.timestamp_quality == TimestampQuality.TENTATIVE.value
    assert cpi.release_timestamp_utc is None
    assert cpi.is_quarantined is True


def test_normalize_disabling_display_timezone_quarantines_precise_times_too():
    html = FIXTURE.read_text(encoding="utf-8")
    rows = parse_forex_factory_html(html, requested_year=2024, requested_month=1)
    mapping = load_event_mapping()
    events = normalize_forex_factory_rows(
        rows, mapping, ACQUIRED_AT, currency_filter="USD", display_timezone=None
    )
    for e in events:
        assert e.release_timestamp_utc is None
        assert e.timestamp_quality == TimestampQuality.UNRESOLVED.value
        assert e.is_quarantined is True


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
