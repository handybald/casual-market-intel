import datetime as dt

from src.data.schemas import MacroEvent, MacroSource, ValidationStatus
from src.data.validation.macro import compare_macro_sources, summarize


def _event(source, actual, family="NFP", day=5, official_actual=None):
    ts = dt.datetime(2024, 1, day, 13, 30, tzinfo=dt.timezone.utc)
    return MacroEvent(
        event_id=f"{source}:{family}:{day}",
        event_family=family,
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=ts,
        actual=actual,
        official_actual=official_actual,
        source=source,
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )


def test_exact_match_across_sources():
    events = [
        _event(MacroSource.MQL5, 216.0),
        _event(MacroSource.FOREX_FACTORY, 216.0),
    ]
    results = compare_macro_sources(events)
    assert len(results) == 1
    assert results[0].status == ValidationStatus.MATCH


def test_rounding_difference_within_tolerance():
    events = [
        _event(MacroSource.MQL5, 216.0),
        _event(MacroSource.FOREX_FACTORY, 216.03),
    ]
    results = compare_macro_sources(events, tolerance=0.05)
    assert results[0].status == ValidationStatus.ROUNDING_DIFFERENCE


def test_mismatch_beyond_tolerance():
    events = [
        _event(MacroSource.MQL5, 216.0),
        _event(MacroSource.FOREX_FACTORY, 225.0),
    ]
    results = compare_macro_sources(events, tolerance=0.05)
    assert results[0].status == ValidationStatus.MISMATCH


def test_missing_when_only_one_source_reports():
    events = [_event(MacroSource.MQL5, 216.0)]
    results = compare_macro_sources(events)
    assert results[0].status == ValidationStatus.MISSING


def test_missing_when_actual_is_none_everywhere():
    events = [_event(MacroSource.MQL5, None), _event(MacroSource.FOREX_FACTORY, None)]
    results = compare_macro_sources(events)
    assert results[0].status == ValidationStatus.MISSING


def test_groups_are_independent_per_event_family_and_date():
    events = [
        _event(MacroSource.MQL5, 1.0, family="CPI_MOM", day=11),
        _event(MacroSource.FOREX_FACTORY, 1.0, family="CPI_MOM", day=11),
        _event(MacroSource.MQL5, 216.0, family="NFP", day=5),
        _event(MacroSource.FOREX_FACTORY, 999.0, family="NFP", day=5),
    ]
    results = compare_macro_sources(events)
    assert len(results) == 2
    counts = summarize(results)
    assert counts["MATCH"] == 1
    assert counts["MISMATCH"] == 1


def test_summarize_counts_by_status():
    events = [
        _event(MacroSource.MQL5, 1.0, family="A", day=1),
        _event(MacroSource.FOREX_FACTORY, 1.0, family="A", day=1),
        _event(MacroSource.MQL5, 2.0, family="B", day=2),
    ]
    results = compare_macro_sources(events)
    counts = summarize(results)
    assert counts == {"MATCH": 1, "MISSING": 1}
