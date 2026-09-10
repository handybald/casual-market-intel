"""Normalize raw Massive OHLCV parquet into the canonical interim layer.

Raw (data/raw/massive/{symbol}/{year}.parquet) already uses our column
names because the OHLCV bar shape barely differs between providers and
storing genuinely provider-raw JSON per 1-minute bar for a decade would
be enormous and useless (see architecture notes in fetch/massive.py).
This step adds dtype enforcement, provenance columns, and canonical
schema validation, and writes the result to data/interim/ untouched raw
files are never modified in place.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import List

import pandas as pd

from ..config import AppConfig
from ..schemas import MarketBar
from ..timeutil import now_utc

logger = logging.getLogger(__name__)


def _raw_path(config: AppConfig, symbol: str, year: int) -> Path:
    return config.provider_raw_dir("massive") / symbol / f"{year}.parquet"


def _interim_path(config: AppConfig, symbol: str, year: int) -> Path:
    return config.interim_root / "massive" / symbol / f"{year}.parquet"


def normalize_market_year(
    config: AppConfig, symbol: str, year: int, sample_validate: int = 50
) -> pd.DataFrame:
    raw_path = _raw_path(config, symbol, year)
    if not raw_path.exists():
        raise FileNotFoundError(f"no raw Massive data for {symbol} {year} at {raw_path}")

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
    df["retrieval_timestamp_utc"] = now_utc()

    _spot_check_schema(df, symbol, sample_validate)

    out_path = _interim_path(config, symbol, year)
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
            source=row["source"],
            retrieval_timestamp_utc=row["retrieval_timestamp_utc"],
        )


def normalize_market_symbol(
    config: AppConfig, symbol: str, start_date: dt.date, end_date: dt.date
) -> List[pd.DataFrame]:
    frames = []
    for year in range(start_date.year, end_date.year + 1):
        raw_path = _raw_path(config, symbol, year)
        if not raw_path.exists():
            logger.warning("[normalize][Massive][%s] no raw data for %d, skipping", symbol, year)
            continue
        frames.append(normalize_market_year(config, symbol, year))
    return frames
