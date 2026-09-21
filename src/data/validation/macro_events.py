"""Calendar-anchored, vintage-aware reconciliation of macro releases (Forex Factory / MQL5 / FRED-ALFRED).

EVENT UNIVERSE. A release-window report contains exactly the calendar releases published by
Forex Factory / MQL5 whose RELEASE DATE falls in the window. FRED/ALFRED observations only ENRICH those
events; they never create standalone events (a FRED observation carries the period a value describes,
not a release time, so e.g. the 2025-09 observation is not a September release -- it is published in
October). Nothing is ever reported as MISSING_MQL5 because an official observation exists.

RELEASE DATE != REFERENCE PERIOD. `release_date` is when the calendar published the number
(2025-09-05); `reference_period` is the month it describes (2025-08). They are separate columns and are
never assumed equal. FRED is joined by (event_family, reference_period).

TWO OFFICIAL VALUES, NEVER SUBSTITUTED FOR EACH OTHER.
  official_release_vintage_value  an ALFRED "as of" value: the observation exactly as published by
                                  the vintage date. The ONLY value the calendar `actual` is validated
                                  against.
  official_latest_value           today's LATEST_REVISED series value. It contains later revisions
                                  (monthly revisions, benchmark and seasonal-factor revisions) that
                                  market participants did not see at the release, so using it as a
                                  release-time truth is look-ahead leakage. Reported for diagnostics only
                                  (official_latest_minus_release_vintage shows how much it moved).
When no valid release vintage exists the result is OFFICIAL_VINTAGE_UNAVAILABLE -- never a
VALUE_MISMATCH against the latest series.

VINTAGE RESOLUTION (no look-ahead). Among AS_OF rows for the same (family, reference_period) the vintage
date V must satisfy  release_date <= V <= release_date + max_vintage_lag_days ; the EARLIEST such V wins
("at or immediately after the release"). A V before the release cannot contain the observation
(rejected); a V later than the lag window may embed subsequent revisions (rejected as look-ahead).

UNITS are compared only when both are known and equal; nothing is rescaled or coerced. NFP is
THOUSANDS (config/event_mapping.yaml `value_unit`). ROUNDING: a calendar value is published at limited
precision, so the official release-vintage value is rounded (half-up) to the calendar's display precision
before comparison -> ROUNDING_MATCH, exact equality -> EXACT_MATCH. The rounding is applied ONLY against
the release vintage. Forex Factory vs MQL5 (both calendars) is compared exactly.

TIMESTAMPS stay UNVERIFIABLE while the MQL5 UTC time is unresolved (broker timezone unconfirmed); an
explicit `mql5_broker_timezone` in ValidationConfig resolves them for that run only.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..event_mapping import EventMapping
from ..schemas import MacroEvent, ValueUnit

# ---- overall per-event status (worst first) ----
MATCH = "MATCH"
VALUE_MISMATCH = "VALUE_MISMATCH"
TIMESTAMP_MISMATCH = "TIMESTAMP_MISMATCH"
UNIT_MISMATCH = "UNIT_MISMATCH"
SEMANTIC_MISMATCH = "SEMANTIC_MISMATCH"
UNIT_UNVERIFIED = "UNIT_UNVERIFIED"
MISSING_FF = "MISSING_FF"
MISSING_MQL5 = "MISSING_MQL5"
OFFICIAL_VINTAGE_UNAVAILABLE = "OFFICIAL_VINTAGE_UNAVAILABLE"
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
STATUS_PRIORITY = [AMBIGUOUS_MATCH, SEMANTIC_MISMATCH, UNIT_MISMATCH, VALUE_MISMATCH, TIMESTAMP_MISMATCH,
                   MISSING_MQL5, MISSING_FF, OFFICIAL_VINTAGE_UNAVAILABLE, UNIT_UNVERIFIED, MATCH]

# ---- release_actual_match_status ----
EXACT_MATCH = "EXACT_MATCH"
ROUNDING_MATCH = "ROUNDING_MATCH"
NOT_COMPARABLE = "NOT_COMPARABLE"
NO_ACTUAL = "NO_ACTUAL"

UNKNOWN = ValueUnit.UNKNOWN.value

SCOPE_FAMILIES = ("NFP", "UNEMPLOYMENT_RATE", "AVG_HOURLY_EARNINGS_MOM", "AVG_HOURLY_EARNINGS_YOY",
                  "CPI_MOM", "CPI_YOY", "CORE_CPI_MOM", "CORE_CPI_YOY")


@dataclass(frozen=True)
class FamilySemantics:
    measure: str      # MOM | YOY | LEVEL_CHANGE | RATE
    scope: str        # HEADLINE | CORE | N/A
    adjustment: str   # SA | NSA
    unit: str         # fallback when config/event_mapping.yaml declares no value_unit


SEMANTICS: Dict[str, FamilySemantics] = {
    "NFP": FamilySemantics("LEVEL_CHANGE", "N/A", "SA", "THOUSANDS"),
    "UNEMPLOYMENT_RATE": FamilySemantics("RATE", "N/A", "SA", "PERCENT"),
    "AVG_HOURLY_EARNINGS_MOM": FamilySemantics("MOM", "N/A", "SA", "PERCENT"),
    "AVG_HOURLY_EARNINGS_YOY": FamilySemantics("YOY", "N/A", "SA", "PERCENT"),
    "CPI_MOM": FamilySemantics("MOM", "HEADLINE", "SA", "PERCENT"),
    "CPI_YOY": FamilySemantics("YOY", "HEADLINE", "NSA", "PERCENT"),
    "CORE_CPI_MOM": FamilySemantics("MOM", "CORE", "SA", "PERCENT"),
    "CORE_CPI_YOY": FamilySemantics("YOY", "CORE", "NSA", "PERCENT"),
}
FRED_TRANSFORM_MEASURE = {"pct_change_1": "MOM", "pct_change_12": "YOY", "diff_1": "LEVEL_CHANGE",
                          "identity": "RATE"}
FRED_SERIES_SCOPE = {"CPIAUCSL": "HEADLINE", "CPIAUCNS": "HEADLINE", "CPILFESL": "CORE", "CPILFENS": "CORE"}


@dataclass(frozen=True)
class FredSeriesProfile:
    series_id: str
    event_family: str
    adjustment: str   # SA | NSA
    transform: str
    unit: str


def fred_profiles_from_config(config) -> Dict[Tuple[str, str], FredSeriesProfile]:
    """{(series_id, event_family): profile} from providers.fred.official_series."""
    out = {}
    for item in config.provider("fred").get("official_series", []):
        p = FredSeriesProfile(item["series_id"], item["event_family"], item.get("seasonal_adjustment", "UNKNOWN"),
                              item.get("transform", "UNKNOWN"), item.get("result_unit", "UNKNOWN"))
        out[(p.series_id, p.event_family)] = p
    return out


@dataclass(frozen=True)
class ValidationConfig:
    timestamp_tolerance_seconds: float = 60.0
    # Decimals the calendars publish per unit (CPI "0.3%", NFP "22K"). The official release-vintage value
    # is rounded half-up to max(this, the calendar value's own decimals) before the comparison.
    display_decimals: Dict[str, int] = field(default_factory=lambda: {"PERCENT": 1, "THOUSANDS": 0})
    max_vintage_lag_days: int = 3
    mql5_broker_timezone: Optional[str] = None   # explicit operator input only; never inferred
    date_fallback_days: int = 1
    families: Tuple[str, ...] = SCOPE_FAMILIES


ROW_FIELDS = [
    "canonical_event_id", "event_family", "event_name", "event_bundle",
    "release_date", "release_date_basis", "release_timestamp", "release_timestamp_source", "reference_period",
    "ff_actual", "ff_provider_forecast", "ff_previous", "ff_revised_previous",
    "mql5_actual", "mql5_previous", "mql5_revised_previous", "mql5_forecast_diagnostic",
    "mql5_previous_differs_from_revised",
    "actual_value", "actual_value_source", "actual_minus_forecast",
    "official_source", "official_series_id", "official_reference_period", "official_vintage_date",
    "official_release_vintage_value", "official_unit",
    "official_latest_value", "official_latest_vintage_date", "official_latest_minus_release_vintage",
    "release_actual_match_status", "comparison_decimals", "actual_minus_official_release_vintage",
    "timestamp_status", "timestamp_delta_seconds", "mql5_implied_server_utc_offset_hours",
    "unit_status", "ff_unit", "mql5_unit",
    "source_match_status", "issues", "notes",
]


@dataclass
class ReconciliationResult:
    rows: List[Dict[str, Any]]
    warnings: List[str]


# --------------------------------------------------------------------------- helpers
def _unit(u) -> str:
    return u.value if isinstance(u, ValueUnit) else (u or UNKNOWN)


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v


def _mql5_utc(ev: MacroEvent, cfg: ValidationConfig) -> Tuple[Optional[dt.datetime], str]:
    if ev.release_timestamp_utc is not None:
        return _aware(ev.release_timestamp_utc), "mql5.release_timestamp_utc"
    if ev.source_timestamp is not None and cfg.mql5_broker_timezone:
        from zoneinfo import ZoneInfo
        local = ev.source_timestamp.replace(tzinfo=ZoneInfo(cfg.mql5_broker_timezone))
        return local.astimezone(dt.timezone.utc), f"mql5.source_timestamp@{cfg.mql5_broker_timezone} (operator-supplied)"
    return None, "unresolved"


def _event_date(ev: MacroEvent, cfg: ValidationConfig) -> Optional[dt.date]:
    """Best release DATE for one calendar row (range filtering, matching, vintage anchoring)."""
    if ev.release_timestamp_utc is not None:
        return ev.release_timestamp_utc.date()
    if ev.source_timestamp is not None:
        src = ev.source.value if hasattr(ev.source, "value") else ev.source
        if cfg.mql5_broker_timezone and src == "MQL5":
            return _mql5_utc(ev, cfg)[0].date()
        return ev.source_timestamp.date()   # server-local calendar date (no timezone applied)
    return None


def _compatible(a: MacroEvent, b: MacroEvent, cfg: ValidationConfig) -> bool:
    if a.event_family != b.event_family:
        return False
    if a.reference_period is not None and b.reference_period is not None:
        return a.reference_period == b.reference_period
    da, db = _event_date(a, cfg), _event_date(b, cfg)
    return da is not None and db is not None and abs((da - db).days) <= cfg.date_fallback_days


def _components(nodes: List[Tuple[str, MacroEvent]], cfg: ValidationConfig) -> List[List[Tuple[str, MacroEvent]]]:
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if _compatible(nodes[i][1], nodes[j][1], cfg):
                parent[find(i)] = find(j)
    groups: Dict[int, List[Tuple[str, MacroEvent]]] = {}
    for i, n in enumerate(nodes):
        groups.setdefault(find(i), []).append(n)
    return list(groups.values())


def _fred_series(ev: MacroEvent) -> str:
    return (ev.official_source or "").split(":", 1)[-1]


def _decimals(x: float) -> int:
    exp = Decimal(repr(float(x))).normalize().as_tuple().exponent
    return max(0, -exp)


def compare_to_release_vintage(actual: float, official: float, unit: str, cfg: ValidationConfig):
    """('EXACT_MATCH'|'ROUNDING_MATCH'|'VALUE_MISMATCH', decimals_used). `official` is rounded half-up to
    the calendar's display precision; call it ONLY with a release-vintage value."""
    decimals = max(cfg.display_decimals.get(unit, 0), _decimals(actual))
    if abs(actual - official) <= 1e-9:
        return EXACT_MATCH, decimals
    quantum = Decimal(1).scaleb(-decimals)
    rounded = float(Decimal(repr(float(official))).quantize(quantum, rounding=ROUND_HALF_UP))
    return (ROUNDING_MATCH if abs(actual - rounded) <= 1e-9 else VALUE_MISMATCH), decimals


