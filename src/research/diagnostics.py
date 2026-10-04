"""Data-quality and purely descriptive statistics for the event-response dataset.

Nothing here is a model or a trading signal. Correlations and conditional means are reported
with their sample sizes and are NOT causal estimates. All inclusion goes through
src/research/filters.py, so excluded events never contribute. Statistics are computed PER
(family, symbol); `require_single_family` guards that no family-specific number pools families.

QUANTILE BINS are tie-safe: bins are formed over the sorted UNIQUE standardized-surprise values,
so equal values always share a bin. actual_bins = min(requested, unique values). Because the
standardization history drifts, identical RAW surprises can map to slightly different
standardized values; a quantile diagnostic is therefore produced only when the rows contain at
least `requested` distinct RAW surprise values, and never for a `weak_surprise_proxy` family --
otherwise one row per (family, symbol, horizon) records why it was skipped. Bin edges are
full-sample and descriptive only.

Families whose surprise is flagged `weak_surprise_proxy` (FOMC) are labelled in every table; their
surprise-response statistics are not evidence about monetary-policy surprises.
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .events import (
    AFTER_RESEARCH_END, EXCLUDED_AMBIGUOUS_TIMESTAMP, EXCLUDED_NONSTANDARD_TIME, EXCLUDED_TIMESTAMP_CONFLICT,
    UNSCHEDULED_RELEASE, official_validation_summary,
)
from .filters import require_single_family, usable_events, usable_response, usable_std_surprise

POST = (1, 5, 15, 30, 60)
REQUESTED_BINS = 5


def tie_safe_bins(values: pd.Series, requested: int = REQUESTED_BINS) -> Dict[str, Any]:
    """Assign bins over unique sorted values (ties never split). Returns labels aligned to
    `values` plus requested/actual/unique counts."""
    v = values.astype(float)
    uniq = np.sort(v.unique())
    actual = min(requested, len(uniq))
    if len(uniq) < 2:
        return {"labels": None, "requested_bins": requested, "actual_bins": 0, "unique_values": int(len(uniq))}
    groups = np.array_split(uniq, actual)
    lookup = {x: b + 1 for b, grp in enumerate(groups) for x in grp}
    return {"labels": v.map(lookup), "requested_bins": requested, "actual_bins": actual, "unique_values": int(len(uniq))}


def window_status_counts(windows: pd.DataFrame) -> Dict[str, int]:
    return {k: int(v) for k, v in windows["status"].value_counts().sort_index().items()}


def quality_tables(wide: pd.DataFrame, events: pd.DataFrame, windows: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    rows = []
    for (fam, sym), g in wide.groupby(["event_family", "symbol"]):
        require_single_family(g)
        usable = usable_events(g)
        rec = {
            "event_family": fam, "symbol": sym, "surprise_measure_quality": g["surprise_measure_quality"].iloc[0],
            "releases": len(g), "usable_events": int(usable.sum()),
            "with_forecast": int(g["forecast"].notna().sum()), "with_actual": int(g["actual"].notna().sum()),
            "valid_surprise": int((usable & g["surprise_raw"].notna()).sum()),
            "valid_std_surprise": int(usable_std_surprise(g).sum()),
            "nonzero_surprises": int((usable & g["surprise_raw"].notna() & (g["surprise_raw"] != 0)).sum()),
            "official_actual_cross_validated": int(g["ff_actual_vs_official_release_vintage"].isin(
                ["EXACT_MATCH", "ROUNDING_MATCH", "VALUE_MISMATCH"]).sum()),
        }
        for h in (1, 5, 30, 60):
            rec[f"valid_post{h}m"] = int(usable_response(g, f"post{h}m").sum())
        post_cols = [c for c in g.columns if c.startswith("post") and c.endswith("_status")]
        rec["provider_gap_exclusions"] = int((usable & g[post_cols].isin(["provider_gap_in_window"]).any(axis=1)).sum())
        rec["market_halt_tagged"] = int((usable & g["any_market_halt"].fillna(False).astype(bool)).sum())
        rec["insufficient_premarket_post5m"] = int((usable & (g["release_session_state"] == "premarket")
                                                   & (g["post5m_status"] == "insufficient_market_window")).sum())
        rec["exchange_closed"] = int((usable & (g["post5m_status"] == "exchange_closed")).sum())
        rec["provisional_exclusions"] = int((g["event_status"] == AFTER_RESEARCH_END).sum())
        rec["ambiguous_timestamp_exclusions"] = int(g["event_status"].isin(
            [EXCLUDED_AMBIGUOUS_TIMESTAMP, EXCLUDED_TIMESTAMP_CONFLICT, EXCLUDED_NONSTANDARD_TIME]).sum())
        rec["unscheduled_exclusions"] = int((g["event_status"] == UNSCHEDULED_RELEASE).sum())
        rows.append(rec)
    by_fs = pd.DataFrame(rows)

    ev = events.copy()
    ev["year"] = ev["release_date_et"].str[:4].fillna("no_time")
    by_year = ev.pivot_table(index="event_family", columns="year", values="event_id", aggfunc="count", fill_value=0).reset_index()
    usable_year = ev[usable_events(ev)].pivot_table(index="event_family", columns="year", values="event_id",
                                                    aggfunc="count", fill_value=0).reset_index()
    one_sym = wide[wide["symbol"] == sorted(wide["symbol"].unique())[0]]
    split = one_sym[usable_events(one_sym)].pivot_table(index="event_family", columns="split", values="event_id",
                                                        aggfunc="count", fill_value=0).reset_index()
    statuses = events.groupby(["event_family", "event_status"]).size().reset_index(name="events")
    wstat = pd.DataFrame([{"status": k, "windows": v} for k, v in window_status_counts(windows).items()])
    off = official_validation_summary(events)
    official = pd.DataFrame([{k: v for k, v in off.items() if not isinstance(v, (dict, str))}])
    return {"by_family_symbol": by_fs, "events_by_year": by_year, "usable_events_by_year": usable_year,
            "usable_events_by_split": split, "event_status": statuses, "window_status": wstat,
            "official_validation": official}


def diagnostics_tables(wide: pd.DataFrame, spec: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    corr_rows, sign_rows, q_rows, dist_rows = [], [], [], []
    for (fam, sym), g in wide.groupby(["event_family", "symbol"]):
        require_single_family(g)
        tag = {"event_family": fam, "symbol": sym, "surprise_measure_quality": g["surprise_measure_quality"].iloc[0]}
        s = g[usable_std_surprise(g)]
        dist_rows.append({**tag, "quantity": "surprise_raw", **_describe(g.loc[usable_events(g), "surprise_raw"])})
        dist_rows.append({**tag, "quantity": "surprise_std", **_describe(s["surprise_std"])})
        for h in POST:
            win = f"post{h}m"
            ok = usable_response(g, win)
            dist_rows.append({**tag, "quantity": f"{win}_ret", **_describe(g.loc[ok, f"{win}_ret"])})
            both = s[usable_response(s, win)]
            x, y = both["surprise_std"].astype(float), both[f"{win}_ret"].astype(float)
            valid = len(both) >= 3 and x.nunique() > 1 and y.nunique() > 1  # undefined without variation in both
            corr_rows.append({**tag, "horizon_min": h, "n": len(both), "unique_std_surprises": int(x.nunique()),
                              "pearson": float(x.corr(y)) if valid else None,
                              # Spearman = Pearson correlation of average ranks (no scipy dependency)
                              "spearman": float(x.rank().corr(y.rank())) if valid else None})
            usable_sign = g[ok & g["surprise_sign"].notna()]
            for sign, gg in usable_sign.groupby("surprise_sign"):
                sign_rows.append({**tag, "horizon_min": h, "surprise_sign": int(sign), "n": len(gg),
                                  "mean_ret": float(gg[f"{win}_ret"].mean()), "median_ret": float(gg[f"{win}_ret"].median())})
            raw_unique = int(both["surprise_raw"].astype(float).nunique())
            skip = ("weak_surprise_proxy" if tag["surprise_measure_quality"] == "weak_surprise_proxy"
                    else "insufficient_distinct_raw_surprises" if raw_unique < REQUESTED_BINS else None)
            if skip:
                q_rows.append({**tag, "horizon_min": h, "bin": None, "requested_bins": REQUESTED_BINS, "actual_bins": 0,
                               "unique_std_surprises": int(x.nunique()), "unique_raw_surprises": raw_unique,
                               "n": len(both), "skipped_reason": skip})
            elif len(both) >= 3:
                b = tie_safe_bins(both["surprise_std"])
                if b["labels"] is not None:
                    for qn, gg in both.groupby(b["labels"]):
                        q_rows.append({**tag, "horizon_min": h, "bin": int(qn), "requested_bins": b["requested_bins"],
                                       "actual_bins": b["actual_bins"], "unique_std_surprises": b["unique_values"],
                                       "unique_raw_surprises": raw_unique, "skipped_reason": None,
                                       "n": len(gg), "min_std_surprise": float(gg["surprise_std"].astype(float).min()),
                                       "max_std_surprise": float(gg["surprise_std"].astype(float).max()),
                                       "mean_ret": float(gg[f"{win}_ret"].mean())})
    return {"correlation": pd.DataFrame(corr_rows), "by_sign": pd.DataFrame(sign_rows),
            "by_quantile_bin": pd.DataFrame(q_rows), "distributions": pd.DataFrame(dist_rows)}


def baseline_evaluation(wide: pd.DataFrame, spec: Dict[str, Any]) -> pd.DataFrame:
    """Descriptive walk-forward evaluation per split / family / symbol / horizon: mean squared
    observed return vs mean squared residual, and how many predictions used same-split history."""
    rows: List[dict] = []
    for (split, fam, sym), g in wide.groupby(["split", "event_family", "symbol"]):
        require_single_family(g)
        split_start = spec["split"][split][0] if split in ("development", "validation", "test") else None
        for h in spec["baseline"]["horizons"]:
            gg = g[usable_response(g, f"post{h}m") & (g[f"baseline_post{h}m_status"] == "ok")]
            if gg.empty:
                continue
            cutoff_dates = pd.to_datetime(gg[f"baseline_post{h}m_training_cutoff_utc"], utc=True).dt.tz_convert(
                "America/New_York").dt.date.astype(str)
            o, r = gg[f"post{h}m_ret"].astype(float), gg[f"resid_post{h}m_ret"].astype(float)
            rows.append({"split": split, "event_family": fam, "symbol": sym, "horizon_min": h, "n": len(gg),
                         "surprise_measure_quality": gg["surprise_measure_quality"].iloc[0],
                         "ms_observed_bp2": float(np.mean(o ** 2) * 1e8), "ms_residual_bp2": float(np.mean(r ** 2) * 1e8),
                         "predictions_using_same_split_history": int((cutoff_dates >= split_start).sum()) if split_start else None,
                         "evaluation_paradigm": spec["baseline"]["evaluation_paradigm"]})
    return pd.DataFrame(rows)


def _describe(x: pd.Series) -> Dict[str, Any]:
    x = pd.to_numeric(x, errors="coerce").dropna()
    if x.empty:
        return {"n": 0}
    return {"n": int(len(x)), "mean": float(x.mean()), "std": float(x.std(ddof=1)) if len(x) > 1 else None,
            "p05": float(x.quantile(0.05)), "median": float(x.median()), "p95": float(x.quantile(0.95)),
            "min": float(x.min()), "max": float(x.max())}
