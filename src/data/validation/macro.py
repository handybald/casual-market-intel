"""Cross-source macro validation.

Compares `actual` (and other overlapping fields) across MQL5, Forex
Factory, and official (FRED) sources for the same canonical event and
flags MATCH / ROUNDING_DIFFERENCE / MISMATCH / MISSING. Mismatches are
never silently discarded -- they come back as results the caller must
look at (e.g. scripts/validate_data.py prints them).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from ..schemas import MacroEvent, MacroValidationResult, ValidationStatus

DEFAULT_ROUNDING_TOLERANCE = 0.05


def _classify(values: Dict[str, float], tolerance: float) -> ValidationStatus:
    present = {k: v for k, v in values.items() if v is not None}
    if len(present) < 2:
        return ValidationStatus.MISSING
    vals = list(present.values())
    if max(vals) - min(vals) == 0:
        return ValidationStatus.MATCH
    if max(vals) - min(vals) <= tolerance:
        return ValidationStatus.ROUNDING_DIFFERENCE
    return ValidationStatus.MISMATCH


def compare_macro_sources(
    events: List[MacroEvent],
    tolerance: float = DEFAULT_ROUNDING_TOLERANCE,
) -> List[MacroValidationResult]:
    """Group events by (event_family, release date) and compare `actual`
    across whichever sources reported that (event_family, date).

    Grouped by calendar date rather than exact timestamp because source
    timestamps are not all equally trustworthy (see
    MacroEvent.timestamp_is_trustworthy) -- date-level grouping is the
    honest granularity we can currently guarantee alignment at.
    """
    groups: Dict[tuple, List[MacroEvent]] = defaultdict(list)
    for e in events:
        key = (e.event_family, e.release_timestamp_utc.date())
        groups[key].append(e)

    results: List[MacroValidationResult] = []
    for (family, date), group_events in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        actual_by_source: Dict[str, float] = {}
        for e in group_events:
            source = e.source if isinstance(e.source, str) else e.source.value
            if e.actual is not None:
                actual_by_source[source] = e.actual
            if e.official_actual is not None:
                actual_by_source[f"{source}:official"] = e.official_actual

        status = _classify(actual_by_source, tolerance)
        results.append(
            MacroValidationResult(
                event_family=family,
                release_timestamp_utc=group_events[0].release_timestamp_utc,
                field="actual",
                status=status,
                values=actual_by_source,
            )
        )

    return results


def summarize(results: List[MacroValidationResult]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for r in results:
        status = r.status if isinstance(r.status, str) else r.status.value
        counts[status] += 1
    return dict(counts)
