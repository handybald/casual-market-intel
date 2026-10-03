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

PRIOR PERIOD. For a release at time t about reference period R, `previous` describes R-1 as first
published at the PREVIOUS release of the same indicator, and `revised_previous` describes R-1 as revised
in THIS release. Both are validated against ALFRED vintages of R-1: `previous` against the vintage of the
previous release date (taken from the calendar's own prior release of the same family), and
`revised_previous` against the vintage of the current release date. Latest-revised values are never used,
and the original MQL5 previous / revised_previous values are never overwritten.

TIMEZONES. MQL5 timestamps are server-local times. They are UNVERIFIABLE until a broker timezone is
supplied explicitly (`mql5_broker_timezone`; never inferred, never written to config). With a candidate
timezone, every MQL5 time is converted (DST-aware) and compared with the TRUSTED (CONFIRMED) Forex Factory
UTC time of the same release; `timezone_validation` aggregates the per-release deltas. Only when the
aggregate is MATCH over at least `min_tz_evidence_releases` distinct releases may MQL5 times be used as
trusted release timestamps (`trusted_release_timestamp_utc`); a CONFIRMED Forex Factory time always wins.
The evidence must also be able to TELL A DST ZONE FROM A FIXED OFFSET: if the candidate zone changes its UTC
offset during the evidence years, the evidence must contain releases under at least two different offsets.
(Real example: Europe/Helsinki and a fixed UTC+3 both fit September 2025 perfectly, but over 2019-2020 the
MQL5 export matched fixed UTC+3 on all 44 releases and Helsinki on only 28 -- the export does not follow
Helsinki's winter time.) A wider `timezone_evidence` block computed over another window may be supplied.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..event_mapping import EventMapping
from ..schemas import MacroEvent, TimestampQuality, ValueUnit

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
    min_tz_evidence_releases: int = 2   # distinct trusted releases needed before a timezone counts as validated
    families: Tuple[str, ...] = SCOPE_FAMILIES


ROW_FIELDS = [
    "canonical_event_id", "event_family", "event_name", "event_bundle",
    "event_id", "ff_event_id", "mql5_event_id",
    "release_date", "release_date_basis", "release_timestamp", "release_timestamp_source", "reference_period",
    "ff_release_timestamp_utc", "mql5_raw_timestamp", "candidate_broker_timezone", "mql5_converted_utc",
    "timezone_validation_status", "trusted_release_timestamp_utc", "trusted_release_timestamp_basis",
    "ff_actual", "ff_provider_forecast", "ff_previous", "ff_revised_previous",
    "mql5_actual", "mql5_previous", "mql5_revised_previous", "mql5_forecast_diagnostic",
    "mql5_previous_differs_from_revised",
    "actual_value", "actual_value_source", "actual_minus_forecast",
    "official_source", "official_series_id", "official_reference_period", "official_vintage_date",
    "official_release_vintage_value", "official_unit",
    "official_latest_value", "official_latest_vintage_date", "official_latest_minus_release_vintage",
    "release_actual_match_status", "comparison_decimals", "actual_minus_official_release_vintage",
    "previous_reference_period", "previous_prior_release_date",
    "previous_official_release_vintage", "previous_official_vintage_date", "previous_validation_status",
    "revised_previous_official_release_vintage", "revised_previous_official_vintage_date",
    "revised_previous_validation_status", "official_prior_period_revision",
    "timestamp_status", "timestamp_delta_seconds", "mql5_implied_server_utc_offset_hours",
    "unit_status", "ff_unit", "mql5_unit",
    "source_match_status", "issues", "notes",
]


@dataclass
class ReconciliationResult:
    rows: List[Dict[str, Any]]
    warnings: List[str]
    timezone_validation: Dict[str, Any] = field(default_factory=dict)
    timezone_evidence: Optional[Dict[str, Any]] = None      # block from a separate evidence window, if supplied


# --------------------------------------------------------------------------- helpers
def _unit(u) -> str:
    return u.value if isinstance(u, ValueUnit) else (u or UNKNOWN)


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v


def _mql5_utc(ev: MacroEvent, cfg: ValidationConfig,
              reference_utc: Optional[dt.datetime] = None) -> Tuple[Optional[dt.datetime], str]:
    if ev.release_timestamp_utc is not None:
        return _aware(ev.release_timestamp_utc), "mql5.release_timestamp_utc"
    if ev.source_timestamp is not None and cfg.mql5_broker_timezone:
        utc, note = convert_broker_local_to_utc(ev.source_timestamp, cfg.mql5_broker_timezone, reference_utc)
        basis = f"mql5.source_timestamp@{cfg.mql5_broker_timezone} (operator-supplied)"
        return utc, basis + (f"; {note}" if note else "")
    return None, "unresolved"


def _ff_trusted_utc(ff: Optional[MacroEvent]) -> Optional[dt.datetime]:
    """Forex Factory UTC time, only when the row's timestamp_quality is CONFIRMED."""
    if ff is None or ff.release_timestamp_utc is None:
        return None
    quality = ff.timestamp_quality.value if hasattr(ff.timestamp_quality, "value") else ff.timestamp_quality
    return _aware(ff.release_timestamp_utc) if quality == TimestampQuality.CONFIRMED.value else None


def convert_broker_local_to_utc(naive: dt.datetime, tz_name: str,
                                reference_utc: Optional[dt.datetime] = None) -> Tuple[dt.datetime, str]:
    """Server-local naive time -> UTC using an IANA zone (DST-aware). Returns (utc, note).

    A local time inside the autumn fall-back hour is ambiguous (two UTC instants): the instant closest to
    `reference_utc` is chosen and the note says so. A spring-forward-gap time does not exist locally; it is
    converted with the pre-transition offset and flagged."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    a = naive.replace(tzinfo=tz, fold=0).astimezone(dt.timezone.utc)
    b = naive.replace(tzinfo=tz, fold=1).astimezone(dt.timezone.utc)
    if a != b:
        if a.astimezone(tz).replace(tzinfo=None) == naive and b.astimezone(tz).replace(tzinfo=None) == naive:
            pick = min((a, b), key=lambda u: abs((u - reference_utc).total_seconds())) if reference_utc else a
            return pick, "DST-ambiguous local time (fall-back hour)"
        return a, "nonexistent local time (DST gap)"
    return a, ""


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


def _display_round(value: float, unit: str, cfg: ValidationConfig) -> float:
    """Round half-up to the calendar display precision of `unit` (used to compare two OFFICIAL values)."""
    quantum = Decimal(1).scaleb(-cfg.display_decimals.get(unit, 0))
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


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
    late = [d for d in dates if d > latest_ok]
    early = [d for d in dates if d < release_date]
    parts = []
    if early:
        parts.append(f"as-of vintage(s) {', '.join(map(str, early))} predate the release {release_date}")
    if late:
        parts.append(f"as-of vintage(s) {', '.join(map(str, late))} are later than the release {release_date} + "
                     f"{max_lag_days}d and may embed later revisions (look-ahead)")
    return VintageResolution(None, "LOOKAHEAD_REJECTED" if late else "PREDATES_RELEASE", "; ".join(parts))


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


def _prev_month(d: dt.date) -> dt.date:
    return dt.date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1)


def _validate_prior(value, unit, vintage: Optional[MacroEvent], vres: "VintageResolution", cfg, flags, issues, label):
    """Status for previous / revised_previous vs an official prior-period vintage. `value` is the calendar's
    (MQL5) number; the official side is a release-time vintage, never a latest-revised value."""
    if value is None:
        return "NOT_APPLICABLE"
    if vintage is None:
        flags.add(OFFICIAL_VINTAGE_UNAVAILABLE)
        issues.append(f"OFFICIAL_VINTAGE_UNAVAILABLE:{label} {vres.message}")
        return OFFICIAL_VINTAGE_UNAVAILABLE
    ou = _unit(vintage.official_actual_unit)
    if UNKNOWN in (unit, ou) or unit != ou:
        flags.add(UNIT_UNVERIFIED)
        issues.append(f"UNIT_UNVERIFIED:{label} unit {unit} vs official {ou}, no comparison made")
        return UNIT_UNVERIFIED
    status, decimals = compare_to_release_vintage(value, vintage.official_actual, unit, cfg)
    if status == VALUE_MISMATCH:
        flags.add(VALUE_MISMATCH)
        issues.append(f"VALUE_MISMATCH:{label} {value:.6g} vs official vintage {vintage.official_vintage_date} "
                      f"value {vintage.official_actual:.6g}")
    elif status == ROUNDING_MATCH:
        issues.append(f"ROUNDING_MATCH:{label} official vintage {vintage.official_actual:.6g} rounds to {value} "
                      f"at {decimals} decimal(s)")
    return status


def _build_row(family, ff: Optional[MacroEvent], m: Optional[MacroEvent], fred_rows: List[MacroEvent],
               mapping: EventMapping, cfg: ValidationConfig, profiles, ambiguity: Optional[str],
               prior_fred_rows: Optional[List[MacroEvent]] = None,
               prior_release_date: Optional[dt.date] = None) -> Dict[str, Any]:
    ref = next((e.reference_period for e in (m, ff) if e is not None and e.reference_period), None)
    row = _blank(family, ref, mapping)
    row["ff_event_id"] = ff.event_id if ff is not None else None
    row["mql5_event_id"] = m.event_id if m is not None else None
    row["event_id"] = f"macro:{row['canonical_event_id']}"
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
            row["actual_minus_forecast"] = round(row["actual_value"] - row["ff_provider_forecast"], 10)   # drop float noise

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

    # ---- prior period (R-1): previous vs the vintage of the previous release, revised_previous vs this one ----
    prev_ref = _prev_month(ref) if ref is not None else None
    prior_asof = [r for r in (prior_fred_rows or []) if r.official_vintage_kind == "AS_OF"]
    row["previous_reference_period"] = prev_ref.isoformat() if prev_ref else None
    row["previous_prior_release_date"] = prior_release_date.isoformat() if prior_release_date else None
    m_prev_unit = _unit(m.previous_unit) if m is not None else UNKNOWN
    if ref is None:
        none_res = VintageResolution(None, "NONE", "event has no reference_period, prior period unknown")
        prev_res = rev_res = none_res
    else:
        prev_res = (resolve_release_vintage(prior_asof, prior_release_date, cfg.max_vintage_lag_days)
                    if prior_release_date else
                    VintageResolution(None, "NONE", "prior release date unknown, cannot anchor the previous vintage"))
        rev_res = resolve_release_vintage(prior_asof, release_date, cfg.max_vintage_lag_days)
    if prev_res.code == "AMBIGUOUS" or rev_res.code == "AMBIGUOUS":
        flags.add(AMBIGUOUS_MATCH); issues.append("AMBIGUOUS:conflicting prior-period as-of values")
    if prev_res.row is not None:
        row.update(previous_official_release_vintage=_clean(prev_res.row.official_actual),
                   previous_official_vintage_date=prev_res.row.official_vintage_date.isoformat())
    if rev_res.row is not None:
        row.update(revised_previous_official_release_vintage=_clean(rev_res.row.official_actual),
                   revised_previous_official_vintage_date=rev_res.row.official_vintage_date.isoformat())
    if m is None:
        row["previous_validation_status"] = row["revised_previous_validation_status"] = "NOT_APPLICABLE"
        notes.append("previous/revised_previous validated from MQL5 values; no MQL5 row")
    else:
        row["previous_validation_status"] = _validate_prior(row["mql5_previous"], m_prev_unit, prev_res.row,
                                                            prev_res, cfg, flags, issues, "MQL5 previous")
        row["revised_previous_validation_status"] = _validate_prior(row["mql5_revised_previous"], m_prev_unit,
                                                                    rev_res.row, rev_res, cfg, flags, issues,
                                                                    "MQL5 revised_previous")
    if prev_res.row is not None and rev_res.row is not None:
        row["official_prior_period_revision"] = row["revised_previous_official_release_vintage"] - row["previous_official_release_vintage"]
        pu = _unit(prev_res.row.official_actual_unit)
        if (row["mql5_revised_previous"] is None and pu == _unit(rev_res.row.official_actual_unit)
                and _display_round(row["previous_official_release_vintage"], pu, cfg)
                != _display_round(row["revised_previous_official_release_vintage"], pu, cfg)):
            issues.append("PRIOR_REVISION_NOT_REPORTED:official prior-period value changed between the two release "
                          f"vintages ({row['previous_official_release_vintage']:.6g} -> "
                          f"{row['revised_previous_official_release_vintage']:.6g}) but the calendar reports no revised_previous")

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

    # ---- timestamps / broker timezone ----
    ff_utc = _ff_trusted_utc(ff)                       # trusted = CONFIRMED Forex Factory time only
    m_utc, m_basis = _mql5_utc(m, cfg, ff_utc) if m is not None else (None, "n/a")
    row["ff_release_timestamp_utc"] = ff_utc.isoformat() if ff_utc else None
    row["candidate_broker_timezone"] = cfg.mql5_broker_timezone
    if m is not None and m.source_timestamp is not None:
        row["mql5_raw_timestamp"] = m.source_timestamp.isoformat()
    if m_utc is not None:
        row["mql5_converted_utc"] = m_utc.isoformat()
    if ff_utc is not None and m_utc is not None:
        delta = (m_utc - ff_utc).total_seconds()
        row["timestamp_delta_seconds"] = delta
        row["timestamp_status"] = "MATCH" if abs(delta) <= cfg.timestamp_tolerance_seconds else "MISMATCH"
        if row["timestamp_status"] == "MISMATCH":
            flags.add(TIMESTAMP_MISMATCH)
            issues.append(f"TIMESTAMP_MISMATCH:MQL5 {m_utc.isoformat()} vs FF {ff_utc.isoformat()} "
                          f"(delta {delta:+.0f}s, tolerance {cfg.timestamp_tolerance_seconds:.0f}s)")
        if "DST" in m_basis:
            notes.append(m_basis.split("; ", 1)[-1])
    elif ff is not None and m is not None:
        row["timestamp_status"] = "UNVERIFIABLE"
        why = ("MQL5 UTC timestamp unresolved (broker timezone not supplied/confirmed)" if m_utc is None
               else "no CONFIRMED Forex Factory UTC timestamp to compare against")
        issues.append("TIMESTAMP_UNVERIFIABLE:" + why)
        if m.source_timestamp is not None and ff_utc is not None:
            row["mql5_implied_server_utc_offset_hours"] = round(
                (m.source_timestamp - ff_utc.replace(tzinfo=None)).total_seconds() / 3600, 4)
            notes.append("mql5_implied_server_utc_offset_hours is a DIAGNOSTIC (MQL5 server time minus FF UTC), "
                         "not a confirmation of the broker timezone")
    else:
        row["timestamp_status"] = "N/A"
    row["timezone_validation_status"] = row["timestamp_status"]
    if m_utc is not None:
        row.update(release_timestamp=m_utc.isoformat(), release_timestamp_source=m_basis)
    elif ff_utc is not None:
        row.update(release_timestamp=ff_utc.isoformat(), release_timestamp_source="ff.release_timestamp_utc (MQL5 UTC unresolved)")
    if ff_utc is not None:
        row.update(trusted_release_timestamp_utc=ff_utc.isoformat(), trusted_release_timestamp_basis="FF_CONFIRMED")

    status = next(s for s in STATUS_PRIORITY if s == MATCH or s in flags)
    row.update(source_match_status=status, issues=" | ".join(issues), notes=" | ".join(notes))
    return row


# --------------------------------------------------------------------------- entry point
def reconcile(ff_events: Sequence[MacroEvent], mql5_events: Sequence[MacroEvent], fred_events: Sequence[MacroEvent],
              start: dt.date, end: dt.date, mapping: EventMapping, cfg: ValidationConfig = ValidationConfig(),
              fred_profiles: Optional[Dict[Tuple[str, str], FredSeriesProfile]] = None,
              timezone_evidence: Optional[Dict[str, Any]] = None) -> ReconciliationResult:
    """`timezone_evidence`: a validate_broker_timezone() block computed over a (wider) evidence window; when given,
    it -- not this window's own block -- decides whether MQL5 times are trusted.
    Rows = calendar releases (Forex Factory / MQL5) with release date in [start, end]; FRED enriches only."""
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

    # Release date of every (family, reference_period) the calendars know about -- also OUTSIDE the window --
    # so the previous release of an indicator can anchor the vintage `previous` is validated against.
    release_dates: Dict[Tuple[str, dt.date], dt.date] = {}
    for events in (ff_events, mql5_events):          # Forex Factory (UTC) first, MQL5 only fills gaps
        for e in events:
            if e.event_family in fams and e.reference_period is not None:
                d = _event_date(e, cfg)
                if d is not None:
                    release_dates.setdefault((e.event_family, e.reference_period), d)

    # Indicators in one release bundle are published simultaneously (config/event_mapping.yaml), so a family
    # without its own calendar history can borrow the prior release date of a bundle sibling (flagged in notes).
    bundle_dates: Dict[Tuple[str, dt.date], dt.date] = {}
    for (fam, ref_), d in release_dates.items():
        entry_ = mapping.by_family(fam)
        if entry_ and entry_.release_bundle:
            bundle_dates.setdefault((entry_.release_bundle, ref_), d)

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
        prev_ref = _prev_month(ref) if ref else None
        if prev_ref:
            used.add((family, prev_ref))
        prior_rd = release_dates.get((family, prev_ref)) if prev_ref else None
        via_bundle = False
        if prev_ref and prior_rd is None:
            entry_ = mapping.by_family(family)
            if entry_ and entry_.release_bundle:
                prior_rd = bundle_dates.get((entry_.release_bundle, prev_ref))
                via_bundle = prior_rd is not None
        row = _build_row(family, f, m, fred_rows, mapping, cfg, fred_profiles, None,
                         prior_fred_rows=fred_by_key.get((family, prev_ref), []) if prev_ref else [],
                         prior_release_date=prior_rd)
        if via_bundle:
            row["notes"] = (row["notes"] + " | " if row["notes"] else "") + (
                f"previous release date {prior_rd} taken from a same-bundle indicator (no own calendar history)")
        rows.append(row)

    unused = sum(len(v) for k, v in fred_by_key.items() if k not in used)
    if unused:
        warnings.append(f"{unused} FRED row(s) for reference periods with no calendar release in the window were "
                        "not used (other reference periods, e.g. transform lookback or observations released after the "
                        "window; FRED enriches calendar releases and never creates release events)")
    tz = validate_broker_timezone(rows, cfg)
    gate = timezone_evidence if timezone_evidence is not None else tz
    for r in rows:      # trusted release timestamp: FF CONFIRMED (already set) > MQL5 under a VALIDATED timezone
        if r["trusted_release_timestamp_utc"] is None and r["mql5_converted_utc"] is not None \
                and gate["mql5_timestamps_trusted"] and gate["candidate_broker_timezone"] == cfg.mql5_broker_timezone:
            r["trusted_release_timestamp_utc"] = r["mql5_converted_utc"]
            r["trusted_release_timestamp_basis"] = f"MQL5_VALIDATED_BROKER_TZ:{cfg.mql5_broker_timezone}"
    rows.sort(key=lambda r: (r["trusted_release_timestamp_utc"] or r["release_timestamp"] or r["release_date"] or "9999",
                             r["event_family"]))
    return ReconciliationResult(rows, warnings, tz)


def _zone_changes_offset(tz_name: Optional[str], years) -> bool:
    if not tz_name:
        return False
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    return any(dt.datetime(y, 1, 1, 12, tzinfo=tz).utcoffset() != dt.datetime(y, 7, 1, 12, tzinfo=tz).utcoffset()
               for y in years)


def validate_broker_timezone(rows: Sequence[Dict[str, Any]], cfg: ValidationConfig) -> Dict[str, Any]:
    """Aggregate verdict on the candidate broker timezone from the per-event comparisons.

    Evidence = releases with BOTH a CONFIRMED Forex Factory UTC time and a converted MQL5 time. Status is
    MATCH only if every such release agrees within tolerance, MISMATCH if any disagrees, UNVERIFIABLE if
    there is no evidence (no timezone supplied, no MQL5 row, or no trusted Forex Factory time). Nothing is
    written to config; this is a report."""
    per_release: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r["timezone_validation_status"] not in ("MATCH", "MISMATCH"):
            continue
        key = r["ff_release_timestamp_utc"]
        rec = per_release.setdefault(key, {"ff_confirmed_utc": key, "mql5_raw_timestamp": r["mql5_raw_timestamp"],
                                            "candidate_broker_timezone": r["candidate_broker_timezone"],
                                            "mql5_converted_utc": r["mql5_converted_utc"],
                                            "delta_seconds": r["timestamp_delta_seconds"],
                                            "status": r["timezone_validation_status"], "events": []})
        rec["events"].append(r["canonical_event_id"])
        if r["timezone_validation_status"] == "MISMATCH":
            rec["status"] = "MISMATCH"
    releases = [per_release[k] for k in sorted(per_release)]
    n_bad = sum(1 for x in releases if x["status"] == "MISMATCH")
    if not releases:
        status = "UNVERIFIABLE"
    else:
        status = "MISMATCH" if n_bad else "MATCH"
    offsets = sorted({round((dt.datetime.fromisoformat(x["mql5_raw_timestamp"])
                             - dt.datetime.fromisoformat(x["mql5_converted_utc"]).replace(tzinfo=None)).total_seconds() / 3600, 4)
                      for x in releases})
    has_dst = _zone_changes_offset(cfg.mql5_broker_timezone, {dt.datetime.fromisoformat(x["ff_confirmed_utc"]).year
                                                                for x in releases}) if releases else False
    dst_covered = (len(offsets) >= 2) if has_dst else True
    sufficient = len(releases) >= cfg.min_tz_evidence_releases and dst_covered
    return {
        "candidate_broker_timezone": cfg.mql5_broker_timezone, "status": status,
        "distinct_trusted_releases": len(releases), "releases_matching": len(releases) - n_bad,
        "releases_mismatching": n_bad, "min_evidence_releases": cfg.min_tz_evidence_releases,
        "evidence_sufficient": sufficient, "observed_utc_offsets_hours": offsets,
        "candidate_zone_has_dst_in_evidence_years": has_dst, "dst_regimes_covered": dst_covered,
        "max_abs_delta_seconds": max((abs(x["delta_seconds"]) for x in releases), default=None),
        "mql5_timestamps_trusted": bool(status == "MATCH" and sufficient and cfg.mql5_broker_timezone),
        "per_release": releases,
        "note": ("Consistency with trusted Forex Factory times is evidence, not proof of the broker's configuration; "
                 "nothing is written to config."
                 + ("" if dst_covered else " INSUFFICIENT: the candidate zone observes DST but every release falls under one "
                    "UTC offset, so it cannot be distinguished from a fixed offset.")),
    }


def summarize(rows: Sequence[Dict[str, Any]], key: str = "source_match_status") -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return counts
