"""Macro release -> market response features from 1-minute bars (no models, no indicators).

BAR CONVENTION. Canonical `timestamp_utc` is the START of a one-minute bar (Massive and Alpaca both document this): the bar stamped 12:30:00 covers
12:30:00-12:30:59 and its `close` is the last price of that minute. Bars exist only for minutes that traded, so
extended-hours data (04:00-20:00 ET) is sparse-capable and is NEVER filtered to regular hours or interpolated.

EVENT TIME. Only a TRUSTED release timestamp `t` is used (Forex Factory CONFIRMED, or MQL5 under a validated
broker timezone -- decided upstream by validation/macro_events.py). With seconds-precision `t`:
  t_floor = t truncated to the minute      (end of the pre-release window: bars starting < t_floor are complete)
  T       = t rounded UP to the minute     (first bar that is wholly post-release)
For the normal minute-aligned t (e.g. 08:30:00 ET) t_floor == T == t. A bar that straddles a non-aligned t is
excluded from both sides, and post-window volatility is left null (its reference bar would straddle t).

DEFINITIONS (bar_x = the bar starting at x; close_x its close):
  baseline_price     close of the last completed bar strictly before t: bar_(t_floor-1m), or if that minute did not
                     trade, the most recent earlier bar within `max_baseline_lookback_minutes`.
  ret_post_{k}m      close_(T+(k-1)m) / baseline_price - 1         (k in 1,5,15,30,60)  -> +1m = the bar starting at T
  ret_pre_{k}m       close_(b) / close_(b-k m) - 1, b = baseline bar   (k in 5,30)     -> the k minutes ending at t
  direction_{k}m     sign(ret_post_{k}m): -1 / 0 / +1, with |ret| <= direction_tolerance counted as 0
  max_up_{H}m        max(high over bars T .. T+(H-1)m) / baseline_price - 1      (H in 5,30,60)
  max_down_{H}m      min(low  over the same bars)      / baseline_price - 1      (raw: normally <= 0, may be > 0)
  realized_vol_pre_30m   sqrt(sum r_i^2), r_i = ln(close_i / close_(i-1m)) over bars t_floor-30m .. t_floor-1m
  realized_vol_post_{30,60}m   same over bars T .. T+(H-1)m, the first return measured against close_(T-1m)
  volume_pre_30m     sum of volume over bars t_floor-30m .. t_floor-1m
  volume_post_{30,60}m   sum of volume over bars T .. T+(H-1)m
  volume_ratio_30m   volume_post_30m / volume_pre_30m (null, with a status, if pre volume is zero/missing)
Simple returns, no annualization. A feature is computed only if EVERY bar it needs exists; otherwise it is null
(nothing is interpolated or forward-filled). Every feature reads only bars up to T+H-1m for its horizon H
(`BarStore` records accessed timestamps so tests can prove it).

WINDOW: t-30m .. t+60m = 30 pre bars + 60 post bars -> bars_expected = 90.
`surprise_raw` = actual - provider_forecast (units validated upstream); `surprise_z_prior_only` is a null placeholder:
a normalized surprise must later use only observations strictly before the event.
PROVENANCE. Bars come from ONE stored provider dataset (provider + feed + adjustment); every output row carries
`market_data_source/feed/feed_scope/adjustment` taken from the stored ROWS (plus `market_data_provenance_basis`), and the
loader refuses a file whose provenance is missing, null, mixed, or not the requested dataset (only pre-provider
Massive files are admitted without feed columns, explicitly labelled `legacy_massive_unlabeled`). A response computed from single-venue (e.g. IEX) bars is a different measurement from
one computed from consolidated bars -- volume features especially -- and must never be pooled with it unknowingly.
`release_session` (America/New_York) is diagnostic only and never alters timestamps; weekends are CLOSED, exchange
holidays are not modelled.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
MIN = dt.timedelta(minutes=1)
UTC = dt.timezone.utc

OK = "OK"
NO_TRUSTED_EVENT_TIMESTAMP = "NO_TRUSTED_EVENT_TIMESTAMP"
NO_BASELINE_BAR = "NO_BASELINE_BAR"
INSUFFICIENT_PRE_WINDOW = "INSUFFICIENT_PRE_WINDOW"
INSUFFICIENT_POST_WINDOW = "INSUFFICIENT_POST_WINDOW"
MISSING_MARKET_DATA = "MISSING_MARKET_DATA"

POST_HORIZONS = (1, 5, 15, 30, 60)
PRE_RETURN_HORIZONS = (5, 30)
EXCURSION_HORIZONS = (5, 30, 60)


@dataclass(frozen=True)
class ResponseConfig:
    pre_minutes: int = 30
    post_minutes: int = 60
    max_baseline_lookback_minutes: int = 5
    direction_tolerance: float = 1e-12


# --------------------------------------------------------------------------- time helpers
def to_utc(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def floor_minute(t: dt.datetime) -> dt.datetime:
    return t.replace(second=0, microsecond=0)


def ceil_minute(t: dt.datetime) -> dt.datetime:
    f = floor_minute(t)
    return f if f == t else f + MIN


def classify_session(t: dt.datetime) -> str:
    """PREMARKET 04:00-09:30, REGULAR 09:30-16:00, AFTER_HOURS 16:00-20:00 (America/New_York, start inclusive);
    everything else, and weekends, CLOSED. Diagnostic only."""
    local = to_utc(t).astimezone(NY)
    if local.weekday() >= 5:
        return "CLOSED"
    hm = local.hour * 60 + local.minute
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "PREMARKET"
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "REGULAR"
    if 16 * 60 <= hm < 20 * 60:
        return "AFTER_HOURS"
    return "CLOSED"


# --------------------------------------------------------------------------- bars
class BarStore:
    """Minute-bar lookup by bar START time that records every timestamp it is asked for, so tests can prove a
    feature never reads beyond its horizon."""

    def __init__(self, bars: Optional[pd.DataFrame]):
        self._rows: Dict[dt.datetime, Tuple[float, float, float, float, float]] = {}
        if bars is not None and len(bars):
            idx = bars.index
            if idx.has_duplicates:
                raise ValueError("duplicate bar timestamps in market data")
            for ts, o, h, l, c, v in zip(idx.to_pydatetime(), bars["open"], bars["high"], bars["low"], bars["close"], bars["volume"]):
                self._rows[ts] = (float(o), float(h), float(l), float(c), float(v))
        self.accessed: Set[dt.datetime] = set()

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, ts: dt.datetime) -> Optional[Tuple[float, float, float, float, float]]:
        self.accessed.add(ts)
        return self._rows.get(ts)

    def has_any(self, start: dt.datetime, end_inclusive: dt.datetime) -> bool:
        return any(start <= ts <= end_inclusive for ts in self._rows)   # coverage probe, not a feature


# Row provenance every provider-aware interim file must carry, non-null and single-valued.
PROVENANCE_COLUMNS = ("source", "feed", "feed_scope", "adjustment", "timeframe")
# Columns the PRE-provider normalizer (Massive-only era) wrote; such files have no feed/feed_scope at all.
LEGACY_MASSIVE_COLUMNS = ("source", "adjustment", "timeframe")
PROVENANCE_BASIS_ROWS = "row_columns"
PROVENANCE_BASIS_LEGACY_MASSIVE = "legacy_massive_unlabeled"


class MarketProvenanceError(ValueError):
    """Stored bars whose provenance is missing, null, mixed, or not the requested dataset."""


def _single_value(df: pd.DataFrame, col: str, symbol: str, root: Path) -> str:
    nulls = int(df[col].isna().sum())
    if nulls:
        raise MarketProvenanceError(f"{symbol}: {nulls} row(s) with null {col!r} in {root} -- provenance unknown, refusing")
    values = sorted(df[col].astype(str).unique())
    if len(values) > 1:
        raise MarketProvenanceError(f"{symbol}: mixed {col} values {values} in {root} -- refusing to combine")
    return values[0]


def check_market_provenance(df: pd.DataFrame, symbol: str, root: Path, expected: Dict[str, str],
                            legacy_unlabeled_source: Optional[str] = None) -> Dict[str, str]:
    """Row provenance of a stored interim frame, validated against `expected` ({source, feed, feed_scope,
    adjustment, timeframe}). Every row must carry all PROVENANCE_COLUMNS, non-null and single-valued, equal to
    `expected` -- the expected values never fill in a missing or null cell.

    The ONLY exception: `legacy_unlabeled_source` (set by a provider that declares it, i.e. Massive) admits files
    written before feed provenance existed -- `feed` and `feed_scope` columns entirely ABSENT (not null), every
    LEGACY_MASSIVE_COLUMNS cell present and matching, and `source` equal to that provider. Their feed is then the
    provider's declared feed, and the returned `provenance_basis` says so explicitly."""
    have = [c for c in PROVENANCE_COLUMNS if c in df.columns]
    if len(have) == len(PROVENANCE_COLUMNS):
        basis, cols = PROVENANCE_BASIS_ROWS, PROVENANCE_COLUMNS
    elif (legacy_unlabeled_source is not None and "feed" not in df.columns and "feed_scope" not in df.columns
          and all(c in df.columns for c in LEGACY_MASSIVE_COLUMNS)):
        basis, cols = PROVENANCE_BASIS_LEGACY_MASSIVE, LEGACY_MASSIVE_COLUMNS
    else:
        missing = [c for c in PROVENANCE_COLUMNS if c not in df.columns]
        raise MarketProvenanceError(f"{symbol}: provenance column(s) {missing} missing in {root} -- refusing")
    found = {c: _single_value(df, c, symbol, root) for c in cols}
    if basis == PROVENANCE_BASIS_LEGACY_MASSIVE and found["source"] != legacy_unlabeled_source:
        raise MarketProvenanceError(
            f"{symbol}: unlabeled-feed file in {root} has source {found['source']!r}; the legacy exception only "
            f"covers {legacy_unlabeled_source!r}")
    for c in cols:
        if c in expected and found[c] != str(expected[c]):
            raise MarketProvenanceError(f"{symbol}: stored {c}={found[c]!r} in {root}, expected {expected[c]!r}")
    prov = dict(found)
    if basis == PROVENANCE_BASIS_LEGACY_MASSIVE:
        prov.update(feed=expected["feed"], feed_scope=expected["feed_scope"])
    prov["provenance_basis"] = basis
    return prov


