"""Massive market data client (1-minute OHLCV bars).

ASSUMED API CONTRACT: this client has been written against a
Polygon-aggregates-style JSON contract (epoch-millisecond timestamps,
`{"results": [...], "next_url" | "next_cursor": ...}` pagination) because
the actual Massive API reference was not available while building this
pipeline. No live call has been made against Massive in this
environment (no MASSIVE_API_KEY here), so this is explicitly unverified.
Before running for real: check config/data_sources.yaml `providers.massive`
against the real API docs and adjust `_parse_bars_response` / the request
params below if the field names differ. Everything AROUND this contract
(chunking, pagination loop, retry, resume, per-year parquet merge,
chronological validation) is provider-agnostic and does not need to change.
"""
from __future__ import annotations

import datetime as dt
import logging
from calendar import monthrange
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import pandas as pd
import requests

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..manifest import Manifest, ManifestEntry, checksum_file
from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)

BAR_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"]


class MissingCredentialsError(RuntimeError):
    pass


class MonthResult(NamedTuple):
    symbol: str
    year: int
    month: int
    status: str  # "complete" | "empty" | "failed" | "skipped_cached"
    rows: int
    error: Optional[str] = None


def _parse_bars_response(payload: dict) -> List[dict]:
    """Extract a flat list of bar dicts with keys matching BAR_COLUMNS
    from one page of the (assumed) Massive API response."""
    raw_bars = payload.get("results") or payload.get("bars") or []
    out = []
    for b in raw_bars:
        ts_raw = b.get("t")
        if ts_raw is None:
            continue
        if isinstance(ts_raw, (int, float)):
            # epoch milliseconds
            timestamp = dt.datetime.fromtimestamp(ts_raw / 1000.0, tz=dt.timezone.utc)
        else:
            timestamp = pd.to_datetime(ts_raw, utc=True).to_pydatetime()
        out.append(
            {
                "timestamp_utc": timestamp,
                "open": b.get("o"),
                "high": b.get("h"),
                "low": b.get("l"),
                "close": b.get("c"),
                "volume": b.get("v"),
                "vwap": b.get("vw"),
                "transactions": b.get("n"),
            }
        )
    return out


def _next_cursor(payload: dict) -> Optional[str]:
    return payload.get("next_cursor") or payload.get("next_page_token")


def fetch_month_bars(
    config: AppConfig,
    symbol: str,
    year: int,
    month: int,
    timeframe: str,
    session: requests.Session,
    api_key: str,
) -> List[dict]:
    provider_cfg = config.provider("massive")
    base_url = provider_cfg["base_url"]
    page_limit = provider_cfg.get("page_limit", 50000)

    start = dt.date(year, month, 1)
    end = dt.date(year, month, monthrange(year, month)[1])

    all_bars: List[dict] = []
    cursor: Optional[str] = None

    while True:
        params: Dict[str, str] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": str(page_limit),
        }
        if cursor:
            params["cursor"] = cursor

        response = request_with_retry(
            "GET",
            f"{base_url}/v1/bars",
            session=session,
            max_retries=provider_cfg.get("max_retries", 5),
            request_delay_seconds=provider_cfg.get("request_delay_seconds", 0.25),
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        payload = response.json()
        page_bars = _parse_bars_response(payload)
        all_bars.extend(page_bars)

        cursor = _next_cursor(payload)
        if not cursor or not page_bars:
            break

    return all_bars


def _year_parquet_path(config: AppConfig, symbol: str, year: int) -> Path:
    return config.provider_raw_dir("massive") / symbol / f"{year}.parquet"


def _merge_year_parquet(path: Path, new_rows: List[dict]) -> pd.DataFrame:
    new_df = pd.DataFrame(new_rows, columns=BAR_COLUMNS)
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined = combined.drop_duplicates(subset=["timestamp_utc"], keep="last")
    combined = combined.sort_values("timestamp_utc").reset_index(drop=True)
    return combined


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_path, index=False)
    tmp_path.replace(path)