# --------------------------------------------------------------------------- vintage resolution
@dataclass
class VintageResolution:
    row: Optional[MacroEvent]
    code: str          # OK | NONE | PREDATES_RELEASE | LOOKAHEAD_REJECTED | AMBIGUOUS
    message: str = ""


def resolve_release_vintage(asof_rows: Sequence[MacroEvent], release_date: Optional[dt.date],
                            max_lag_days: int) -> VintageResolution:
    """Pick the ALFRED vintage that was available at or immediately after `release_date`, rejecting any
    vintage that could contain later information. See the module docstring."""
    rows = [r for r in asof_rows if _clean(r.official_actual) is not None and r.official_vintage_date is not None]
    if not rows:
        return VintageResolution(None, "NONE", "no ALFRED as-of row for this reference period")
    if release_date is None:
        return VintageResolution(None, "NONE", "release date unknown, cannot anchor a vintage")
    latest_ok = release_date + dt.timedelta(days=max_lag_days)
    eligible = [r for r in rows if release_date <= r.official_vintage_date <= latest_ok]
    if eligible:
        first = min(r.official_vintage_date for r in eligible)
        at_first = [r for r in eligible if r.official_vintage_date == first]
        if len({r.official_actual for r in at_first}) > 1:
            return VintageResolution(None, "AMBIGUOUS", f"conflicting as-of values for vintage {first}")
        return VintageResolution(at_first[0], "OK")
    dates = sorted({r.official_vintage_date for r in rows})
    if all(d > latest_ok for d in dates):
        return VintageResolution(None, "LOOKAHEAD_REJECTED",
                                 f"as-of vintage(s) {', '.join(map(str, dates))} are later than the release "
                                 f"{release_date} + {max_lag_days}d and may embed later revisions")
    return VintageResolution(None, "PREDATES_RELEASE",
                             f"as-of vintage(s) {', '.join(map(str, dates))} predate the release {release_date}")


