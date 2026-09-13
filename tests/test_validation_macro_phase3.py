"""Phase 3 regression tests (fourth review): `compare_macro_sources`
already distinguished LATEST_REVISED from AS_OF official values by dict
key, but still combined them into ONE numerical comparison per group --
calendar actual=100, a matching historical AS_OF=100, and a
since-revised LATEST_REVISED=120 for the SAME period produced a single
MISMATCH (spread 100..120), even though the calendar's real-time print
was actually correct at the time it was made. Multiple AS_OF vintage
dates for the same period also shared one dict key
("{source}:official:AS_OF") and silently overwrote each other.

The fix separates every group's comparisons into distinct results:
  - field="actual"                              -- cross-source
    real-time comparison (mql5 vs forex_factory etc), unaffected by any
    official/vintage data at all.
  - field="actual_vs_official_asof:{date}"       -- one per distinct
    AS_OF vintage date, comparing actual(s) against THAT snapshot only.
  - field="actual_vs_official_latest_revised"    -- diagnostic-only,
    clearly separate, so a later revision can never retroactively turn
    a matching historical (AS_OF) comparison into a MISMATCH.
  - field="actual_vs_official_asof" (no date)    -- explicit
    "unavailable" MISSING result when an actual exists but no AS_OF
    vintage data does, so historical alignment being impossible is
    visible rather than silently absent.
"""
import datetime as dt

from src.data.schemas import MacroEvent, MacroSource, ValidationStatus, ValueUnit
from src.data.validation.macro import compare_macro_sources


def _actual_event(source, actual, family="NFP", day=5, actual_unit=ValueUnit.THOUSANDS):
    return MacroEvent(
        event_id=f"{source}:{family}:{day}:actual",
        event_family=family,
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=dt.datetime(2024, 1, day, 13, 30, tzinfo=dt.timezone.utc),
        actual=actual,
        actual_unit=actual_unit,
        source=source,
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )


def _official_event(
    official_actual, vintage_kind, vintage_date, family="NFP", day=5,
    official_actual_unit=ValueUnit.THOUSANDS, suffix="",
):
    return MacroEvent(
        event_id=f"FRED:{family}:{day}:official:{vintage_kind}:{vintage_date}{suffix}",
        event_family=family,
        indicator="Nonfarm Payrolls",
        release_timestamp_utc=dt.datetime(2024, 1, day, 13, 30, tzinfo=dt.timezone.utc),
        official_actual=official_actual,
        official_actual_unit=official_actual_unit,
        official_vintage_kind=vintage_kind,
        official_vintage_date=vintage_date,
        source=MacroSource.FRED,
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )


def _field_result(results, field_prefix):
    matches = [r for r in results if r.field.startswith(field_prefix)]
    assert matches, f"no result with field starting {field_prefix!r} among {[r.field for r in results]}"
    return matches


def test_matching_historical_asof_is_not_dragged_into_mismatch_by_later_revision():
    """The exact named bug: actual=100, AS_OF=100 (matches), but
    LATEST_REVISED=120 (later corrected). Must NOT produce one combined
    MISMATCH -- the AS_OF comparison must independently show MATCH."""
    events = [
        _actual_event(MacroSource.MQL5, 100.0),
        _official_event(100.0, "AS_OF", dt.date(2024, 1, 6)),
        _official_event(120.0, "LATEST_REVISED", dt.date(2024, 6, 1)),
    ]
    results = compare_macro_sources(events)

    asof_results = _field_result(results, "actual_vs_official_asof:2024-01-06")
    assert len(asof_results) == 1
    assert asof_results[0].status == ValidationStatus.MATCH

    latest_results = _field_result(results, "actual_vs_official_latest_revised")
    assert len(latest_results) == 1
    # The later revision legitimately differs from the original print --
    # that's real and worth surfacing, but as its OWN diagnostic result,
    # never merged with (or capable of overturning) the AS_OF verdict.
    assert latest_results[0].status == ValidationStatus.MISMATCH

    # No result should ever combine both official values together with
    # the actual into one three-way comparison.
    for r in results:
        assert not ("AS_OF" in r.field and "LATEST_REVISED" in r.field)