def fetch_massive_symbol(
    config: AppConfig,
    manifest: Manifest,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    timeframe: str,
    force: bool = False,
    session: Optional[requests.Session] = None,
) -> List[MonthResult]:
    api_key = config.env("MASSIVE_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "MASSIVE_API_KEY not set (see .env.example). Cannot fetch Massive market data."
        )

    sess = session or requests.Session()
    results: List[MonthResult] = []

    months_by_year: Dict[int, List[int]] = {}
    for chunk in iter_date_chunks(start_date, end_date, frequency="month"):
        months_by_year.setdefault(chunk.start.year, []).append(chunk.start.month)

    for year, months in sorted(months_by_year.items()):
        new_rows_for_year: List[dict] = []
        touched = False

        for month in sorted(months):
            key = symbol
            start = dt.date(year, month, 1).isoformat()
            end = dt.date(year, month, monthrange(year, month)[1]).isoformat()

            if not force and manifest.is_complete("massive", key, start, end):
                logger.info("[Massive][%s] %s-%02d already fetched, skipping", symbol, year, month)
                results.append(MonthResult(symbol, year, month, "skipped_cached", 0))
                continue

            try:
                bars = fetch_month_bars(config, symbol, year, month, timeframe, sess, api_key)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                logger.error("[Massive][%s] %s-%02d FAILED: %s", symbol, year, month, exc)
                manifest.record(
                    ManifestEntry(
                        provider="massive", key=key, start=start, end=end,
                        status="failed", error=str(exc),
                    )
                )
                results.append(MonthResult(symbol, year, month, "failed", 0, error=str(exc)))
                continue

            status = "complete" if bars else "empty"
            new_rows_for_year.extend(bars)
            touched = True
            logger.info("[Massive][%s] %s-%02d %s: %d bars", symbol, year, month, status, len(bars))
            # Manifest write deferred until after the year file is merged
            # successfully, so a crash mid-merge doesn't falsely mark the
            # month complete.
            results.append(MonthResult(symbol, year, month, status, len(bars)))

        if not touched:
            continue

        path = _year_parquet_path(config, symbol, year)
        merged = _merge_year_parquet(path, new_rows_for_year)
        _validate_chronological(merged, symbol, year)
        _atomic_write_parquet(merged, path)
        checksum = checksum_file(path)

        for month in sorted(months):
            r = next((r for r in results if r.year == year and r.month == month and r.symbol == symbol), None)
            if r is None or r.status not in ("complete", "empty"):
                continue
            start = dt.date(year, month, 1).isoformat()
            end = dt.date(year, month, monthrange(year, month)[1]).isoformat()
            manifest.record(
                ManifestEntry(
                    provider="massive",
                    key=symbol,
                    start=start,
                    end=end,
                    status=r.status,
                    rows=r.rows,
                    checksum=checksum,
                    path=str(path),
                )
            )

    return results


def _validate_chronological(df: pd.DataFrame, symbol: str, year: int) -> None:
    if df.empty:
        return
    if not df["timestamp_utc"].is_monotonic_increasing:
        raise ValueError(f"[Massive][{symbol}] {year}: timestamps not sorted after merge")
    if df["timestamp_utc"].duplicated().any():
        raise ValueError(f"[Massive][{symbol}] {year}: duplicate timestamps after merge")


def fetch_massive_market_data(
    config: AppConfig,
    manifest: Manifest,
    symbols: List[str],
    start_date: dt.date,
    end_date: dt.date,
    timeframe: str,
    force: bool = False,
) -> Dict[str, List[MonthResult]]:
    session = requests.Session()
    out: Dict[str, List[MonthResult]] = {}
    for symbol in symbols:
        out[symbol] = fetch_massive_symbol(
            config, manifest, symbol, start_date, end_date, timeframe, force=force, session=session
        )
    return out