# --------------------------------------------------------------------------- semantic checks
def _semantic_issues(family: str, ff, mql5, fred_rows, mapping: EventMapping,
                     profiles: Optional[Dict[Tuple[str, str], FredSeriesProfile]]) -> List[str]:
    sem = SEMANTICS.get(family)
    out: List[str] = []
    entry = mapping.by_family(family)
    labelled = [("FF", ff), ("MQL5", mql5)] + [("FRED", r) for r in fred_rows]
    for label, ev in labelled:
        if ev is not None and entry is not None and ev.indicator != entry.indicator:
            out.append(f"SEMANTIC:{label} row labeled {ev.indicator!r} is filed under {family} ({entry.indicator!r})")
    if sem is not None and profiles is not None:
        for fred in {_fred_series(r): r for r in fred_rows}.values():
            series = _fred_series(fred)
            prof = profiles.get((series, family))
            if prof is None:
                out.append(f"SEMANTIC:FRED series {series!r} has no configured profile for {family}")
            else:
                if prof.adjustment != sem.adjustment:
                    out.append(f"SEMANTIC:FRED {series} is {prof.adjustment} but {family} is conventionally {sem.adjustment}")
                measure = FRED_TRANSFORM_MEASURE.get(prof.transform)
                if measure and measure != sem.measure:
                    out.append(f"SEMANTIC:FRED {series} transform {prof.transform} yields {measure}, {family} is {sem.measure}")
            scope = FRED_SERIES_SCOPE.get(series)
            if scope and sem.scope in ("HEADLINE", "CORE") and scope != sem.scope:
                out.append(f"SEMANTIC:FRED {series} is {scope} but {family} is {sem.scope}")
    return out


