"""Market OHLCV validation.

Never auto-fills missing bars -- the point is to make gaps/anomalies
observable, not paper over them. All checks are read-only reports.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List

import pandas as pd

from ..timeutil import NY_TZ

REGULAR_SESSION_START = dt.time(9, 30)
REGULAR_SESSION_END = dt.time(16, 0)
REGULAR_SESSION_MINUTES = 390  # 9:30-16:00 ET


@dataclass
class MarketValidationReport:
    symbol: str
    year: int
    total_rows: int = 0
    is_sorted: bool = True
    duplicate_timestamp_count: int = 0
    invalid_ohlc_count: int = 0
    non_positive_price_count: int = 0
    negative_volume_count: int = 0
    suspicious_volume_count: int = 0  # > 10x the symbol-year median volume
    gap_count: int = 0
    largest_gap_minutes: float = 0.0
    extended_hours_rows: int = 0
    regular_hours_rows: int = 0
    trading_days: int = 0
    regular_hours_coverage_ratio: float = 0.0  # regular_hours_rows / (trading_days * 390)
    issues: List[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return (
            self.is_sorted
            and self.duplicate_timestamp_count == 0
            and self.invalid_ohlc_count == 0
            and self.non_positive_price_count == 0
            and self.negative_volume_count == 0
        )


def validate_market_bars(df: pd.DataFrame, symbol: str, year: int, gap_threshold_minutes: float = 5.0) -> MarketValidationReport:
    report = MarketValidationReport(symbol=symbol, year=year, total_rows=len(df))
    if df.empty:
        report.issues.append("no rows")
        return report

    ts = pd.to_datetime(df["timestamp_utc"], utc=True)

    report.is_sorted = bool(ts.is_monotonic_increasing)
    if not report.is_sorted:
        report.issues.append("timestamps not sorted")

    report.duplicate_timestamp_count = int(ts.duplicated().sum())
    if report.duplicate_timestamp_count:
        report.issues.append(f"{report.duplicate_timestamp_count} duplicate timestamps")

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    invalid_ohlc = (h < l) | (h < o) | (h < c) | (l > o) | (l > c)
    report.invalid_ohlc_count = int(invalid_ohlc.sum())
    if report.invalid_ohlc_count:
        report.issues.append(f"{report.invalid_ohlc_count} rows with invalid OHLC relationships")

    non_positive = (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
    report.non_positive_price_count = int(non_positive.sum())
    if report.non_positive_price_count:
        report.issues.append(f"{report.non_positive_price_count} rows with zero/negative price")

    volume = df["volume"]
    report.negative_volume_count = int((volume < 0).sum())
    if report.negative_volume_count:
        report.issues.append(f"{report.negative_volume_count} rows with negative volume")

    median_vol = volume[volume > 0].median() if (volume > 0).any() else 0
    if median_vol and median_vol > 0:
        report.suspicious_volume_count = int((volume > median_vol * 10).sum())

    # Timezone-aware gap / session-coverage analysis (NY session hours).
    ts_ny = ts.dt.tz_convert(NY_TZ)
    df_sorted = df.assign(_ts_ny=ts_ny).sort_values("timestamp_utc")

    diffs_minutes = df_sorted["_ts_ny"].diff().dt.total_seconds().div(60.0)
    same_day = df_sorted["_ts_ny"].dt.date == df_sorted["_ts_ny"].shift().dt.date
    intraday_diffs = diffs_minutes[same_day]
    gaps = intraday_diffs[intraday_diffs > gap_threshold_minutes]
    report.gap_count = int(gaps.shape[0])
    report.largest_gap_minutes = float(intraday_diffs.max()) if not intraday_diffs.empty else 0.0

    time_of_day = df_sorted["_ts_ny"].dt.time
    is_regular = (time_of_day >= REGULAR_SESSION_START) & (time_of_day < REGULAR_SESSION_END)
    report.regular_hours_rows = int(is_regular.sum())
    report.extended_hours_rows = int((~is_regular).sum())

    trading_days = df_sorted["_ts_ny"].dt.date.nunique()
    report.trading_days = int(trading_days)
    expected_regular_bars = trading_days * REGULAR_SESSION_MINUTES
    report.regular_hours_coverage_ratio = (
        report.regular_hours_rows / expected_regular_bars if expected_regular_bars else 0.0
    )

    return report
