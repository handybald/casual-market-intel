from pathlib import Path

from src.data.event_mapping import load_event_mapping
from src.data.fetch.mql5 import parse_mql5_csv
from src.data.normalize.mql5 import normalize_mql5_rows

FIXTURE = Path(__file__).parent / "fixtures" / "mql5_sample.csv"


def test_parse_mql5_csv_row_count_and_missing_values():
    rows = parse_mql5_csv(FIXTURE)
    assert len(rows) == 4

    cpi_row = next(r for r in rows if r.event_name == "CPI m/m")
    assert cpi_row.actual_value is None  # blank field in CSV -> None
    assert cpi_row.forecast_value == 0.2

    unemployment_row = next(r for r in rows if r.event_name == "Unemployment Rate")
    assert unemployment_row.revised_prev_value is None


def test_normalize_mql5_rows_maps_known_events_and_skips_unmapped():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping)

    families = {e.event_family for e in events}
    assert families == {"NFP", "UNEMPLOYMENT_RATE", "CPI_MOM"}
    assert len(events) == 3  # "Some Unmapped MQL5 Event" dropped


def test_normalize_mql5_marks_timestamp_untrustworthy():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping)

    for e in events:
        assert e.source_timezone == "SERVER"
        assert e.timestamp_is_trustworthy is False


def test_normalize_mql5_forecast_source_is_mql5_diagnostics_only():
    rows = parse_mql5_csv(FIXTURE)
    mapping = load_event_mapping()
    events = normalize_mql5_rows(rows, mapping)
    nfp = next(e for e in events if e.event_family == "NFP")
    assert nfp.forecast_source == "MQL5"
    assert nfp.provider_forecast == 170.0
    assert nfp.actual == 216.0
    assert nfp.previous == 173.0
    assert nfp.revised_previous == 182.0