def load_market_dataset(symbol: str, start: dt.datetime, end: dt.datetime, root: Path, expected: Dict[str, str],
                        legacy_unlabeled_source: Optional[str] = None
                        ) -> Tuple[Optional[pd.DataFrame], Optional[Dict[str, str]]]:
    """(bars, provenance) for [start, end] from <root>/<SYMBOL>/1min/<adjustment>/<YEAR>.parquet, where <root> is
    ONE provider dataset's interim root (e.g. data/interim/massive, data/interim/alpaca/iex) and <adjustment> is
    expected["adjustment"]. Bars are indexed by UTC bar start. (None, None) if no file exists. Provenance is
    checked over EVERY row read (not just the window) by `check_market_provenance`; local files only."""
    frames = []
    for year in range(start.year, end.year + 1):
        path = Path(root) / symbol / "1min" / expected["adjustment"] / f"{year}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        return None, None
    df = pd.concat(frames, ignore_index=True)
    prov = check_market_provenance(df, symbol, Path(root), expected, legacy_unlabeled_source)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df = df[(df["timestamp_utc"] >= start) & (df["timestamp_utc"] <= end)]
    return df[["timestamp_utc", "open", "high", "low", "close", "volume"]].set_index("timestamp_utc").sort_index(), prov


MASSIVE_EXPECTED_PROVENANCE = {"source": "MASSIVE", "feed": "consolidated", "feed_scope": "consolidated",
                               "adjustment": "raw", "timeframe": "1min"}


