"""Canonical macro event table with explicit point-in-time provenance.

One row per Forex Factory release of a configured family (config/event_research.yaml).

VALUES AND WHAT IS (NOT) KNOWN ABOUT THEM. `actual`, `forecast`, `previous` and
`revised_previous` are the values on Forex Factory historical calendar pages that were SAVED
RETROSPECTIVELY (in 2026) and preserved unchanged by the importer
(`value_provenance = retrospective_historical_page`). It is plausible -- but for most events NOT
independently verified -- that these are exactly the values displayed immediately before / at the
historical release. Point-in-time status fields state the evidence actually available:

  forecast_point_in_time_status  always `historical_page_unverified_point_in_time`: no source in
                                 this repository can verify a historical forecast's pre-release
                                 availability. `forecast` is Forex Factory's provider forecast, not
                                 a documented economist consensus.
  actual_point_in_time_status    `official_release_vintage_verified` when the Forex Factory actual
                                 equals (exactly or within display rounding) the official ALFRED
                                 release-vintage value; `official_release_vintage_mismatch` when it
                                 differs; else `historical_page_unverified_point_in_time`.
  event_point_in_time_status     `partially_cross_validated` (actual verified, forecast not) or
                                 `historical_page_unverified_point_in_time`.

Official values come from the reconciliation layer as NUMBERS ONLY (release-vintage value, its
vintage date, latest-revised value). Every comparison status is computed HERE from the Forex
Factory actual -- reconciliation's own match statuses may describe an MQL5 value and are never
copied, so MQL5 (quarantined) can never upgrade Forex Factory or official verification.

Events are never silently dropped: every row carries `event_status` / `event_status_reason`.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from ..data.config import REPO_ROOT, AppConfig
from ..data.validation.macro_events import ValidationConfig, compare_to_release_vintage

NY = ZoneInfo("America/New_York")
DEFAULT_SPEC_PATH = REPO_ROOT / "config" / "event_research.yaml"

USABLE = "usable"
EXCLUDED_AMBIGUOUS_TIMESTAMP = "excluded_ambiguous_timestamp"        # no confirmed release time
EXCLUDED_TIMESTAMP_CONFLICT = "excluded_timestamp_conflict"          # contradicts its own release bundle
EXCLUDED_NONSTANDARD_TIME = "excluded_nonstandard_release_time"      # unverifiable off-schedule minute
UNSCHEDULED_RELEASE = "excluded_unscheduled_release"                 # e.g. emergency FOMC action
AFTER_RESEARCH_END = "excluded_after_research_end"                   # market data provisional / not frozen

VALUE_PROVENANCE = "retrospective_historical_page"
PIT_UNVERIFIED = "historical_page_unverified_point_in_time"
PIT_OFFICIAL_VERIFIED = "official_release_vintage_verified"
PIT_OFFICIAL_MISMATCH = "official_release_vintage_mismatch"
PIT_PARTIAL = "partially_cross_validated"
PROVENANCE_STATEMENT = (
    "Forex Factory historical forecast/actual/previous values preserved from retrospectively saved "
    "historical calendar pages (saved 2026); exact pre-release availability is not independently "
    "verified for most events. Only actuals with an official ALFRED release-vintage comparison are "
    "cross-validated; no forecast is independently verified.")

EVENT_COLUMNS = [
    "event_id", "source_event_id", "event_family", "event_name", "family_group", "release_bundle",
    "country", "frequency", "publisher", "source",
    "release_id", "release_timestamp_utc", "release_time_et", "release_date_et", "release_timestamp_quality",
    "release_time_precision", "release_bar_timing_status", "release_time_status", "concurrent_families",
    "reference_period", "reference_period_basis",
    "value_unit", "actual", "forecast", "previous", "revised_previous",
    "value_source", "value_provenance", "forecast_point_in_time_status", "actual_point_in_time_status",
    "event_point_in_time_status",
    "official_source", "official_release_vintage_value", "official_vintage_date", "official_unit",
    "official_latest_value", "official_latest_vintage_date", "ff_actual_vs_official_release_vintage",
    "surprise_measure_quality", "numeric_surprise_direction", "raw_artifact_checksum",
    "event_status", "event_status_reason",
]


def load_research_spec(path: Optional[Path] = None) -> Dict[str, Any]:
    with open(path or DEFAULT_SPEC_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _num(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else float(x)


def official_comparison(ff_actual, official_value, ff_unit, official_unit) -> str:
    """Forex Factory actual vs official release-vintage value -- never anything MQL5-derived."""
    if official_value is None:
        return "no_official_release_vintage"
    if ff_actual is None:
        return "no_ff_actual"
    if not ff_unit or not official_unit or ff_unit != official_unit or ff_unit == "UNKNOWN":
        return "not_comparable_units"
    status, _ = compare_to_release_vintage(float(ff_actual), float(official_value), ff_unit, ValidationConfig())
    return status  # EXACT_MATCH | ROUNDING_MATCH | VALUE_MISMATCH


def build_event_table(ff: pd.DataFrame, spec: Dict[str, Any], official_rows=None) -> pd.DataFrame:
    """`ff`: normalized Forex Factory events. `official_rows`: reconciliation rows; only their
    official NUMBERS (linked by `ff_event_id`) are used."""
    fams = spec["families"]
    research_end = dt.date.fromisoformat(spec["split"]["research_end"])
    df = ff[ff["event_family"].isin(fams)].copy()
    df["ts"] = pd.to_datetime(df["release_timestamp_utc"], utc=True)
    official = {r["ff_event_id"]: r for r in (official_rows or []) if r.get("ff_event_id")}

    rows = []
    for _, r in df.iterrows():
        fam = r["event_family"]
        fs = fams[fam]
        ts = r["ts"]
        has_ts = pd.notna(ts) and r["timestamp_quality"] == "CONFIRMED"
        local = ts.tz_convert(NY) if has_ts else None
        off = official.get(r["event_id"], {})
        actual = _num(r.get("actual"))
        off_value = _num(off.get("official_release_vintage_value"))
        cmp = official_comparison(actual, off_value, r.get("actual_unit"), off.get("official_unit"))
        actual_pit = {"EXACT_MATCH": PIT_OFFICIAL_VERIFIED, "ROUNDING_MATCH": PIT_OFFICIAL_VERIFIED,
                      "VALUE_MISMATCH": PIT_OFFICIAL_MISMATCH}.get(cmp, PIT_UNVERIFIED)
        rows.append({
            "event_id": r["event_id"], "source_event_id": r.get("source_event_id"), "event_family": fam,
            "event_name": fs["name"], "family_group": fs["group"], "release_bundle": r.get("release_bundle"),
            "country": spec["country"], "frequency": fs["frequency"], "publisher": fs["publisher"],
            "source": spec["event_source"],
            "release_id": ts.isoformat() if has_ts else None,
            "release_timestamp_utc": ts if has_ts else pd.NaT,
            "release_time_et": local.strftime("%H:%M") if has_ts else None,
            "release_date_et": local.date().isoformat() if has_ts else None,
            "release_timestamp_quality": r["timestamp_quality"],
            "release_time_precision": spec["release_time_precision"],
            # Forex Factory gives a scheduled minute: the release bar may contain a few seconds of
            # pre-publication trading; a timestamp with seconds straddles its release bar.
            "release_bar_timing_status": (None if not has_ts else
                                          "minute_precision" if ts.second == 0 and ts.microsecond == 0 and ts.nanosecond == 0
                                          else "straddling_release_bar"),
            "release_time_status": None,
            "concurrent_families": None,
            "reference_period": r.get("reference_period").isoformat() if r.get("reference_period") is not None and not pd.isna(r.get("reference_period")) else None,
            # The importer's "released the following month" heuristic is wrong for e.g. PCE and
            # quarterly GDP: informational only, never a join key.
            "reference_period_basis": "official_validation_join" if off.get("official_reference_period") else "calendar_heuristic_unverified",
            "value_unit": r.get("actual_unit"),
            "actual": actual,
            "forecast": _num(r.get("provider_forecast")),
            "previous": _num(r.get("previous")),
            "revised_previous": _num(r.get("revised_previous")),
            "value_source": spec["event_source"],
            "value_provenance": VALUE_PROVENANCE,
            "forecast_point_in_time_status": PIT_UNVERIFIED,
            "actual_point_in_time_status": actual_pit,
            "event_point_in_time_status": PIT_PARTIAL if actual_pit == PIT_OFFICIAL_VERIFIED else PIT_UNVERIFIED,
            "official_source": off.get("official_source") if off_value is not None else None,
            "official_release_vintage_value": off_value,
            "official_vintage_date": off.get("official_vintage_date") if off_value is not None else None,
            "official_unit": off.get("official_unit") if off_value is not None else None,
            "official_latest_value": _num(off.get("official_latest_value")),
            "official_latest_vintage_date": off.get("official_latest_vintage_date"),
            "ff_actual_vs_official_release_vintage": cmp,
            "surprise_measure_quality": fs.get("surprise_measure_quality", "calendar_forecast_surprise"),
            "numeric_surprise_direction": fs["numeric_surprise_direction"],
            "raw_artifact_checksum": r.get("raw_artifact_checksum"),
            "event_status": USABLE, "event_status_reason": None,
        })
    out = pd.DataFrame(rows, columns=EVENT_COLUMNS)

    # Release grouping: every configured event published at the same instant is one market release.
    has_ts = out["release_id"].notna()
    conc = out[has_ts].groupby("release_id")["event_family"].apply(lambda s: ",".join(sorted(s)))
    out.loc[has_ts, "concurrent_families"] = out.loc[has_ts, "release_id"].map(conc)

    for i, r in out.iterrows():
        fs = fams[r["event_family"]]
        if r["release_timestamp_quality"] != "CONFIRMED" or pd.isna(r["release_timestamp_utc"]):
            out.at[i, "release_time_status"] = "missing"
            out.at[i, "event_status"], out.at[i, "event_status_reason"] = (
                EXCLUDED_AMBIGUOUS_TIMESTAMP, f"timestamp_quality={r['release_timestamp_quality']}: no confirmed release time")
            continue
        standard = r["release_time_et"] in fs["expected_release_times_et"]
        out.at[i, "release_time_status"] = "standard" if standard else "nonstandard"
        if r["event_family"] == "FED_FUNDS_RATE" and (not standard or pd.isna(r["forecast"])):
            out.at[i, "release_time_status"] = "unscheduled"
            out.at[i, "event_status"], out.at[i, "event_status_reason"] = (
                UNSCHEDULED_RELEASE, f"FOMC action at {r['release_time_et']} ET"
                f"{' without a forecast' if pd.isna(r['forecast']) else ''}: not a scheduled decision")
            continue
        if not standard:
            # Same bundle published at a different minute the same day => internal contradiction.
            same_day = out[(out["release_date_et"] == r["release_date_et"]) & (out["release_bundle"] == r["release_bundle"])
                           & (out["event_id"] != r["event_id"]) & out["release_time_et"].isin(fs["expected_release_times_et"])]
            if len(same_day):
                out.at[i, "event_status"], out.at[i, "event_status_reason"] = (
                    EXCLUDED_TIMESTAMP_CONFLICT, f"{r['release_time_et']} ET contradicts same-day {r['release_bundle']} bundle "
                    f"members at {sorted(set(same_day['release_time_et']))} ET")
            else:
                out.at[i, "event_status"], out.at[i, "event_status_reason"] = (
                    EXCLUDED_NONSTANDARD_TIME, f"{r['release_time_et']} ET is not a documented release time for this family; "
                    f"not verifiable without an authoritative release schedule")
            continue
        if dt.date.fromisoformat(r["release_date_et"]) > research_end:
            out.at[i, "event_status"], out.at[i, "event_status_reason"] = (
                AFTER_RESEARCH_END, f"after research_end {research_end} (market data provisional / not frozen)")
    out["release_timestamp_utc"] = pd.to_datetime(out["release_timestamp_utc"], utc=True)
    return out.sort_values(["release_timestamp_utc", "event_family", "event_id"], na_position="last").reset_index(drop=True)


def official_validation_summary(events: pd.DataFrame) -> Dict[str, Any]:
    c = events["ff_actual_vs_official_release_vintage"].value_counts().to_dict()
    validated = int(c.get("EXACT_MATCH", 0) + c.get("ROUNDING_MATCH", 0) + c.get("VALUE_MISMATCH", 0))
    return {"total_events": int(len(events)), "officially_cross_validated_events": validated,
            "exact_matches": int(c.get("EXACT_MATCH", 0)), "rounding_matches": int(c.get("ROUNDING_MATCH", 0)),
            "mismatches": int(c.get("VALUE_MISMATCH", 0)), "not_validated": int(len(events) - validated),
            "comparison_status_counts": {k: int(v) for k, v in sorted(c.items())},
            "forecasts_independently_verified": 0, "statement": PROVENANCE_STATEMENT}


def load_ff_events(config: AppConfig) -> pd.DataFrame:
    return pd.read_parquet(config.interim_root / "macro" / "forex_factory_events.parquet")
