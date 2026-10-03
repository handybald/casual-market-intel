"""Cross-provider comparison of overlapping 1-minute (or n-minute) bars.

Two stored datasets for the same symbol -- e.g. Massive (consolidated) vs
Alpaca/IEX (single venue) -- are aligned on exact bar-start timestamps and
compared. The report keeps three things strictly apart:

  1. compatibility -- can these two datasets be compared at all? Different
     timeframes or adjustment policies are REFUSED (IncompatibleComparisonError):
     a raw vs. split-adjusted price difference is a definition difference,
     not a provider discrepancy.
  2. hard_integrity_failures -- per-provider defects that make a dataset
     itself unusable regardless of the other one (naive or duplicate
     timestamps, non-finite values, impossible OHLC, non-positive prices,
     negative volume, misaligned bar starts).
  3. descriptive -- discrepancy METRICS (coverage, OHLC abs/rel
     differences, volume differences, return correlation, per-session
     coverage differences). These are measurements, not verdicts: two
     feeds can legitimately disagree (a single-venue feed prints a
     different last trade per minute and a fraction of the volume), so
     no tolerance here is treated as a pass/fail threshold.

Nothing is filled, interpolated or dropped to make the providers agree:
minutes present in only one provider are counted, not imputed, and
returns are only computed between consecutive minutes present in BOTH.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .market import nyse_sessions, timestamp_integrity

NY = ZoneInfo("America/New_York")
PRICE_FIELDS = ("open", "high", "low", "close")
_WORST_N = 10


class IncompatibleComparisonError(ValueError):
    """The two datasets measure different things; comparing them would
    produce a meaningless discrepancy report."""


class NoComparableDataError(ValueError):
    """At least one side has zero bars inside the requested window: the
    comparison cannot be performed. Not a provider discrepancy."""

    def __init__(self, empty_sides: List[str], start: dt.date, end: dt.date):
        self.empty_sides = empty_sides
        super().__init__(f"zero bars in requested window {start}..{end} for: {', '.join(empty_sides)}")


@dataclass(frozen=True)
class DatasetSide:
    label: str          # e.g. "massive", "alpaca-iex"
    source: str         # e.g. "MASSIVE"
    feed: str
    feed_scope: str     # see fetch/market_provider.py FEED_SCOPE_*
    adjustment: str
    timeframe: str


def _check_compatibility(a: DatasetSide, b: DatasetSide) -> Dict[str, Any]:
    if a.timeframe != b.timeframe:
        raise IncompatibleComparisonError(f"timeframe differs: {a.label}={a.timeframe} vs {b.label}={b.timeframe}")
    if a.adjustment != b.adjustment:
        raise IncompatibleComparisonError(
            f"adjustment policy differs: {a.label}={a.adjustment!r} vs {b.label}={b.adjustment!r} -- "
            f"price differences would reflect adjustment, not provider disagreement"
        )
    if (a.source, a.feed) == (b.source, b.feed):
        raise IncompatibleComparisonError(f"both sides are the same dataset ({a.source}/{a.feed})")
    notes = []
    volume_comparable = a.feed_scope == b.feed_scope
    if not volume_comparable:
        notes.append(
            f"feed scopes differ ({a.label}: {a.feed_scope}, {b.label}: {b.feed_scope}). A single-venue feed "
            f"aggregates only that venue's trades: its volume/transaction counts are a FRACTION of consolidated "
            f"volume, and its OHLC are that venue's prints. Volume differences below are expected, not errors."
        )
    return {"comparable": True, "volume_comparable": volume_comparable, "notes": notes}


def _integrity_failures(df: pd.DataFrame, timeframe_minutes: int) -> List[str]:
    issues: List[str] = []
    if df.empty:
        return issues
    naive, misaligned, ts = timestamp_integrity(df["timestamp_utc"], timeframe_minutes)
    if naive:
        issues.append(f"{naive} timezone-naive timestamps (canonical layer requires UTC-aware)")
        return issues
    dupes = int(ts.duplicated().sum())
    if dupes:
        issues.append(f"{dupes} duplicate timestamps")
    if misaligned:
        issues.append(f"{misaligned} bars not aligned to the {timeframe_minutes}-minute bar grid")
    vals = df[list(PRICE_FIELDS) + ["volume"]].apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(vals).all(axis=1)
    if (~finite).any():
        issues.append(f"{int((~finite).sum())} rows with null/non-finite OHLCV")
    v = vals[finite]
    bad_ohlc = (v.high < v.low) | (v.high < v.open) | (v.high < v.close) | (v.low > v.open) | (v.low > v.close)
    if bad_ohlc.any():
        issues.append(f"{int(bad_ohlc.sum())} rows with invalid OHLC relationships")
    nonpos = (v[list(PRICE_FIELDS)] <= 0).any(axis=1)
    if nonpos.any():
        issues.append(f"{int(nonpos.sum())} rows with zero/negative price")
    if (v.volume < 0).any():
        issues.append(f"{int((v.volume < 0).sum())} rows with negative volume")
    return issues


def _stats(x: pd.Series) -> Dict[str, Optional[float]]:
    x = pd.to_numeric(x, errors="coerce").dropna()
    if x.empty:
        return {"n": 0, "median": None, "p95": None, "max": None}
    return {
        "n": int(len(x)),
        "median": float(x.median()),
        "p95": float(x.quantile(0.95)),
        "max": float(x.max()),
    }


def _session_frame(start: dt.date, end: dt.date) -> pd.DataFrame:
    sched = nyse_sessions(start, end)
    out = pd.DataFrame({
        "open": pd.to_datetime(sched["market_open"], utc=True),
        "close": pd.to_datetime(sched["market_close"], utc=True),
    })
    out.index = [d.date() for d in sched.index]
    return out


def _is_regular(ts: pd.Series, sessions: pd.DataFrame) -> pd.Series:
    """True when a bar starts inside its NY date's real NYSE regular
    session (holiday- and early-close-aware), False otherwise."""
    if ts.empty:
        return pd.Series([], dtype=bool, index=ts.index)
    ny_dates = ts.dt.tz_convert(NY).dt.date
    opens = ny_dates.map(sessions["open"].to_dict())
    closes = ny_dates.map(sessions["close"].to_dict())
    opens = pd.to_datetime(opens, utc=True)
    closes = pd.to_datetime(closes, utc=True)
    return ((ts >= opens) & (ts < closes)).fillna(False).astype(bool)


def bars_in_window(df: Optional[pd.DataFrame], start: dt.date, end: dt.date) -> int:
    if df is None or df.empty:
        return 0
    return int(len(_window(df.assign(timestamp_utc=pd.to_datetime(df["timestamp_utc"], utc=True)), start, end)))


def _window(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    lo = pd.Timestamp(dt.datetime.combine(start, dt.time(0), tzinfo=NY))
    hi = pd.Timestamp(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=NY))
    return df[(df["timestamp_utc"] >= lo) & (df["timestamp_utc"] < hi)]


def compare_market_bars(
    a_df: pd.DataFrame,
    b_df: pd.DataFrame,
    a: DatasetSide,
    b: DatasetSide,
    symbol: str,
    start: dt.date,
    end: dt.date,
    timeframe_minutes: int = 1,
) -> Dict[str, Any]:
    """Compare two providers' bars for `symbol` over NYSE dates [start, end].
    Raises IncompatibleComparisonError when the datasets are not
    comparable, and NoComparableDataError when either side has no bar in
    the requested window (a comparison over nothing is not a result).
    Returns a JSON-serialisable report (see module docstring)."""
    compat = _check_compatibility(a, b)
    empty = [side.label for side, df in ((a, a_df), (b, b_df)) if bars_in_window(df, start, end) == 0]
    if empty:
        raise NoComparableDataError(empty, start, end)

    report: Dict[str, Any] = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "providers": {"a": asdict(a), "b": asdict(b)},
        "compatibility": compat,
        "hard_integrity_failures": {
            a.label: _integrity_failures(a_df, timeframe_minutes),
            b.label: _integrity_failures(b_df, timeframe_minutes),
        },
    }
    report["integrity_ok"] = not any(report["hard_integrity_failures"].values())
    if not report["integrity_ok"]:
        # Discrepancy metrics over structurally broken data would be
        # misleading; report the defects only.
        report["descriptive"] = None
        return report

    a_w = _window(a_df, start, end).set_index("timestamp_utc").sort_index()
    b_w = _window(b_df, start, end).set_index("timestamp_utc").sort_index()
    sessions = _session_frame(start, end)

    a_reg = _is_regular(a_w.index.to_series(), sessions)
    b_reg = _is_regular(b_w.index.to_series(), sessions)
    both_idx = a_w.index.intersection(b_w.index)
    only_a = a_w.index.difference(b_w.index)
    only_b = b_w.index.difference(a_w.index)
    union = a_w.index.union(b_w.index)

    def _split(idx: pd.Index) -> Dict[str, int]:
        reg = _is_regular(idx.to_series(), sessions) if len(idx) else pd.Series([], dtype=bool)
        return {"total": int(len(idx)), "regular_session": int(reg.sum()), "extended_hours": int((~reg).sum())}

    expected_regular = int(sum((r.close - r.open) / pd.Timedelta(minutes=timeframe_minutes) for r in sessions.itertuples()))
    coverage = {
        "bars": {a.label: _split(a_w.index), b.label: _split(b_w.index)},
        "in_both": _split(both_idx),
        f"only_in_{a.label}": _split(only_a),
        f"only_in_{b.label}": _split(only_b),
        "overlap_over_union": (len(both_idx) / len(union)) if len(union) else None,
        f"overlap_over_{a.label}": (len(both_idx) / len(a_w)) if len(a_w) else None,
        f"overlap_over_{b.label}": (len(both_idx) / len(b_w)) if len(b_w) else None,
        "expected_regular_session_bars": expected_regular,
        f"regular_session_fill_{a.label}": (int(a_reg.sum()) / expected_regular) if expected_regular else None,
        f"regular_session_fill_{b.label}": (int(b_reg.sum()) / expected_regular) if expected_regular else None,
    }

    # Per-session presence: which provider has data on which real NYSE session.
    a_dates = set(a_w.index[a_reg.values].tz_convert(NY).date) if len(a_w) else set()
    b_dates = set(b_w.index[b_reg.values].tz_convert(NY).date) if len(b_w) else set()
    session_dates = list(sessions.index)
    session_presence = {
        "nyse_sessions": len(session_dates),
        f"sessions_with_regular_bars_only_in_{a.label}": sorted(d.isoformat() for d in session_dates if d in a_dates and d not in b_dates),
        f"sessions_with_regular_bars_only_in_{b.label}": sorted(d.isoformat() for d in session_dates if d in b_dates and d not in a_dates),
        "sessions_with_no_regular_bars_in_either": sorted(d.isoformat() for d in session_dates if d not in a_dates and d not in b_dates),
    }

    descriptive: Dict[str, Any] = {"coverage": coverage, "session_presence": session_presence}

    if len(both_idx):
        pa = a_w.loc[both_idx]
        pb = b_w.loc[both_idx]
        reg_both = _is_regular(both_idx.to_series(), sessions).values
        prices: Dict[str, Any] = {}
        for f in PRICE_FIELDS:
            x, y = pa[f].astype(float), pb[f].astype(float)
            absd = (x - y).abs()
            reld = absd / ((x.abs() + y.abs()) / 2.0)
            prices[f] = {
                "abs_diff": {"all": _stats(absd), "regular_session": _stats(absd[reg_both]), "extended_hours": _stats(absd[~reg_both])},
                "rel_diff": {"all": _stats(reld), "regular_session": _stats(reld[reg_both]), "extended_hours": _stats(reld[~reg_both])},
                "exact_equal_fraction": float((absd == 0).mean()),
            }
        descriptive["price_differences"] = prices

        va, vb = pa["volume"].astype(float), pb["volume"].astype(float)
        pct = ((va - vb) / vb).where(vb > 0)
        ratio = (va / vb).where(vb > 0)
        descriptive["volume_differences"] = {
            "comparable_as_full_market": compat["volume_comparable"],
            "abs_diff": _stats((va - vb).abs()),
            f"pct_diff_{a.label}_vs_{b.label}": _stats(pct.abs()),
            f"ratio_{a.label}_over_{b.label}": _stats(ratio),
            "total_volume": {a.label: float(va.sum()), b.label: float(vb.sum())},
        }

        # Close-to-close log returns only between CONSECUTIVE bars present
        # in BOTH providers (never across a gap, never across sessions).
        step = pd.Timedelta(minutes=timeframe_minutes)
        idx = both_idx.sort_values()
        prev = idx - step
        has_prev = prev.isin(idx)
        same_day = idx.tz_convert(NY).date == prev.tz_convert(NY).date
        keep = has_prev & same_day
        cur_t, prev_t = idx[keep], prev[keep]
        ra = np.log(a_w.loc[cur_t, "close"].values.astype(float) / a_w.loc[prev_t, "close"].values.astype(float))
        rb = np.log(b_w.loc[cur_t, "close"].values.astype(float) / b_w.loc[prev_t, "close"].values.astype(float))
        # A return is "regular-session" only if BOTH endpoints are regular-
        # session bars (09:29 -> 09:30 spans the open and is not).
        reg_ret = (_is_regular(cur_t.to_series(), sessions).values
                   & _is_regular(prev_t.to_series(), sessions).values)

        def _corr(x: np.ndarray, y: np.ndarray) -> Optional[float]:
            if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
                return None
            return float(np.corrcoef(x, y)[0, 1])

        descriptive["return_correlation"] = {
            "definition": "Pearson correlation of close-to-close log returns over consecutive bars present in both "
                          "providers on the same NY date; 'regular_session' requires both bars of each return to be "
                          "inside the NYSE regular session",
            "all": {"n": int(len(ra)), "corr": _corr(ra, rb)},
            "regular_session": {"n": int(reg_ret.sum()), "corr": _corr(ra[reg_ret], rb[reg_ret])},
        }

        close_abs = (pa["close"].astype(float) - pb["close"].astype(float)).abs().sort_values(ascending=False)
        descriptive["largest_close_differences"] = [
            {"timestamp_utc": t.isoformat(), f"close_{a.label}": float(pa.at[t, "close"]),
             f"close_{b.label}": float(pb.at[t, "close"]), "abs_diff": float(d)}
            for t, d in close_abs.head(_WORST_N).items()
        ]
    else:
        descriptive["price_differences"] = None
        descriptive["volume_differences"] = None
        descriptive["return_correlation"] = None
        descriptive["largest_close_differences"] = []

    report["descriptive"] = descriptive
    return report
