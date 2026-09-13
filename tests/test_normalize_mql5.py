import datetime as dt
from pathlib import Path

from src.data.event_mapping import load_event_mapping
from src.data.fetch.mql5 import parse_mql5_csv
from src.data.normalize.mql5 import normalize_mql5_rows
from src.data.schemas import TimestampQuality

FIXTURE = Path(__file__).parent / "fixtures" / "mql5_sample.csv"
ACQUIRED_AT = dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc)


def test_parse_mql5_csv_row_count_and_missing_values():
    rows = parse_mql5_csv(FIXTURE)
    assert len(rows) == 4

    cpi_row = next(r for r in rows if r.event_name == "CPI m/m")
    assert cpi_row.actual_value is None  # blank field in CSV -> None
    assert cpi_row.forecast_value == 0.2

    unemployment_row = next(r for r in rows if r.event_name == "Unemployment Rate")
    assert unemployment_row.revised_prev_value is None


def test_parse_mql5_csv_preserves_value_id_and_revision():
    rows = parse_mql5_csv(FIXTURE)
    nfp = next(r for r in rows if r.event_name == "Nonfarm Payrolls")
    assert nfp.value_id == "900000100001"
    assert nfp.revision == 0
    assert nfp.unit == "JOB"
    assert nfp.multiplier == "THOUSANDS"
    assert nfp.period_raw == "2023.12.01"


def test_normalize_mql5_rows_maps_known_events_and_skips_unmapped():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT)

    families = {e.event_family for e in events}
    assert families == {"NFP", "UNEMPLOYMENT_RATE", "CPI_MOM"}
    assert len(events) == 3  # "Some Unmapped MQL5 Event" dropped


def test_normalize_mql5_quarantines_timestamp_without_confirmed_broker_timezone():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT, broker_timezone=None)

    for e in events:
        assert e.release_timestamp_utc is None
        assert e.timestamp_quality == TimestampQuality.UNRESOLVED.value
        assert e.timestamp_is_trustworthy is False
        assert e.is_quarantined is True
        # Original evidence is preserved even though it's quarantined.
        assert e.source_timestamp is not None


def test_normalize_mql5_uses_confirmed_broker_timezone_when_configured():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT, broker_timezone="America/New_York")

    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.timestamp_quality == TimestampQuality.CONFIRMED.value
    assert nfp.is_quarantined is False
    # 2024-01-05 13:30 ET (winter, EST = UTC-5) -> 18:30 UTC
    assert nfp.release_timestamp_utc == dt.datetime(2024, 1, 5, 18, 30, tzinfo=dt.timezone.utc)


def test_normalize_mql5_forecast_source_is_mql5_diagnostics_only():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT)
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.forecast_source == "MQL5"
    assert nfp.provider_forecast == 170.0
    assert nfp.actual == 216.0
    assert nfp.previous == 173.0
    assert nfp.revised_previous == 182.0


def test_normalize_mql5_uses_real_period_field_when_present():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT)
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.reference_period == dt.date(2023, 12, 1)  # from the fixture's real `period` column


def test_normalize_mql5_prefers_real_period_over_heuristic_when_they_differ():
    from src.data.fetch.mql5 import Mql5Row

    row = Mql5Row(
        value_id="1", event_id="1", event_name="Nonfarm Payrolls", country_code="US", currency_code="USD",
        importance="HIGH", event_time_raw="2024.01.05 13:30:00", period_raw="2023.10.01",  # NOT the prior month
        unit="JOB", multiplier="THOUSANDS", actual_value=216.0, forecast_value=170.0,
        prev_value=173.0, revised_prev_value=None, revision=0, source_timezone="SERVER",
    )
    mapping = load_event_mapping()
    [event] = normalize_mql5_rows([row], mapping, ACQUIRED_AT)
    assert event.reference_period == dt.date(2023, 10, 1)  # real period wins, not Dec 2023


def test_normalize_mql5_falls_back_to_heuristic_when_period_missing():
    from src.data.fetch.mql5 import Mql5Row

    row = Mql5Row(
        value_id="1", event_id="1", event_name="Nonfarm Payrolls", country_code="US", currency_code="USD",
        importance="HIGH", event_time_raw="2024.01.05 13:30:00", period_raw=None,
        unit="JOB", multiplier="THOUSANDS", actual_value=216.0, forecast_value=170.0,
        prev_value=173.0, revised_prev_value=None, revision=0, source_timezone="SERVER",
    )
    mapping = load_event_mapping()
    [event] = normalize_mql5_rows([row], mapping, ACQUIRED_AT)
    assert event.reference_period == dt.date(2023, 12, 1)  # heuristic: prior month


def test_normalize_mql5_percent_unit_is_comparable_other_units_are_unknown():
    """Regression: unit/multiplier are distinct MQL5 enums; only the
    PERCENT-family `unit` values are confidently mapped to a comparable
    ValueUnit. JOB (an `unit`, not `multiplier`, value) must NOT be
    treated as a safe/comparable unit -- it's UNKNOWN, so it's excluded
    from numerical cross-source comparison (see validation/macro.py)."""
    from src.data.schemas import ValueUnit

    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT)

    cpi = next(e for e in events if e.event_family == "CPI_MOM")
    assert cpi.actual_unit == ValueUnit.PERCENT.value

    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.actual_unit == ValueUnit.UNKNOWN.value


def test_normalize_mql5_acquisition_time_is_preserved_not_normalize_time():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping, ACQUIRED_AT)
    for e in events:
        assert e.retrieval_timestamp_utc == ACQUIRED_AT
        assert e.normalized_at_utc >= ACQUIRED_AT  # normalization happens after acquisition