def load_massive_bars(symbol: str, start: dt.datetime, end: dt.datetime, root: Path) -> Optional[pd.DataFrame]:
    """Backward-compatible: raw 1-minute Massive bars from a Massive interim root (data/interim/massive), with the
    explicit legacy-Massive provenance exception."""
    return load_market_dataset(symbol, start, end, root, MASSIVE_EXPECTED_PROVENANCE, legacy_unlabeled_source="MASSIVE")[0]


# --------------------------------------------------------------------------- features (each reads only what it needs)
def find_baseline(store: BarStore, t_floor: dt.datetime, cfg: ResponseConfig) -> Optional[Tuple[dt.datetime, float]]:
    for back in range(1, cfg.max_baseline_lookback_minutes + 1):
        ts = t_floor - back * MIN
        bar = store.get(ts)
        if bar is not None:
            return ts, bar[3]
    return None


def ret_post(store: BarStore, T: dt.datetime, k: int, baseline: float) -> Optional[float]:
    bar = store.get(T + (k - 1) * MIN)
    return None if bar is None else bar[3] / baseline - 1.0


def ret_pre(store: BarStore, baseline_ts: dt.datetime, baseline: float, k: int) -> Optional[float]:
    bar = store.get(baseline_ts - k * MIN)
    return None if bar is None else baseline / bar[3] - 1.0


def excursions(store: BarStore, T: dt.datetime, horizon: int, baseline: float) -> Tuple[Optional[float], Optional[float]]:
    highs, lows = [], []
    for i in range(horizon):
        bar = store.get(T + i * MIN)
        if bar is None:
            return None, None
        highs.append(bar[1]); lows.append(bar[2])
    return max(highs) / baseline - 1.0, min(lows) / baseline - 1.0


