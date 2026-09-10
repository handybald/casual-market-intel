import datetime as dt

import pandas as pd
import pytest

from src.data.config import AppConfig
from src.data.fetch.massive import _parse_bars_response, _merge_year_parquet, BAR_COLUMNS
from src.data.normalize.market import normalize_market_year


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2016-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"),
            "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"),
            "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "massive": {
                "chunk_frequency": "month",
                "raw_dir": str(tmp_path / "raw" / "massive"),
                "base_url": "http://example.invalid",
                "max_retries": 1,
                "request_delay_seconds": 0,
                "page_limit": 1000,
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_parse_bars_response_epoch_millis():
    payload = {
        "results": [
            {"t": 1704110400000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000, "vw": 1.2, "n": 42},
        ]
    }
    bars = _parse_bars_response(payload)
    assert len(bars) == 1
    assert bars[0]["timestamp_utc"] == dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    assert bars[0]["open"] == 1.0
    assert bars[0]["transactions"] == 42


def test_parse_bars_response_skips_rows_without_timestamp():
    payload = {"results": [{"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000}]}
    assert _parse_bars_response(payload) == []


def test_merge_year_parquet_dedups_by_timestamp_new_wins(tmp_path):
    path = tmp_path / "QQQ_2020.parquet"
    ts = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    first = [{"timestamp_utc": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100, "vwap": 1.0, "transactions": 1}]
    df1 = _merge_year_parquet(path, first)
    df1.to_parquet(path, index=False)

    second = [{"timestamp_utc": ts, "open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 200, "vwap": 2.0, "transactions": 2}]
    merged = _merge_year_parquet(path, second)

    assert len(merged) == 1
    assert merged.iloc[0]["open"] == 2.0  # new row wins


def test_merge_year_parquet_sorts_chronologically(tmp_path):
    path = tmp_path / "QQQ_2020.parquet"
    later = dt.datetime(2020, 1, 2, 14, 31, tzinfo=dt.timezone.utc)
    earlier = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    rows = [
        {"timestamp_utc": later, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "vwap": 1, "transactions": 1},
        {"timestamp_utc": earlier, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "vwap": 1, "transactions": 1},
    ]
    merged = _merge_year_parquet(path, rows)
    assert list(merged["timestamp_utc"]) == [earlier, later]


def test_normalize_market_year_produces_interim_file(tmp_path):
    config = make_config(tmp_path)
    raw_dir = config.provider_raw_dir("massive") / "QQQ"
    raw_dir.mkdir(parents=True)
    ts = pd.to_datetime(["2020-01-02T14:30:00Z", "2020-01-02T14:31:00Z"])
    df = pd.DataFrame(
        {
            "timestamp_utc": ts,
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.9, 1.9],
            "close": [1.2, 2.2],
            "volume": [100, 200],
            "vwap": [1.1, 2.1],
            "transactions": [5, 6],
        }
    )
    df.to_parquet(raw_dir / "2020.parquet", index=False)

    result = normalize_market_year(config, "QQQ", 2020)
    assert len(result) == 2
    assert set(result["symbol"]) == {"QQQ"}
    assert set(result["source"]) == {"MASSIVE"}
    assert result["timestamp_utc"].is_monotonic_increasing

    interim_path = config.interim_root / "massive" / "QQQ" / "2020.parquet"
    assert interim_path.exists()


def test_normalize_market_year_missing_raw_raises(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        normalize_market_year(config, "QQQ", 1999)
