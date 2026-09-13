"""Shared test fixture helpers for Massive market-bar data.

Not a test module itself (no `test_` prefix) -- imported by the actual
test files. Centralized here because validation now requires genuine
session-level completeness (see src/data/validation/market.py and
tests/test_validation_market_phase1.py): a single bar per session or
per calendar day is no longer "realistic enough" fixture data to pass
finalization, so every test that fetches Massive data through the real
`fetch_massive_symbol`/CLI path needs full-density bars.
"""
from __future__ import annotations

import datetime as dt
from typing import List

from src.data.validation.market import nyse_sessions


def full_session_bars(start: dt.date, end: dt.date, timeframe_minutes: int = 1) -> List[dict]:
    """One bar per `timeframe_minutes`-minute interval for every real
    NYSE session in [start, end] -- genuinely complete data, not a
    single token bar, so it passes the real completeness validation."""
    sessions = nyse_sessions(start, end)
    bars = []
    step = dt.timedelta(minutes=timeframe_minutes)
    for _, row in sessions.iterrows():
        t = row["market_open"].to_pydatetime()
        close = row["market_close"].to_pydatetime()
        while t < close:
            bars.append({
                "timestamp_utc": t, "open": 1, "high": 1, "low": 1, "close": 1,
                "volume": 1, "vwap": 1, "transactions": 1,
            })
            t += step
    return bars