def test_multiple_as_of_vintage_dates_preserved_without_overwrite():
    """Regression: two different AS_OF snapshots for the same period
    (e.g. re-queried on two different historical dates) must both
    remain visible -- not silently collapse to whichever was processed
    last."""
    events = [
        _actual_event(MacroSource.MQL5, 100.0),
        _official_event(100.0, "AS_OF", dt.date(2024, 1, 6), suffix=":v1"),
        _official_event(105.0, "AS_OF", dt.date(2024, 2, 1), suffix=":v2"),
    ]
    results = compare_macro_sources(events)

    v1 = _field_result(results, "actual_vs_official_asof:2024-01-06")
    v2 = _field_result(results, "actual_vs_official_asof:2024-02-01")
    assert v1[0].status == ValidationStatus.MATCH
    assert v2[0].status == ValidationStatus.MISMATCH  # 100 vs 105, beyond tolerance


def test_actual_vs_official_asof_result_identity_includes_the_as_of_date():
    events = [
        _actual_event(MacroSource.MQL5, 100.0),
        _official_event(100.0, "AS_OF", dt.date(2024, 3, 15)),
    ]
    results = compare_macro_sources(events)
    asof_results = _field_result(results, "actual_vs_official_asof")
    assert asof_results[0].field == "actual_vs_official_asof:2024-03-15"


def test_actual_present_but_no_asof_data_reports_explicit_unavailable():
    """An actual (real-time) value exists, and there's a LATEST_REVISED
    official value, but NO AS_OF vintage was ever fetched for this
    period -- point-in-time validation is simply not possible. Must be
    reported explicitly as unavailable, not silently omitted (which
    would look identical to "nothing to compare" rather than "we
    couldn't do the comparison we'd like to have done")."""
    events = [
        _actual_event(MacroSource.MQL5, 100.0),
        _official_event(120.0, "LATEST_REVISED", dt.date(2024, 6, 1)),
    ]
    results = compare_macro_sources(events)
    asof_results = _field_result(results, "actual_vs_official_asof")
    assert asof_results[0].field == "actual_vs_official_asof"  # no date suffix -- none available
    assert asof_results[0].status == ValidationStatus.MISSING
    assert "unavailable" in (asof_results[0].note or "").lower()


def test_asof_and_latest_revised_from_same_provider_never_compared_to_each_other():
    """Regression: with NO actual (real-time) value present at all, an
    AS_OF and a LATEST_REVISED official reading from the SAME provider
    must never be treated as two independent corroborating sources and
    numerically compared against each other."""
    events = [
        _official_event(100.0, "AS_OF", dt.date(2024, 1, 6)),
        _official_event(120.0, "LATEST_REVISED", dt.date(2024, 6, 1)),
    ]
    results = compare_macro_sources(events)
    for r in results:
        sources_compared = set(r.values.keys())
        has_asof = any("AS_OF" in s for s in sources_compared)
        has_latest = any("LATEST_REVISED" in s for s in sources_compared)
        assert not (has_asof and has_latest), (r.field, r.values)


def test_cross_source_actual_comparison_unaffected_by_official_vintage_split():
    """Non-regression: the plain cross-source (mql5 vs forex_factory)
    `actual` comparison must remain a single, ordinary result,
    unaffected by whatever official/vintage data also exists for the
    same period."""
    events = [
        _actual_event(MacroSource.MQL5, 216.0),
        _actual_event(MacroSource.FOREX_FACTORY, 216.0),
        _official_event(216.0, "AS_OF", dt.date(2024, 1, 6)),
        _official_event(220.0, "LATEST_REVISED", dt.date(2024, 6, 1)),
    ]
    results = compare_macro_sources(events)
    actual_results = [r for r in results if r.field == "actual"]
    assert len(actual_results) == 1
    assert actual_results[0].status == ValidationStatus.MATCH
    assert set(actual_results[0].values.keys()) == {"MQL5", "FOREX_FACTORY"}