# --------------------------------------------------------------------------- row building
def _blank(family: str, ref: Optional[dt.date], mapping: EventMapping) -> Dict[str, Any]:
    entry = mapping.by_family(family)
    row = {k: None for k in ROW_FIELDS}
    row.update(canonical_event_id=f"{family}:{ref.isoformat()[:7]}" if ref else f"{family}:unknown-period",
               event_family=family, event_name=entry.indicator if entry else family,
               event_bundle=entry.release_bundle if entry else None,
               reference_period=ref.isoformat() if ref else None)
    return row


def _unique(rows: List[MacroEvent]) -> Tuple[Optional[MacroEvent], Optional[str]]:
    if len(rows) > 1:
        return None, f"{len(rows)} FRED LATEST_REVISED rows for one reference period"
    return (rows[0] if rows else None), None


def _build_row(family, ff: Optional[MacroEvent], m: Optional[MacroEvent], fred_rows: List[MacroEvent],
               mapping: EventMapping, cfg: ValidationConfig, profiles, ambiguity: Optional[str]) -> Dict[str, Any]:
    ref = next((e.reference_period for e in (m, ff) if e is not None and e.reference_period), None)
    row = _blank(family, ref, mapping)
    issues: List[str] = []
    notes: List[str] = []
    flags = set()

    release_date = None
    if ff is not None or m is not None:
        ff_d = _event_date(ff, cfg) if ff is not None else None
        m_d = _event_date(m, cfg) if m is not None else None
        release_date = ff_d or m_d
        row["release_date"] = release_date.isoformat() if release_date else None
        row["release_date_basis"] = ("forex_factory.release_timestamp_utc" if ff_d
                                     else "mql5 (see release_timestamp_source)")

    if ambiguity:
        row.update(source_match_status=AMBIGUOUS_MATCH, issues=f"AMBIGUOUS:{ambiguity}",
                   notes="values left blank: candidates cannot be paired without guessing")
        return row

    sem = SEMANTICS.get(family)
    entry = mapping.by_family(family)
    expected_unit = (entry.value_unit if entry and entry.value_unit else (sem.unit if sem else None))

    # ---- raw calendar values, exactly as normalized ----
    if ff is not None:
        row.update(ff_actual=_clean(ff.actual), ff_provider_forecast=_clean(ff.provider_forecast),
                   ff_previous=_clean(ff.previous), ff_revised_previous=_clean(ff.revised_previous),
                   ff_unit=_unit(ff.actual_unit))
    if m is not None:
        row.update(mql5_actual=_clean(m.actual), mql5_previous=_clean(m.previous),
                   mql5_revised_previous=_clean(m.revised_previous),
                   mql5_forecast_diagnostic=_clean(m.provider_forecast), mql5_unit=_unit(m.actual_unit))
        if row["mql5_previous"] is not None and row["mql5_revised_previous"] is not None:
            row["mql5_previous_differs_from_revised"] = row["mql5_previous"] != row["mql5_revised_previous"]

    if ff is None:
        flags.add(MISSING_FF); issues.append("MISSING_FF:no Forex Factory row for this release")
    if m is None:
        flags.add(MISSING_MQL5); issues.append("MISSING_MQL5:no MQL5 row for this release")

    # ---- official values: release vintage (validation target) vs latest (diagnostic) ----
    asof = [r for r in fred_rows if r.official_vintage_kind == "AS_OF"]
    latest_rows = [r for r in fred_rows if r.official_vintage_kind != "AS_OF" and _clean(r.official_actual) is not None]
    latest, latest_amb = _unique(latest_rows)
    if latest_amb:
        flags.add(AMBIGUOUS_MATCH); issues.append(f"AMBIGUOUS:{latest_amb}")
    if ref is None:
        vres = VintageResolution(None, "NONE", "event has no reference_period, so no official observation can be joined")
    else:
        vres = resolve_release_vintage(asof, release_date, cfg.max_vintage_lag_days)
    vintage = vres.row
    if vres.code == "AMBIGUOUS":
        flags.add(AMBIGUOUS_MATCH); issues.append(f"AMBIGUOUS:{vres.message}")

    if latest is not None:
        row.update(official_latest_value=_clean(latest.official_actual),
                   official_latest_vintage_date=latest.official_vintage_date.isoformat() if latest.official_vintage_date else None)
        notes.append("official_latest_value is LATEST_REVISED (may include later revisions): diagnostic only, "
                     "never used for the match status")
    if vintage is not None:
        row.update(official_source=vintage.official_source, official_series_id=_fred_series(vintage),
                   official_reference_period=vintage.reference_period.isoformat() if vintage.reference_period else None,
                   official_vintage_date=vintage.official_vintage_date.isoformat(),
                   official_release_vintage_value=_clean(vintage.official_actual), official_unit=_unit(vintage.official_actual_unit))
        if latest is not None:
            row["official_latest_minus_release_vintage"] = row["official_latest_value"] - row["official_release_vintage_value"]
    else:
        src = latest or (asof[0] if asof else None)
        if src is not None:
            row.update(official_source=src.official_source, official_series_id=_fred_series(src),
                       official_reference_period=src.reference_period.isoformat() if src.reference_period else None,
                       official_unit=_unit(src.official_actual_unit))
        if vres.code != "AMBIGUOUS":
            flags.add(OFFICIAL_VINTAGE_UNAVAILABLE)
            extra = (f"; latest-revised value {row['official_latest_value']:.6g} shown as diagnostic only"
                     if latest is not None else "")
            issues.append(f"OFFICIAL_VINTAGE_UNAVAILABLE:{vres.message}{extra}")
        row["release_actual_match_status"] = OFFICIAL_VINTAGE_UNAVAILABLE

    # ---- semantics ----
    sem_issues = _semantic_issues(family, ff, m, ([vintage] if vintage else []) + ([latest] if latest else []),
                                  mapping, profiles)
    if sem_issues:
        flags.add(SEMANTIC_MISMATCH); issues.extend(sem_issues)

    # ---- units ----
    unit_state = "MATCH"
    for label, u, present in (("FF", row["ff_unit"], ff is not None and row["ff_actual"] is not None),
                              ("MQL5", row["mql5_unit"], m is not None and row["mql5_actual"] is not None),
                              ("FRED", row["official_unit"], row["official_unit"] is not None)):
        if not present:
            continue
        if u == UNKNOWN:
            unit_state = "UNVERIFIED" if unit_state == "MATCH" else unit_state
            flags.add(UNIT_UNVERIFIED)
            issues.append(f"UNIT_UNVERIFIED:{label} unit is UNKNOWN, no comparison involving it was made")
        elif expected_unit and u != expected_unit:
            unit_state = "MISMATCH"
            flags.add(UNIT_MISMATCH)
            issues.append(f"UNIT_MISMATCH:{label} unit {u} != expected {expected_unit} for {family}")
    row["unit_status"] = unit_state if (ff or m or row["official_unit"]) else None

    # ---- primary actual, forecast delta ----
    if row["mql5_actual"] is not None:
        row.update(actual_value=row["mql5_actual"], actual_value_source="MQL5"); a_unit = row["mql5_unit"]
    elif row["ff_actual"] is not None:
        row.update(actual_value=row["ff_actual"], actual_value_source="FF (MQL5 actual absent)"); a_unit = row["ff_unit"]
    else:
        a_unit = UNKNOWN
    f_unit = _unit(ff.provider_forecast_unit) if ff is not None else UNKNOWN
    if row["actual_value"] is not None and row["ff_provider_forecast"] is not None:
        if UNKNOWN in (a_unit, f_unit) or a_unit != f_unit:
            issues.append("NOT_COMPARABLE:actual vs FF forecast (unit unknown or different, no delta computed)")
        else:
            row["actual_minus_forecast"] = row["actual_value"] - row["ff_provider_forecast"]

    # ---- release actual vs official RELEASE VINTAGE ----
    if vintage is not None:
        if row["actual_value"] is None:
            row["release_actual_match_status"] = NO_ACTUAL
            issues.append("NO_ACTUAL:calendar has no actual value to validate")
        elif UNKNOWN in (a_unit, row["official_unit"]) or a_unit != row["official_unit"]:
            row["release_actual_match_status"] = NOT_COMPARABLE
            issues.append("NOT_COMPARABLE:actual vs official release vintage (unit unknown or different, no delta computed)")
        else:
            status, decimals = compare_to_release_vintage(row["actual_value"], row["official_release_vintage_value"], a_unit, cfg)
            row.update(release_actual_match_status=status, comparison_decimals=decimals,
                       actual_minus_official_release_vintage=row["actual_value"] - row["official_release_vintage_value"])
            if status == ROUNDING_MATCH:
                issues.append(f"ROUNDING_MATCH:official release vintage {row['official_release_vintage_value']:.6g} "
                              f"rounds to {row['actual_value']} at {decimals} decimal(s)")
            elif status == VALUE_MISMATCH:
                flags.add(VALUE_MISMATCH)
                issues.append(f"VALUE_MISMATCH:actual {row['actual_value']:.6g} vs official release vintage "
                              f"{row['official_release_vintage_value']:.6g} (vintage {row['official_vintage_date']})")

    # ---- FF vs MQL5 cross-checks: previous and revised_previous stay separate fields ----
    if ff is not None and m is not None:
        for name, fa, ma, uf, um in (("actual", row["ff_actual"], row["mql5_actual"], row["ff_unit"], row["mql5_unit"]),
                                     ("previous", row["ff_previous"], row["mql5_previous"], _unit(ff.previous_unit), _unit(m.previous_unit)),
                                     ("revised_previous", row["ff_revised_previous"], row["mql5_revised_previous"],
                                      _unit(ff.previous_unit), _unit(m.previous_unit))):
            if fa is None or ma is None:
                continue
            if UNKNOWN in (uf, um) or uf != um:
                issues.append(f"NOT_COMPARABLE:FF vs MQL5 {name} (unit unknown or different, no delta computed)")
            elif abs(fa - ma) > 1e-9:
                flags.add(VALUE_MISMATCH)
                issues.append(f"VALUE_MISMATCH:FF {name} {fa:.6g} vs MQL5 {ma:.6g}")
        if row["ff_revised_previous"] is None and row["mql5_revised_previous"] is not None:
            notes.append("revised_previous only present in MQL5")

    # ---- timestamps ----
    ff_utc = _aware(ff.release_timestamp_utc) if ff is not None and ff.release_timestamp_utc is not None else None
    m_utc, m_basis = _mql5_utc(m, cfg) if m is not None else (None, "n/a")
    if ff_utc is not None and m_utc is not None:
        delta = (m_utc - ff_utc).total_seconds()
        row["timestamp_delta_seconds"] = delta
        row["timestamp_status"] = "MATCH" if abs(delta) <= cfg.timestamp_tolerance_seconds else "MISMATCH"
        if row["timestamp_status"] == "MISMATCH":
            flags.add(TIMESTAMP_MISMATCH)
            issues.append(f"TIMESTAMP_MISMATCH:MQL5 {m_utc.isoformat()} vs FF {ff_utc.isoformat()} "
                          f"(delta {delta:+.0f}s, tolerance {cfg.timestamp_tolerance_seconds:.0f}s)")
    elif ff is not None and m is not None:
        row["timestamp_status"] = "UNVERIFIABLE"
        issues.append("TIMESTAMP_UNVERIFIABLE:" + ("MQL5 UTC timestamp unresolved (broker timezone not confirmed)"
                                                    if m_utc is None else "Forex Factory UTC timestamp unresolved"))
        if m.source_timestamp is not None and ff_utc is not None:
            row["mql5_implied_server_utc_offset_hours"] = round(
                (m.source_timestamp - ff_utc.replace(tzinfo=None)).total_seconds() / 3600, 4)
            notes.append("mql5_implied_server_utc_offset_hours is a DIAGNOSTIC (MQL5 server time minus FF UTC), "
                         "not a confirmation of the broker timezone")
    else:
        row["timestamp_status"] = "N/A"
    if m_utc is not None:
        row.update(release_timestamp=m_utc.isoformat(), release_timestamp_source=m_basis)
    elif ff_utc is not None:
        row.update(release_timestamp=ff_utc.isoformat(), release_timestamp_source="ff.release_timestamp_utc (MQL5 UTC unresolved)")

    status = next(s for s in STATUS_PRIORITY if s == MATCH or s in flags)
    row.update(source_match_status=status, issues=" | ".join(issues), notes=" | ".join(notes))
    return row


