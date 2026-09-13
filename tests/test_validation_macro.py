import datetime as dt

from src.data.schemas import MacroEvent, MacroSource, ValidationStatus, ValueUnit
from src.data.validation.macro import compare_macro_sources, summarize


def _event(source, actual, family="NFP", day=5, official_actual=None, actual_unit=ValueUnit.THOUSANDS,
           official_actual_unit=ValueUnit.UNKNOWN, reference_period=None, release_timestamp_utc="default"):
    ts = dt.datetime(2024, 1, day, 13, 30, tzinfo=dt.timezone.utc) if release_timestamp_utc == "default" else release_timestamp_utc
    return MacroEvent(
        event_id=f"{source}:{family}:{day}",
        event_family=family,
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=ts,
        reference_period=reference_period,
        actual=actual,
        actual_unit=actual_unit,
        official_actual=official_actual,
        official_actual_unit=official_actual_unit,
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
        _event(MacroSource.MQL5, 1.0, family="CPI_MOM", day=11, actual_unit=ValueUnit.PERCENT),
        _event(MacroSource.FOREX_FACTORY, 1.0, family="CPI_MOM", day=11, actual_unit=ValueUnit.PERCENT),
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
        _event(MacroSource.MQL5, 1.0, family="A", day=1, actual_unit=ValueUnit.PERCENT),
        _event(MacroSource.FOREX_FACTORY, 1.0, family="A", day=1, actual_unit=ValueUnit.PERCENT),
        _event(MacroSource.MQL5, 2.0, family="B", day=2),
    ]
    results = compare_macro_sources(events)
    counts = summarize(results)
    assert counts == {"MATCH": 1, "MISSING": 1}


# -- unit alignment (item #6/#12: never silently compare across units) --

def test_differing_units_do_not_produce_a_false_match_or_mismatch():
    events = [
        _event(MacroSource.MQL5, 216.0, actual_unit=ValueUnit.THOUSANDS),
        _event(MacroSource.FOREX_FACTORY, 216.0, actual_unit=ValueUnit.PERCENT),
    ]
    results = compare_macro_sources(events)
    assert results[0].status == ValidationStatus.MISSING
    assert "unit" in results[0].note.lower()


def test_unknown_unit_value_never_produces_mismatch_against_a_known_unit():
    """Reproduces the exact bug: an UNKNOWN-unit value next to a single
    known-unit (THOUSANDS) value must NOT be numerically compared at
    all -- not MATCH, not MISMATCH, only MISSING (nothing comparable)."""
    events = [
        _event(MacroSource.MQL5, 216.0, actual_unit=ValueUnit.UNKNOWN),
        _event(MacroSource.FOREX_FACTORY, 216.0, actual_unit=ValueUnit.THOUSANDS),
    ]
    results = compare_macro_sources(events)
    assert results[0].status == ValidationStatus.MISSING
    assert results[0].status != ValidationStatus.MISMATCH


def test_unknown_unit_value_never_produces_false_match_either():
    events = [
        _event(MacroSource.MQL5, 999.0, actual_unit=ValueUnit.UNKNOWN),  # wildly different value
        _event(MacroSource.FOREX_FACTORY, 216.0, actual_unit=ValueUnit.THOUSANDS),
    ]
    results = compare_macro_sources(events)
    assert results[0].status == ValidationStatus.MISSING


def test_official_value_compared_against_matching_unit_source():
    """Since round 4 (Phase 3), the official comparison is its own
    result (field="actual_vs_official_latest_revised"), separate from
    the plain cross-source "actual" comparison -- see
    tests/test_validation_macro_phase3.py."""
    events = [
        _event(MacroSource.MQL5, 216.0, actual_unit=ValueUnit.THOUSANDS, official_actual=216.0, official_actual_unit=ValueUnit.THOUSANDS),
    ]
    results = compare_macro_sources(events)
    latest = next(r for r in results if r.field == "actual_vs_official_latest_revised")
    assert latest.status == ValidationStatus.MATCH
    # keyed by vintage_kind too (default LATEST_REVISED) -- see item #6
    assert latest.values == {"MQL5": 216.0, "MQL5:official:LATEST_REVISED": 216.0}


def test_latest_revised_and_asof_official_values_do_not_collide():
    """Regression: two DIFFERENT vintage kinds of official value for the
    same period (today's revised number vs. a specific historical as-of
    snapshot) must both be visible, not silently overwrite each other --
    and (since Phase 3) never merged into ONE numerical comparison
    either; see tests/test_validation_macro_phase3.py."""
    events = [
        _event(MacroSource.MQL5, 216.0, actual_unit=ValueUnit.THOUSANDS),
        _event(MacroSource.FRED, None, official_actual=220.0, official_actual_unit=ValueUnit.THOUSANDS),  # LATEST_REVISED (default)
        _event(MacroSource.FRED, None, official_actual=216.0, official_actual_unit=ValueUnit.THOUSANDS).model_copy(
            update={"official_vintage_kind": "AS_OF"}
        ),
    ]
    results = compare_macro_sources(events)
    latest = next(r for r in results if r.field == "actual_vs_official_latest_revised")
    asof = next(r for r in results if r.field.startswith("actual_vs_official_asof"))
    assert latest.values["FRED:official:LATEST_REVISED"] == 220.0
    assert asof.values["FRED:official:AS_OF"] == 216.0


# -- reference_period grouping (item #12: FRED official rows join by period, not release date) --

def test_reference_period_takes_priority_over_release_date_for_grouping():
    mql5_event = _event(MacroSource.MQL5, 216.0, release_timestamp_utc=dt.datetime(2024, 2, 2, 13, 30, tzinfo=dt.timezone.utc))
    fred_event = _event(
        MacroSource.FRED, None, official_actual=216.0, official_actual_unit=ValueUnit.THOUSANDS,
        reference_period=dt.date(2024, 1, 1),  # January data, released Feb 2 -- different calendar date
    )
    mql5_with_period = mql5_event.model_copy(update={"reference_period": dt.date(2024, 1, 1)})

    results = compare_macro_sources([mql5_with_period, fred_event])
    # joined by reference_period, not release date -- both results share one reference_key
    assert {r.reference_key for r in results} == {"period:2024-01-01"}
    latest = next(r for r in results if r.field == "actual_vs_official_latest_revised")
    assert latest.status == ValidationStatus.MATCH


def test_events_without_period_or_release_timestamp_are_excluded_not_misjoined():
    quarantined = _event(MacroSource.MQL5, 216.0, release_timestamp_utc=None)
    normal = _event(MacroSource.FOREX_FACTORY, 216.0)
    results = compare_macro_sources([quarantined, normal])
    # quarantined row has nothing to group by -> excluded, leaving normal alone -> MISSING (only 1 source)
    assert len(results) == 1
    assert results[0].status == ValidationStatus.MISSING