def realized_vol(store: BarStore, first_bar: dt.datetime, n: int) -> Optional[float]:
    """sqrt(sum ln(close_i/close_(i-1))^2) for bars first_bar .. first_bar+(n-1)m; needs the bar before first_bar too."""
    prev = store.get(first_bar - MIN)
    if prev is None:
        return None
    last, total = prev[3], 0.0
    for i in range(n):
        bar = store.get(first_bar + i * MIN)
        if bar is None:
            return None
        total += math.log(bar[3] / last) ** 2
        last = bar[3]
    return math.sqrt(total)


def volume_sum(store: BarStore, first_bar: dt.datetime, n: int) -> Optional[float]:
    total = 0.0
    for i in range(n):
        bar = store.get(first_bar + i * MIN)
        if bar is None:
            return None
        total += bar[4]
    return total


def direction(ret: Optional[float], tol: float) -> Optional[int]:
    if ret is None:
        return None
    return 0 if abs(ret) <= tol else (1 if ret > 0 else -1)


# --------------------------------------------------------------------------- one release x one symbol
FEATURE_COLUMNS = (
    [f"ret_pre_{k}m" for k in PRE_RETURN_HORIZONS] + [f"ret_post_{k}m" for k in POST_HORIZONS]
    + [f"direction_{k}m" for k in POST_HORIZONS]
    + [f"{n}_{h}m" for h in EXCURSION_HORIZONS for n in ("max_up", "max_down")]
    + ["realized_vol_pre_30m", "realized_vol_post_30m", "realized_vol_post_60m",
       "volume_pre_30m", "volume_post_30m", "volume_post_60m", "volume_ratio_30m"]
)


def compute_window(store: Optional[BarStore], t: Optional[dt.datetime], cfg: ResponseConfig = ResponseConfig()) -> Dict[str, Any]:
    """All market-response fields for one trusted timestamp `t` and one symbol's bars."""
    out: Dict[str, Any] = {c: None for c in FEATURE_COLUMNS}
    out.update(volume_ratio_30m_status=None, baseline_timestamp_utc=None, baseline_price=None, baseline_found=False,
               bars_expected=cfg.pre_minutes + cfg.post_minutes, bars_found=None, missing_bar_count=None,
               missing_pre_bars=None, missing_post_bars=None, timestamp_minute_aligned=None,
               market_window_status=NO_TRUSTED_EVENT_TIMESTAMP)
    if t is None:
        return out
    t = to_utc(t)
    t_floor, T = floor_minute(t), ceil_minute(t)
    out["timestamp_minute_aligned"] = (t_floor == T)
    span_start = t_floor - (cfg.pre_minutes + 1 + cfg.max_baseline_lookback_minutes) * MIN
    span_end = T + (cfg.post_minutes - 1) * MIN
    if store is None or not store.has_any(span_start, span_end):
        out["market_window_status"] = MISSING_MARKET_DATA
        return out

    pre_slots = [t_floor - i * MIN for i in range(cfg.pre_minutes, 0, -1)]
    post_slots = [T + i * MIN for i in range(cfg.post_minutes)]
    pre_found = sum(store.get(ts) is not None for ts in pre_slots)
    post_found = sum(store.get(ts) is not None for ts in post_slots)
    out.update(bars_found=pre_found + post_found, missing_pre_bars=cfg.pre_minutes - pre_found,
               missing_post_bars=cfg.post_minutes - post_found)
    out["missing_bar_count"] = out["missing_pre_bars"] + out["missing_post_bars"]

    base = find_baseline(store, t_floor, cfg)
    if base is None:
        out["market_window_status"] = NO_BASELINE_BAR
        return out
    b_ts, b_px = base
    out.update(baseline_found=True, baseline_timestamp_utc=b_ts, baseline_price=b_px)

    for k in PRE_RETURN_HORIZONS:
        out[f"ret_pre_{k}m"] = ret_pre(store, b_ts, b_px, k)
    for k in POST_HORIZONS:
        r = ret_post(store, T, k, b_px)
        out[f"ret_post_{k}m"], out[f"direction_{k}m"] = r, direction(r, cfg.direction_tolerance)
    for h in EXCURSION_HORIZONS:
        out[f"max_up_{h}m"], out[f"max_down_{h}m"] = excursions(store, T, h, b_px)
    out["realized_vol_pre_30m"] = realized_vol(store, t_floor - 30 * MIN, 30)
    if t_floor == T:            # otherwise the reference bar (T-1m) would straddle the release
        out["realized_vol_post_30m"] = realized_vol(store, T, 30)
        out["realized_vol_post_60m"] = realized_vol(store, T, 60)
    out["volume_pre_30m"] = volume_sum(store, t_floor - 30 * MIN, 30)
    out["volume_post_30m"] = volume_sum(store, T, 30)
    out["volume_post_60m"] = volume_sum(store, T, 60)
    pre_v, post_v = out["volume_pre_30m"], out["volume_post_30m"]
    if pre_v is None:
        out["volume_ratio_30m_status"] = "PRE_VOLUME_MISSING"
    elif post_v is None:
        out["volume_ratio_30m_status"] = "POST_VOLUME_MISSING"
    elif pre_v == 0:
        out["volume_ratio_30m_status"] = "PRE_VOLUME_ZERO"
    else:
        out["volume_ratio_30m"], out["volume_ratio_30m_status"] = post_v / pre_v, "OK"

    if out["missing_post_bars"]:
        out["market_window_status"] = INSUFFICIENT_POST_WINDOW
    elif out["missing_pre_bars"]:
        out["market_window_status"] = INSUFFICIENT_PRE_WINDOW
    else:
        out["market_window_status"] = OK
    return out


