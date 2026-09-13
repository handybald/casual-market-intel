"""Normalize raw Massive OHLCV parquet into the canonical interim layer.

Raw (data/raw/massive/{symbol}/{timeframe}/{raw|adjusted}/{year}.parquet)
already uses our column names because the OHLCV bar shape barely
differs between providers and storing genuinely provider-raw JSON per
1-minute bar for a decade would be enormous and useless (see
architecture notes in fetch/massive.py). This step adds dtype
enforcement, provenance columns, and canonical schema validation, and
writes the result to data/interim/ -- raw files are never modified in
place.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..config import AppConfig
from ..manifest import Manifest, checksum_file
from ..schemas import MarketBar
from ..timeutil import now_utc

logger = logging.getLogger(__name__)


def _raw_path(config: AppConfig, symbol: str, timeframe: str, adjusted: bool, year: int) -> Path:
    adj_label = "adjusted" if adjusted else "raw"
    return config.provider_raw_dir("massive") / symbol / timeframe / adj_label / f"{year}.parquet"


def _interim_path(config: AppConfig, symbol: str, timeframe: str, adjusted: bool, year: int) -> Path:
    adj_label = "adjusted" if adjusted else "raw"
    return config.interim_root / "massive" / symbol / timeframe / adj_label / f"{year}.parquet"


def normalize_market_year(
    config: AppConfig,
    symbol: str,
    year: int,
    timeframe: Optional[str] = None,
    adjusted: Optional[bool] = None,
    acquisition_timestamp_utc: Optional[dt.datetime] = None,
    month_acquisition_map: Optional[Dict[int, dt.datetime]] = None,
    sample_validate: int = 50,
) -> pd.DataFrame:
    """`month_acquisition_map` (preferred, real-pipeline path): {month:
    retrieved_at} -- each row gets the acquisition time of the SPECIFIC
    fetch chunk it actually came from, not one blanket timestamp for the
    whole year file (a prior version assigned the single LATEST month's
    acquisition time to every row in the year, including months fetched
    long before). `acquisition_timestamp_utc` (single value) remains
    supported as a lower-fidelity fallback for standalone/test use where
    no manifest/per-month breakdown is available; it also serves as the
    fallback for any month unexpectedly absent from `month_acquisition_map`.
    """
    provider_cfg = config.provider("massive")
    timeframe = timeframe or config.market_timeframe
    adjusted = provider_cfg.get("adjusted", False) if adjusted is None else adjusted

    raw_path = _raw_path(config, symbol, timeframe, adjusted, year)
    if not raw_path.exists():
        raise FileNotFoundError(f"no raw Massive data for {symbol} {year} at {raw_path}")

    # Single-value fallback default: file mtime, if the caller supplied
    # neither a per-month map nor an explicit single timestamp. Callers
    # driving the real pipeline should always pass `month_acquisition_map`
    # so acquisition time is never conflated with normalization time NOR
    # collapsed across months that were actually fetched on different runs.
    fallback_ts = acquisition_timestamp_utc
    if fallback_ts is None:
        fallback_ts = dt.datetime.fromtimestamp(raw_path.stat().st_mtime, tz=dt.timezone.utc)

    df = pd.read_parquet(raw_path)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    for col in ("open", "high", "low", "close", "volume", "vwap"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "transactions" in df.columns:
        df["transactions"] = pd.to_numeric(df["transactions"], errors="coerce").astype("Int64")

    df = df.sort_values("timestamp_utc").reset_index(drop=True)

    df["symbol"] = symbol
    df["source"] = "MASSIVE"
    df["timeframe"] = timeframe
    df["adjustment"] = "adjusted" if adjusted else "raw"
    if month_acquisition_map:
        row_months = df["timestamp_utc"].dt.month
        df["retrieval_timestamp_utc"] = [
            month_acquisition_map.get(int(m), fallback_ts) for m in row_months
        ]
    else:
        df["retrieval_timestamp_utc"] = fallback_ts
    df["normalized_at_utc"] = now_utc()
    df["raw_artifact_checksum"] = checksum_file(raw_path)

    _spot_check_schema(df, symbol, sample_validate)

    out_path = _interim_path(config, symbol, timeframe, adjusted, year)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_path, index=False)
    tmp_path.replace(out_path)

    logger.info("[normalize][Massive][%s] %d: %d bars -> %s", symbol, year, len(df), out_path)
    return df


def _spot_check_schema(df: pd.DataFrame, symbol: str, sample_size: int) -> None:
    """Validate a sample of rows against the canonical MarketBar model.
    Row-by-row pydantic validation of ~3.5M bars/symbol/decade would be
    slow for little benefit once the pipeline is stable; a sample catches
    structural regressions (bad dtypes, unexpected nulls) cheaply."""
    if df.empty:
        return
    sample = df.sample(min(sample_size, len(df)), random_state=0)
    for _, row in sample.iterrows():
        MarketBar(
            symbol=row["symbol"],
            timestamp_utc=row["timestamp_utc"].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            vwap=float(row["vwap"]) if pd.notna(row.get("vwap")) else None,
            transactions=int(row["transactions"]) if pd.notna(row.get("transactions")) else None,
            timeframe=row["timeframe"],
            adjustment=row["adjustment"],
            source=row["source"],
            retrieval_timestamp_utc=row["retrieval_timestamp_utc"],
            normalized_at_utc=row["normalized_at_utc"],
        )


def normalize_market_symbol(
    config: AppConfig,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    timeframe: Optional[str] = None,
    manifest: Optional[Manifest] = None,
) -> List[pd.DataFrame]:
    provider_cfg = config.provider("massive")
    timeframe = timeframe or config.market_timeframe
    adjusted = bool(provider_cfg.get("adjusted", False))
    frames = []
    for year in range(start_date.year, end_date.year + 1):
        raw_path = _raw_path(config, symbol, timeframe, adjusted, year)
        if not raw_path.exists():
            logger.warning("[normalize][Massive][%s] no raw data for %d, skipping", symbol, year)
            continue
        month_acquisition_map: Dict[int, dt.datetime] = {}
        fallback_ts = None
        if manifest is not None:
            from ..fetch.massive import cache_key

            key = cache_key(symbol, timeframe, adjusted)
            entries = [e for e in manifest.entries_for("massive", key) if e.start.startswith(str(year))]
            for e in entries:
                # Each manifest entry's OWN retrieved_at -- the actual
                # chunk it came from -- not a single collapsed value for
                # the whole year (that was the bug: every row in the
                # year used to get the LATEST month's acquisition time).
                month = dt.date.fromisoformat(e.start).month
                month_acquisition_map[month] = dt.datetime.fromisoformat(e.retrieved_at)
            if entries:
                fallback_ts = dt.datetime.fromisoformat(max(e.retrieved_at for e in entries))
        frames.append(
            normalize_market_year(
                config, symbol, year, timeframe, adjusted,
                acquisition_timestamp_utc=fallback_ts,
                month_acquisition_map=month_acquisition_map or None,
            )
        )
    return frames
