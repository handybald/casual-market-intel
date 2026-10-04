"""Canonical surprise definitions (surprise_spec_version in config/event_research.yaml).

  surprise_raw       = actual - forecast                               (null when either is missing)
  surprise_relative  = surprise_raw / forecast   only for families with `relative_surprise: true`
                       AND forecast > 0; otherwise null with a reason
  surprise_std       = (surprise_raw_t - mean_{s<t}) / std_{s<t}       per event family, using ONLY
                       usable releases of the same family whose release timestamp is STRICTLY
                       earlier than t (expanding window, ddof=1). Families are never pooled (GDP
                       advance / second / third are separate families).
  surprise_sign      = -1 / 0 / +1 of surprise_raw (numeric only -- see numeric_surprise_direction)

History eligibility compares SEMANTIC UTC timestamps (pandas Timestamps), never integer storage
values, so it is independent of datetime64[ns]/[us]/[ms] or Python-datetime storage; rows of the
same release (equal timestamps) never enter each other's history.

surprise_std_status:
  ok                          standardized value present
  missing_forecast            no forecast -> no raw surprise -> no standardized value
  missing_actual              no actual   -> no raw surprise -> no standardized value
  insufficient_history        fewer than `min_history` earlier surprises
  zero_historical_dispersion  earlier std <= zero_dispersion_rel_tol * max(1, max|earlier|): the
                              history is (numerically) constant; null, never a giant z-score
  event_excluded              the event itself is not usable; null (and never enters history)
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from .events import USABLE

SURPRISE_COLUMNS = [
    "surprise_raw", "surprise_relative", "surprise_relative_status", "surprise_sign",
    "surprise_std", "surprise_std_status", "surprise_std_n_history", "surprise_std_hist_mean",
    "surprise_std_hist_std", "surprise_std_history_through_utc",
]


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x)) or pd.isna(x)


def utc_timestamps(series: pd.Series) -> pd.Series:
    """Resolution-safe semantic timestamps (tz-aware UTC), whatever the storage dtype."""
    return pd.to_datetime(series, utc=True)


def add_surprises(events: pd.DataFrame, spec: Dict[str, Any]) -> pd.DataFrame:
    fams = spec["families"]
    std_cfg = spec["standardization"]
    min_hist = int(std_cfg["min_history"])
    tol = float(std_cfg.get("zero_dispersion_rel_tol", 1e-9))
    out = events.copy().reset_index(drop=True)
    out["release_timestamp_utc"] = utc_timestamps(out["release_timestamp_utc"])

    raw, status = [], []
    for _, r in out.iterrows():
        if _missing(r["forecast"]):
            raw.append(None); status.append("missing_forecast")
        elif _missing(r["actual"]):
            raw.append(None); status.append("missing_actual")
        else:
            raw.append(round(float(r["actual"]) - float(r["forecast"]), 10)); status.append(None)
    out["surprise_raw"] = pd.array(raw, dtype="Float64")
    out["surprise_sign"] = pd.array([None if v is None else int(np.sign(v)) for v in raw], dtype="Int64")

    rel, rel_status = [], []
    for v, st, (_, r) in zip(raw, status, out.iterrows()):
        if v is None:
            rel.append(None); rel_status.append(st)
        elif not fams[r["event_family"]].get("relative_surprise", False):
            rel.append(None); rel_status.append("not_meaningful_for_family")
        elif not (r["forecast"] > 0):
            rel.append(None); rel_status.append("forecast_not_positive")
        else:
            rel.append(v / r["forecast"]); rel_status.append("ok")
    out["surprise_relative"] = pd.array(rel, dtype="Float64")
    out["surprise_relative_status"] = rel_status

    std = [None] * len(out)
    std_status = list(status)
    n_hist = [None] * len(out)
    h_mean = [None] * len(out)
    h_std = [None] * len(out)
    h_thru = [None] * len(out)
    for fam, idx in out.groupby("event_family").groups.items():
        sub = out.loc[idx]
        eligible = sub[(sub["event_status"] == USABLE) & sub["surprise_raw"].notna() & sub["release_timestamp_utc"].notna()]
        e_ts = eligible["release_timestamp_utc"]
        e_val = eligible["surprise_raw"].astype(float)
        for i in idx:
            r = out.loc[i]
            if raw[i] is None:
                continue
            if r["event_status"] != USABLE or pd.isna(r["release_timestamp_utc"]):
                std_status[i] = "event_excluded"
                continue
            before = e_ts < r["release_timestamp_utc"]  # semantic comparison; equal timestamps excluded
            prior = e_val[before].to_numpy()
            n_hist[i] = int(len(prior))
            if len(prior):
                h_thru[i] = e_ts[before].max().isoformat()
            if len(prior) < min_hist:
                std_status[i] = "insufficient_history"
                continue
            mu, sd = float(prior.mean()), float(prior.std(ddof=1))
            h_mean[i], h_std[i] = mu, sd
            if not np.isfinite(sd) or sd <= tol * max(1.0, float(np.abs(prior).max())):
                std_status[i] = "zero_historical_dispersion"
                continue
            std[i] = (raw[i] - mu) / sd
            std_status[i] = "ok"
    out["surprise_std"] = pd.array(std, dtype="Float64")
    out["surprise_std_status"] = std_status
    out["surprise_std_n_history"] = pd.array(n_hist, dtype="Int64")
    out["surprise_std_hist_mean"] = pd.array(h_mean, dtype="Float64")
    out["surprise_std_hist_std"] = pd.array(h_std, dtype="Float64")
    out["surprise_std_history_through_utc"] = h_thru
    return out