# --------------------------------------------------------------------------- all releases x symbols
RESPONSE_COLUMNS = [
    "event_id", "canonical_event_id", "event_family", "release_timestamp_utc", "release_timestamp_basis",
    "release_timestamp_america_new_york", "release_date", "reference_period",
    "provider_forecast", "actual", "actual_source", "surprise_raw", "surprise_z_prior_only",
    "symbol", "market_data_source", "market_data_feed", "market_data_feed_scope", "market_data_adjustment",
    "market_data_provenance_basis",
    "baseline_timestamp_utc", "baseline_price",
    *FEATURE_COLUMNS, "volume_ratio_30m_status",
    "release_session", "market_window_status", "bars_expected", "bars_found", "baseline_found",
    "missing_bar_count", "missing_pre_bars", "missing_post_bars", "timestamp_minute_aligned",
]


def _parse_ts(value) -> Optional[dt.datetime]:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value == "":
        return None
    return to_utc(dt.datetime.fromisoformat(value) if isinstance(value, str) else value)


def build_market_response(events: Sequence[Dict[str, Any]], bars_by_symbol: Dict[str, Optional[pd.DataFrame]],
                          cfg: ResponseConfig = ResponseConfig(),
                          market_provenance: Optional[Dict[str, Optional[Dict[str, str]]]] = None) -> List[Dict[str, Any]]:
    """Long-format rows, one per (validated calendar release x symbol). `events` are rows from the calendar-
    anchored macro validation; only `trusted_release_timestamp_utc` is used as event time. Symbols never share
    state: each has its own BarStore. `market_provenance` maps symbol -> the provenance returned by
    `load_market_dataset` for that symbol's bars; it is copied onto that symbol's rows (null when absent, e.g. no
    stored data)."""
    provenance = market_provenance or {}
    stores = {sym: (BarStore(df) if df is not None else None) for sym, df in bars_by_symbol.items()}
    rows: List[Dict[str, Any]] = []
    for ev in events:
        t = _parse_ts(ev.get("trusted_release_timestamp_utc"))
        for sym, store in stores.items():
            prov = provenance.get(sym) or {}
            w = compute_window(store, t, cfg)
            row = {c: None for c in RESPONSE_COLUMNS}
            row.update(
                event_id=ev.get("event_id"), canonical_event_id=ev.get("canonical_event_id"),
                event_family=ev.get("event_family"),
                release_timestamp_utc=t.isoformat() if t else None,
                release_timestamp_basis=ev.get("trusted_release_timestamp_basis") if t else "NO_TRUSTED_EVENT_TIMESTAMP",
                release_timestamp_america_new_york=t.astimezone(NY).isoformat() if t else None,
                release_date=ev.get("release_date"), reference_period=ev.get("reference_period"),
                provider_forecast=ev.get("ff_provider_forecast"), actual=ev.get("actual_value"),
                actual_source=ev.get("actual_value_source"), surprise_raw=ev.get("actual_minus_forecast"),
                surprise_z_prior_only=None, symbol=sym,
                market_data_source=prov.get("source"), market_data_feed=prov.get("feed"),
                market_data_feed_scope=prov.get("feed_scope"), market_data_adjustment=prov.get("adjustment"),
                market_data_provenance_basis=prov.get("provenance_basis"),
                release_session=classify_session(t) if t else None)
            row.update({k: v for k, v in w.items() if k in row})
            if w["baseline_timestamp_utc"] is not None:
                row["baseline_timestamp_utc"] = w["baseline_timestamp_utc"].isoformat()
            rows.append(row)
    return rows