# --------------------------------------------------------------------------- entry point
def reconcile(ff_events: Sequence[MacroEvent], mql5_events: Sequence[MacroEvent], fred_events: Sequence[MacroEvent],
              start: dt.date, end: dt.date, mapping: EventMapping, cfg: ValidationConfig = ValidationConfig(),
              fred_profiles: Optional[Dict[Tuple[str, str], FredSeriesProfile]] = None) -> ReconciliationResult:
    """Rows = calendar releases (Forex Factory / MQL5) with release date in [start, end]; FRED enriches only."""
    warnings: List[str] = []
    fams = set(cfg.families)

    def in_window(events, label):
        kept, undated = [], 0
        for e in events:
            if e.event_family not in fams:
                continue
            d = _event_date(e, cfg)
            if d is None:
                # Could still belong to the window if its reference period is unknown or plausibly published
                # within it (data is released 0-2 months after the period it describes).
                ref = e.reference_period
                if ref is None or (start - dt.timedelta(days=62) <= ref <= end):
                    undated += 1
                continue
            if start <= d <= end:
                kept.append(e)
        if undated:
            warnings.append(f"{undated} {label} row(s) in scope families have no usable release date and were excluded")
        return kept

    ff = in_window(ff_events, "Forex Factory")
    mq = in_window(mql5_events, "MQL5")
    fred_by_key: Dict[Tuple[str, Optional[dt.date]], List[MacroEvent]] = {}
    for e in fred_events:
        if e.event_family in fams:
            fred_by_key.setdefault((e.event_family, e.reference_period), []).append(e)

    rows: List[Dict[str, Any]] = []
    used = set()
    for comp in _components([("FF", e) for e in ff] + [("MQL5", e) for e in mq], cfg):
        family = comp[0][1].event_family
        f_list = [e for s, e in comp if s == "FF"]
        m_list = [e for s, e in comp if s == "MQL5"]
        if len(f_list) > 1 or len(m_list) > 1:
            amb = f"{len(f_list)} Forex Factory and {len(m_list)} MQL5 candidate rows for {family}"
            row = _build_row(family, None, None, [], mapping, cfg, fred_profiles, amb)
            ref = next((e.reference_period for _, e in comp if e.reference_period), None)
            row["reference_period"] = ref.isoformat() if ref else None
            row["canonical_event_id"] = f"{family}:{ref.isoformat()[:7]}" if ref else f"{family}:unknown-period"
            dates = [d for d in (_event_date(e, cfg) for _, e in comp) if d]
            row["release_date"] = min(dates).isoformat() if dates else None
            rows.append(row)
            continue
        f, m = (f_list or [None])[0], (m_list or [None])[0]
        ref = next((e.reference_period for e in (m, f) if e is not None and e.reference_period), None)
        fred_rows = fred_by_key.get((family, ref), []) if ref else []
        used.add((family, ref))
        rows.append(_build_row(family, f, m, fred_rows, mapping, cfg, fred_profiles, None))

    unused = sum(len(v) for k, v in fred_by_key.items() if k not in used)
    if unused:
        warnings.append(f"{unused} FRED row(s) for reference periods with no calendar release in the window were "
                        "not used (other reference periods, e.g. transform lookback or observations released after the "
                        "window; FRED enriches calendar releases and never creates release events)")
    rows.sort(key=lambda r: (r["release_timestamp"] or r["release_date"] or "9999", r["event_family"]))
    return ReconciliationResult(rows, warnings)


def summarize(rows: Sequence[Dict[str, Any]], key: str = "source_match_status") -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return counts
