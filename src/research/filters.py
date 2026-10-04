"""Single source of truth for which rows enter research statistics.

Every diagnostic, quality denominator for usable research rows, baseline fit and evaluation goes
through these helpers -- no statistic re-implements its own inclusion rule. An event whose
`event_status` is not `usable` never contributes to correlations, sign means, quantiles, baseline
history or error metrics.

Statistical unit: the canonical row grain is event x symbol, but members of one simultaneous
release (same `release_id`, e.g. CPI m/m, CPI y/y, Core CPI m/m, Core CPI y/y at 08:30) share ONE
market response. The observational cluster for market responses is (release_id, symbol): any
statistic that combines families must aggregate or cluster by release_id
(`release_level`) and must not count simultaneous components as independent draws.
"""
from __future__ import annotations

import pandas as pd

from .events import USABLE

USABLE_WINDOW_STATUSES = ("ok", "ok_market_halt")


def usable_events(df: pd.DataFrame) -> pd.Series:
    return df["event_status"] == USABLE


def usable_response(df: pd.DataFrame, window: str) -> pd.Series:
    """Usable event AND usable window AND an observed value (window like 'post5m')."""
    return usable_events(df) & df[f"{window}_status"].isin(USABLE_WINDOW_STATUSES) & df[f"{window}_ret"].notna()


def usable_std_surprise(df: pd.DataFrame) -> pd.Series:
    return usable_events(df) & (df["surprise_std_status"] == "ok") & df["surprise_std"].notna()


def require_single_family(df: pd.DataFrame) -> None:
    """Guard for per-family statistics: family-specific numbers must never silently pool families
    (which would also pseudo-replicate shared release responses)."""
    fams = df["event_family"].dropna().unique()
    if len(fams) > 1:
        raise ValueError(f"per-family statistic received several families {sorted(fams)}; "
                         f"use release_level() for cross-family analysis")


def release_level(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (release_id, symbol): the market-response observational cluster. Surprise
    components of all member families are kept as a list."""
    keep = df[usable_events(df) & df["release_id"].notna()]
    g = keep.groupby(["release_id", "symbol"], sort=True)
    first = g.first()
    first["member_families"] = g["event_family"].apply(lambda s: ",".join(sorted(s)))
    first["n_member_events"] = g.size()
    return first.reset_index()
