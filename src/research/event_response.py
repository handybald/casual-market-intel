"""Release-aligned market response windows on canonical Alpaca SIP 1-minute bars.

BAR SEMANTICS. A canonical bar stamped T covers [T, T+1m); its close is the last eligible trade
price of that minute. Price "at" instant x is therefore the close of the bar starting x-1m.

ALIGNMENT. Forex Factory gives a scheduled release minute (`release_time_precision =
scheduled_minute`; no seconds are invented). For release time t:
  t_floor        = t truncated to the minute
  release bar    = the bar starting t_floor (the release-containing minute)
  reference bar  = the bar starting t_floor-1m: last bar completed before the release;
                   its close is the pre-release reference price P(t)
  first fully post-release bar = t_floor+1m when t has seconds (the release bar straddles
                   the release: flagged `release_bar_straddles_release`); for a minute-aligned
                   t the release bar itself is already wholly post-release
Because the true publication second within the scheduled minute is unknown, the release bar
may contain a few seconds of pre-publication trading; that uncertainty is inherent to
minute-precision timing and is not hidden.

WINDOWS (minutes; config `windows`):
  post h : anchor = reference bar (price at t); bars t_floor .. t_floor+(h-1)m. post1 == the
           release bar, so the release-minute response is post1 (plus `release_*` volume fields).
  pre k  : anchor = bar starting t_floor-(k+1)m; bars t_floor-k m .. t_floor-1m (ends at t).

SUFFICIENCY. A window's features are computed only when its anchor bar and every expected bar
exist. Missing bars are never treated as zero return or zero volume and nothing is filled.
Minutes inside registered market_wide_halt / exchange_closed / legitimate_no_trade intervals are
not expected (status `ok_market_halt`, tagged market_halt_in_window: the nominal clock horizon
then spans less traded time). Every window queries the known-exception registry; a provider
gap makes the window `provider_gap_in_window` with null features.

FEATURES per window (returns relative to the anchor close A, over the window's bars):
  ret, logret              C_last/A - 1, ln(C_last/A)
  high_exc, low_exc        max(high)/A - 1, min(low)/A - 1
  mfe, mae                 relative to the window's own net move direction d = sign(ret):
                           mfe = d*(extreme in d)/A - d, mae = largest move against d (>= 0 when
                           the window ever traded against its net direction); null when ret == 0
  range                    (max high - min low)/A
  rv_abs, rv_sq            sum |r_i| and sum r_i^2 of consecutive bar log returns (r_1 vs A)
  volume                   summed bar volume (anchor excluded)
  volume_rel               volume / mean volume over up to `volume_baseline_sessions` VALID
                           COMPARABLE historical windows, found by searching backward (at most
                           `volume_baseline_max_lookback_sessions` sessions). A candidate is valid
                           only if it is strictly earlier, covers the same local clock minutes
                           with the SAME per-minute session classification (pre-market / regular
                           / after-hours under that day's schedule -- an early-close day's
                           after-hours never stands in for regular-session minutes), has no
                           known-exception registry hit (provider gap, halt, closure, ...) and
                           has every bar. Fewer than `volume_baseline_min_sessions` valid
                           windows -> null with status.

mfe / mae (and every post-window metric) are EX-POST RESPONSE CHARACTERIZATIONS: they depend on
the realized direction and path after the release and are never available before it.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..data.market_exceptions import EXCLUDED_FROM_EXPECTED, ExceptionClass, MarketExceptionRegistry
from ..data.validation.market import nyse_sessions

NY = ZoneInfo("America/New_York")
MIN_NS = 60_000_000_000
METRICS = ["ret", "logret", "high_exc", "low_exc", "mfe", "mae", "range", "rv_abs", "rv_sq", "volume", "volume_rel"]

EVENT_EXCLUDED = "event_excluded"   # set by the dataset assembly for releases with no usable event
OK = "ok"
OK_MARKET_HALT = "ok_market_halt"
INSUFFICIENT = "insufficient_market_window"
PROVIDER_GAP = "provider_gap_in_window"
EXCHANGE_CLOSED = "exchange_closed"
PROVISIONAL = "provisional_market_data"
NO_RELEASE_TIME = "no_release_time"
USABLE_WINDOW_STATUSES = (OK, OK_MARKET_HALT)


def canonical_utc_ns(values: pd.Series) -> np.ndarray:
    """Timezone-aware bar timestamps -> int64 nanoseconds since the epoch (UTC).

    Accepts datetime64[ns|us|ms|s] with any timezone, and tz-aware Python datetimes / pandas
    Timestamps. A naive value is never assumed to be UTC: it raises. The conversion goes through
    semantic timestamps and an explicit `as_unit("ns")`, never a raw integer reinterpretation."""
    s = pd.Series(values)
    if pd.api.types.is_datetime64_any_dtype(s.dtype):
        if not isinstance(s.dtype, pd.DatetimeTZDtype):
            raise ValueError("bar timestamps are timezone-naive; canonical market time must be tz-aware UTC")
    else:
        naive = [v for v in s if v is not None and not pd.isna(v) and pd.Timestamp(v).tzinfo is None]
        if naive:
            raise ValueError(f"{len(naive)} timezone-naive bar timestamp(s); canonical market time must be tz-aware UTC")
    utc = pd.to_datetime(s, utc=True)
    if utc.isna().any():
        raise ValueError("null bar timestamp")
    return utc.dt.as_unit("ns").astype("int64").to_numpy()


class SymbolBars:
    """Sorted canonical bars of one symbol with O(log n) window lookups."""

    def __init__(self, df: pd.DataFrame):
        df = df.sort_values("timestamp_utc", kind="mergesort")
        # Canonical int64 NANOSECONDS since the epoch (UTC), whatever the input storage resolution:
        # every lookup below works in ns, so the representation is normalized here, once.
        self.ts = canonical_utc_ns(df["timestamp_utc"])
        if len(self.ts) and (np.diff(self.ts) <= 0).any():
            raise ValueError("bars must have strictly increasing, unique timestamps")
        self.o, self.h, self.l, self.c = (df[k].astype(float).to_numpy() for k in ("open", "high", "low", "close"))
        self.v = df["volume"].astype(float).to_numpy()
        self.cumv = np.concatenate([[0.0], np.cumsum(self.v)])

    def index_of(self, t_ns: int) -> Optional[int]:
        i = int(np.searchsorted(self.ts, t_ns))
        return i if i < len(self.ts) and self.ts[i] == t_ns else None

    def count_and_volume(self, start_ns: int, n: int) -> Tuple[int, float]:
        i0 = int(np.searchsorted(self.ts, start_ns))
        i1 = int(np.searchsorted(self.ts, start_ns + n * MIN_NS))
        return i1 - i0, float(self.cumv[i1] - self.cumv[i0])


def session_state(t: pd.Timestamp, sched: pd.DataFrame) -> str:
    d = t.tz_convert(NY).date()
    if d not in sched.index:
        return "closed_day"
    o, c = sched.loc[d, "open"], sched.loc[d, "close"]
    local = t.tz_convert(NY)
    four = pd.Timestamp(dt.datetime.combine(d, dt.time(4), tzinfo=NY))
    eight = pd.Timestamp(dt.datetime.combine(d, dt.time(20), tzinfo=NY))
    if o <= t < c:
        return "regular"
    if four <= local < o:
        return "premarket"
    if c <= t < eight:
        return "after_hours"
    return "overnight"


def calendar_frame(start: dt.date, end: dt.date) -> pd.DataFrame:
    s = nyse_sessions(start, end)
    frame = pd.DataFrame({"open": pd.to_datetime(s["market_open"], utc=True),
                          "close": pd.to_datetime(s["market_close"], utc=True)}, index=[d.date() for d in s.index])
    frame.attrs["dates"] = sorted(frame.index)  # for O(log n) "previous sessions" lookups
    return frame


def window_specs(spec: Dict[str, Any]) -> List[Tuple[str, str, int]]:
    w = spec["windows"]
    return ([(f"pre{k}m", "pre", int(k)) for k in w["pre_minutes"]]
            + [(f"post{h}m", "post", int(h)) for h in w["post_minutes"]])


def _window_geometry(t_floor_ns: int, side: str, n: int) -> Tuple[int, List[int]]:
    if side == "post":
        return t_floor_ns - MIN_NS, [t_floor_ns + i * MIN_NS for i in range(n)]
    return t_floor_ns - (n + 1) * MIN_NS, [t_floor_ns - (n - i) * MIN_NS for i in range(n)]


def compute_release_windows(
    release_ts: Optional[pd.Timestamp],
    symbol: str,
    bars: SymbolBars,
    registry: MarketExceptionRegistry,
    spec: Dict[str, Any],
    sched: pd.DataFrame,
    research_end: dt.date,
    provider: str = "alpaca",
    feed: str = "sip",
) -> List[Dict[str, Any]]:
    """One row per window for one (release, symbol)."""
    specs = window_specs(spec)
    base = {"symbol": symbol, "release_id": release_ts.isoformat() if release_ts is not None and not pd.isna(release_ts) else None}
    if release_ts is None or pd.isna(release_ts):
        return [{**base, "window": name, "side": side, "minutes": n, "status": NO_RELEASE_TIME} for name, side, n in specs]

    t_ns = int(release_ts.value)
    t_floor_ns = t_ns - (t_ns % MIN_NS)
    aligned = t_floor_ns == t_ns
    state = session_state(release_ts, sched)
    rows = []
    for name, side, n in specs:
        anchor_ns, bar_ns = _window_geometry(t_floor_ns, side, n)
        span_start = pd.Timestamp(anchor_ns, tz="UTC")
        span_end = pd.Timestamp(bar_ns[-1] + MIN_NS, tz="UTC")
        q = registry.query_window(symbol, span_start.to_pydatetime(), span_end.to_pydatetime(), provider=provider, feed=feed)
        row: Dict[str, Any] = {
            **base, "window": name, "side": side, "minutes": n,
            "release_timestamp_minute_aligned": aligned, "release_bar_straddles_release": not aligned,
            "release_session_state": state,
            "anchor_bar_utc": span_start.isoformat(), "first_bar_utc": pd.Timestamp(bar_ns[0], tz="UTC").isoformat(),
            "last_bar_utc": pd.Timestamp(bar_ns[-1], tz="UTC").isoformat(),
            "exception_ids": ",".join(q.entry_ids) or None, "exception_tags": ",".join(q.tags) or None,
            "has_provider_gap": q.has_provider_gap, "has_market_halt": q.has_market_halt,
        }
        for m in METRICS:
            row[m] = None
        row["volume_rel_status"] = None
        row["volume_rel_n_windows"] = None

        not_expected = set()
        for hit in q.hits:
            if hit.entry.classification in EXCLUDED_FROM_EXPECTED:
                lo, hi = int(hit.entry.start_utc.timestamp()) * 10**9, int(hit.entry.end_utc.timestamp()) * 10**9
                not_expected |= {b for b in [anchor_ns] + bar_ns if lo <= b < hi}
        expected = [b for b in bar_ns if b not in not_expected]
        idx = [bars.index_of(b) for b in expected]
        a_idx = bars.index_of(anchor_ns)
        row["n_expected_bars"] = len(expected) + 1
        row["n_observed_bars"] = sum(i is not None for i in idx) + (a_idx is not None)
        row["coverage"] = row["n_observed_bars"] / row["n_expected_bars"]

        end_date = pd.Timestamp(bar_ns[-1], tz="UTC").tz_convert(NY).date()
        if q.has_provider_gap:
            row["status"] = PROVIDER_GAP
        elif state == "closed_day" or q.has(ExceptionClass.EXCHANGE_CLOSED):
            row["status"] = EXCHANGE_CLOSED
        elif end_date > research_end:
            row["status"] = PROVISIONAL
        elif a_idx is None or any(i is None for i in idx) or not expected:
            row["status"] = INSUFFICIENT
        else:
            row["status"] = OK_MARKET_HALT if q.has_market_halt else OK
            row.update(_metrics(bars, a_idx, idx))
            row.update(_volume_rel(bars, sched, expected, row["volume"], spec, registry, symbol, provider, feed))
        rows.append(row)
    return rows


def _metrics(bars: SymbolBars, a: int, idx: Sequence[int]) -> Dict[str, Any]:
    A = bars.c[a]
    closes = bars.c[list(idx)]
    highs, lows = bars.h[list(idx)], bars.l[list(idx)]
    ret = closes[-1] / A - 1.0
    hi, lo = highs.max() / A - 1.0, lows.min() / A - 1.0
    d = int(np.sign(ret))
    mfe = mae = None
    if d > 0:
        mfe, mae = hi, max(0.0, -lo)
    elif d < 0:
        mfe, mae = -lo, max(0.0, hi)
    r = np.diff(np.log(np.concatenate([[A], closes])))
    return {"ret": float(ret), "logret": float(math.log(closes[-1] / A)), "high_exc": float(hi), "low_exc": float(lo),
            "mfe": mfe, "mae": mae, "range": float((highs.max() - lows.min()) / A),
            "rv_abs": float(np.abs(r).sum()), "rv_sq": float((r ** 2).sum()), "volume": float(bars.v[list(idx)].sum())}


def _session_labels(minutes_ns: Sequence[int], open_utc: pd.Timestamp, close_utc: pd.Timestamp) -> Tuple[str, ...]:
    o, c = int(open_utc.value), int(close_utc.value)
    return tuple("pre" if m < o else "regular" if m < c else "after" for m in minutes_ns)


def _volume_rel(bars: SymbolBars, sched: pd.DataFrame, expected_ns: List[int], volume: float, spec,
                registry: MarketExceptionRegistry, symbol: str, provider: str, feed: str) -> Dict[str, Any]:
    w = spec["windows"]
    start = pd.Timestamp(expected_ns[0], tz="UTC")
    n = int((expected_ns[-1] - expected_ns[0]) // MIN_NS) + 1
    if n != len(expected_ns):  # window with excluded (halt) minutes: no like-for-like baseline
        return {"volume_rel": None, "volume_rel_status": "not_comparable_excluded_minutes", "volume_rel_n_windows": 0}
    local_t = start.tz_convert(NY).time()
    d0 = start.tz_convert(NY).date()
    if d0 not in sched.index:
        return {"volume_rel": None, "volume_rel_status": "target_session_closed", "volume_rel_n_windows": 0}
    target_labels = _session_labels(expected_ns, sched.loc[d0, "open"], sched.loc[d0, "close"])
    dates = sched.attrs.get("dates") or sorted(sched.index)
    k = bisect.bisect_left(dates, d0)  # candidates are strictly earlier sessions only
    want, lookback = int(w["volume_baseline_sessions"]), int(w.get("volume_baseline_max_lookback_sessions", 60))
    vols = []
    for d in reversed(dates[max(0, k - lookback):k]):
        s = pd.Timestamp(dt.datetime.combine(d, local_t, tzinfo=NY)).tz_convert("UTC")
        mins = [int(s.value) + i * MIN_NS for i in range(n)]
        if _session_labels(mins, sched.loc[d, "open"], sched.loc[d, "close"]) != target_labels:
            continue  # e.g. early close: different session structure at these clock minutes
        if registry.query_window(symbol, s.to_pydatetime(), (s + pd.Timedelta(minutes=n)).to_pydatetime(),
                                 provider=provider, feed=feed).any:
            continue  # provider gap / halt / closure / registered no-trade interval
        cnt, vol = bars.count_and_volume(int(s.value), n)
        if cnt != n:
            continue  # incomplete coverage: missing bars are never treated as zero volume
        vols.append(vol)
        if len(vols) == want:
            break
    if len(vols) < int(w["volume_baseline_min_sessions"]):
        return {"volume_rel": None, "volume_rel_status": "insufficient_comparable_history", "volume_rel_n_windows": len(vols)}
    base = float(np.mean(vols))
    if base <= 0:
        return {"volume_rel": None, "volume_rel_status": "zero_baseline", "volume_rel_n_windows": len(vols)}
    return {"volume_rel": volume / base, "volume_rel_status": "ok", "volume_rel_n_windows": len(vols)}
