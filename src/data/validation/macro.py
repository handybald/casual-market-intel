"""Cross-source macro validation.

Compares `actual` across MQL5, Forex Factory, and official (FRED) rows
for the same canonical event and flags MATCH / ROUNDING_DIFFERENCE /
MISMATCH / MISSING. Mismatches are never silently discarded -- they
come back as results the caller must look at (e.g.
scripts/validate_data.py prints them).

Grouping key: prefer `reference_period` (the calendar period a value
DESCRIBES, e.g. "January 2024 CPI") when available -- this is the only
reliable join key for FRED-derived official rows, whose observation
date is NOT a release timestamp (see normalize/fred.py). Rows with
neither a reference_period nor a resolved release_timestamp_utc (i.e.
fully quarantined rows) cannot be grouped at all and are excluded, not
silently mis-joined.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..schemas import MacroEvent, MacroValidationResult, ValidationStatus, ValueUnit

logger = logging.getLogger(__name__)

DEFAULT_ROUNDING_TOLERANCE = 0.05


def _unit_str(unit) -> str:
    return unit.value if isinstance(unit, ValueUnit) else (unit or ValueUnit.UNKNOWN.value)


def _source_str(source) -> str:
    return source.value if hasattr(source, "value") else str(source)


def _classify(
    values_by_source: Dict[str, Tuple[float, str]], tolerance: float
) -> Tuple[ValidationStatus, Optional[str]]:
    present = {k: v for k, v in values_by_source.items() if v[0] is not None}

    # A value whose unit is UNKNOWN cannot be safely compared to anything
    # numerically, regardless of what unit the other side has -- exclude
    # it from the comparable set entirely. Excluding it only from the
    # "differing units" check (leaving it in the actual numeric
    # comparison below) was the bug: an UNKNOWN-unit value next to a
    # single known-unit value passed the "only one distinct known unit"
    # check and then got compared as if the units matched, which could
    # produce a false MISMATCH (or false MATCH) between e.g. an MQL5 JOB
    # count with an unconfirmed scale and a FRED THOUSANDS-denominated
    # value.
    comparable = {k: v for k, v in present.items() if v[1] and v[1] != ValueUnit.UNKNOWN.value}
    unknown_unit_sources = sorted(set(present) - set(comparable))

    if len(comparable) < 2:
        note = None
        if unknown_unit_sources and len(present) >= 2:
            note = f"cannot compare: unit unknown/ambiguous for {unknown_unit_sources}"
        return ValidationStatus.MISSING, note

    known_units = {u for _, u in comparable.values()}
    if len(known_units) > 1:
        return ValidationStatus.MISSING, f"cannot compare: differing units {sorted(known_units)}"

    vals = [v for v, _ in comparable.values()]
    spread = max(vals) - min(vals)
    if spread == 0:
        return ValidationStatus.MATCH, None
    if spread <= tolerance:
        return ValidationStatus.ROUNDING_DIFFERENCE, None
    return ValidationStatus.MISMATCH, None


def _group_key(event: MacroEvent) -> Optional[Tuple[str, str]]:
    if event.reference_period is not None:
        return (event.event_family, f"period:{event.reference_period.isoformat()}")
    if event.release_timestamp_utc is not None:
        return (event.event_family, f"release:{event.release_timestamp_utc.date().isoformat()}")
    return None


def compare_macro_sources(
    events: List[MacroEvent],
    tolerance: float = DEFAULT_ROUNDING_TOLERANCE,
) -> List[MacroValidationResult]:
    """For each (event_family, reference_key) group, produce SEPARATE
    comparison results rather than one combined number:

      field="actual"                            cross-source real-time
                                                  comparison only (e.g.
                                                  MQL5 vs Forex Factory)
                                                  -- never affected by
                                                  official/vintage data.
      field="actual_vs_official_asof:{date}"     actual(s) vs ONE
                                                  specific official AS_OF
                                                  vintage. One result PER
                                                  distinct as-of date --
                                                  never merged together,
                                                  so multiple historical
                                                  snapshots are all
                                                  preserved. `{date}` is
                                                  the DAY-level
                                                  `official_vintage_date`
                                                  (ALFRED vintages are
                                                  day-granular; this is
                                                  never conflated with a
                                                  release's own exact
                                                  intraday timestamp).
      field="actual_vs_official_asof"            no AS_OF vintage data
      (no date suffix)                           exists for this period
                                                  at all, even though an
                                                  actual is present --
                                                  reported explicitly as
                                                  MISSING/"unavailable"
                                                  rather than silently
                                                  omitted, so a caller
                                                  can tell "point-in-time
                                                  validation was
                                                  attempted but
                                                  impossible" apart from
                                                  "nothing to report".
      field="actual_vs_official_latest_revised"  actual(s) vs the
                                                  official LATEST_REVISED
                                                  reading -- DIAGNOSTIC
                                                  ONLY. A later revision
                                                  changing this must
                                                  never retroactively
                                                  turn a matching AS_OF
                                                  result into a mismatch,
                                                  since the two live in
                                                  entirely separate
                                                  results.

    An AS_OF and a LATEST_REVISED official reading are never placed in
    the same comparison, even when no `actual` is present at all --
    two vintages of the SAME underlying provider are not independent
    corroborating sources, so treating a difference between them as a
    MATCH/MISMATCH would be meaningless.
    """
    groups: Dict[Tuple[str, str], List[MacroEvent]] = defaultdict(list)
    excluded = 0
    for e in events:
        key = _group_key(e)
        if key is None:
            excluded += 1
            continue
        groups[key].append(e)

    if excluded:
        logger.warning(
            "[macro validation] excluded %d event(s) with neither a reference_period "
            "nor a resolved release_timestamp_utc -- cannot be joined across sources",
            excluded,
        )

    results: List[MacroValidationResult] = []
    for (family, reference_key), group_events in sorted(groups.items(), key=lambda kv: kv[0]):
        actual_by_source: Dict[str, Tuple[float, str]] = {}
        # {vintage_date_iso_or_None: {source_key: (value, unit)}}, kept
        # separate per vintage_kind so distinct AS_OF dates never collide.
        asof_by_date: Dict[str, Dict[str, Tuple[float, str]]] = defaultdict(dict)
        latest_revised: Dict[str, Tuple[float, str]] = {}

        for e in group_events:
            source = _source_str(e.source)
            if e.actual is not None:
                unit = _unit_str(e.actual_unit)
                actual_by_source[source] = (e.actual, unit)
            if e.official_actual is not None:
                unit = _unit_str(e.official_actual_unit)
                key = f"{source}:official:{e.official_vintage_kind}"
                if e.official_vintage_kind == "AS_OF":
                    vintage_label = e.official_vintage_date.isoformat() if e.official_vintage_date else "unknown"
                    asof_by_date[vintage_label][key] = (e.official_actual, unit)
                else:
                    latest_revised[key] = (e.official_actual, unit)

        # -- cross-source "actual" comparison, unaffected by official data --
        status, note = _classify(actual_by_source, tolerance)
        results.append(
            MacroValidationResult(
                event_family=family, reference_key=reference_key, field="actual",
                status=status,
                values={k: v[0] for k, v in actual_by_source.items()},
                units={k: v[1] for k, v in actual_by_source.items()},
                note=note,
            )
        )

        # -- one result PER distinct AS_OF vintage date --
        if asof_by_date:
            for vintage_label, official_values in sorted(asof_by_date.items()):
                combined = dict(actual_by_source)
                combined.update(official_values)
                status, note = _classify(combined, tolerance)
                results.append(
                    MacroValidationResult(
                        event_family=family, reference_key=reference_key,
                        field=f"actual_vs_official_asof:{vintage_label}",
                        status=status,
                        values={k: v[0] for k, v in combined.items()},
                        units={k: v[1] for k, v in combined.items()},
                        note=note,
                    )
                )
        elif actual_by_source and latest_revised:
            # Official validation IS relevant for this indicator (a
            # LATEST_REVISED reading exists) and an actual exists, but no
            # AS_OF vintage was ever fetched for this period -- explicit
            # "unavailable", not silent omission. When there is no
            # official data at all for this family (neither AS_OF nor
            # LATEST_REVISED), point-in-time validation was never
            # relevant in the first place, so nothing extra is reported.
            results.append(
                MacroValidationResult(
                    event_family=family, reference_key=reference_key,
                    field="actual_vs_official_asof",
                    status=ValidationStatus.MISSING,
                    values={k: v[0] for k, v in actual_by_source.items()},
                    units={k: v[1] for k, v in actual_by_source.items()},
                    note="official AS_OF vintage unavailable for this reference period -- "
                         "point-in-time validation not possible",
                )
            )

        # -- LATEST_REVISED: diagnostic-only, always its own result --
        if latest_revised:
            combined = dict(actual_by_source)
            combined.update(latest_revised)
            status, note = _classify(combined, tolerance)
            results.append(
                MacroValidationResult(
                    event_family=family, reference_key=reference_key,
                    field="actual_vs_official_latest_revised",
                    status=status,
                    values={k: v[0] for k, v in combined.items()},
                    units={k: v[1] for k, v in combined.items()},
                    note=note,
                )
            )

    return results


def summarize(results: List[MacroValidationResult]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for r in results:
        status = r.status if isinstance(r.status, str) else r.status.value
        counts[status] += 1
    return dict(counts)
