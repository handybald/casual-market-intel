import datetime as dt

import pandas as pd
import pytest

from src.data.config import AppConfig
from src.data.fetch.massive import _parse_bars_response, _merge_year_parquet
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
                "adjusted": False,
            },
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_parse_bars_response_epoch_millis():
    payload = {
        "status": "OK",
        "results": [
            {"t": 1704110400000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000, "vw": 1.2, "n": 42},
        ],
    }
    bars = _parse_bars_response(payload)
    assert len(bars) == 1
    assert bars[0]["timestamp_utc"] == dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    assert bars[0]["open"] == 1.0
    assert bars[0]["transactions"] == 42


def test_parse_bars_response_skips_rows_without_timestamp():
    payload = {"status": "OK", "results": [{"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000}]}
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
    raw_dir = config.provider_raw_dir("massive") / "QQQ" / "1min" / "raw"
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

    acquired_at = dt.datetime(2020, 2, 1, tzinfo=dt.timezone.utc)
    result = normalize_market_year(config, "QQQ", 2020, timeframe="1min", adjusted=False, acquisition_timestamp_utc=acquired_at)
    assert len(result) == 2
    assert set(result["symbol"]) == {"QQQ"}
    assert set(result["source"]) == {"MASSIVE"}
    assert set(result["timeframe"]) == {"1min"}
    assert set(result["adjustment"]) == {"raw"}
    assert result["timestamp_utc"].is_monotonic_increasing
    assert (result["retrieval_timestamp_utc"] == acquired_at).all()
    assert (result["normalized_at_utc"] > result["retrieval_timestamp_utc"]).all()

    interim_path = config.interim_root / "massive" / "QQQ" / "1min" / "raw" / "2020.parquet"
    assert interim_path.exists()


def test_normalize_market_year_missing_raw_raises(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        normalize_market_year(config, "QQQ", 1999, timeframe="1min", adjusted=False)


# -- regression: per-row acquisition time, not one blanket value for the whole year (item #8) --

def test_normalize_market_year_uses_per_month_acquisition_map(tmp_path):
    config = make_config(tmp_path)
    raw_dir = config.provider_raw_dir("massive") / "QQQ" / "1min" / "raw"
    raw_dir.mkdir(parents=True)
    ts = pd.to_datetime(["2020-01-05T14:30:00Z", "2020-02-05T14:30:00Z"])
    df = pd.DataFrame({
        "timestamp_utc": ts, "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.9],
        "close": [1.2, 2.2], "volume": [100, 200], "vwap": [1.1, 2.1], "transactions": [5, 6],
    })
    df.to_parquet(raw_dir / "2020.parquet", index=False)

    jan_acquired = dt.datetime(2020, 1, 10, tzinfo=dt.timezone.utc)
    feb_acquired = dt.datetime(2020, 2, 10, tzinfo=dt.timezone.utc)
    result = normalize_market_year(
        config, "QQQ", 2020, timeframe="1min", adjusted=False,
        month_acquisition_map={1: jan_acquired, 2: feb_acquired},
    )

    jan_row = result[result["timestamp_utc"].dt.month == 1].iloc[0]
    feb_row = result[result["timestamp_utc"].dt.month == 2].iloc[0]
    # THE CORE ASSERTION: each row carries the acquisition time of the
    # chunk it ACTUALLY came from -- not the same (e.g. latest) value for both.
    assert jan_row["retrieval_timestamp_utc"] == jan_acquired
    assert feb_row["retrieval_timestamp_utc"] == feb_acquired
    assert jan_row["retrieval_timestamp_utc"] != feb_row["retrieval_timestamp_utc"]


def test_normalize_market_symbol_builds_per_month_map_from_manifest(tmp_path):
    from src.data.manifest import Manifest, ManifestEntry
    from src.data.normalize.market import normalize_market_symbol

    config = make_config(tmp_path)
    raw_dir = config.provider_raw_dir("massive") / "QQQ" / "1min" / "raw"
    raw_dir.mkdir(parents=True)
    ts = pd.to_datetime(["2020-01-05T14:30:00Z", "2020-02-05T14:30:00Z"])
    df = pd.DataFrame({
        "timestamp_utc": ts, "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.9, 1.9],
        "close": [1.2, 2.2], "volume": [100, 200], "vwap": [1.1, 2.1], "transactions": [5, 6],
    })
    df.to_parquet(raw_dir / "2020.parquet", index=False)

    manifest = Manifest(config.manifest_path)
    key = "QQQ:1min:raw"
    # January fetched long ago; February fetched much more recently --
    # two genuinely different acquisition times for the same year file.
    manifest.record(ManifestEntry(
        provider="massive", key=key, start="2020-01-01", end="2020-01-31", status="complete",
        retrieved_at="2020-01-10T00:00:00+00:00",
    ))
    manifest.record(ManifestEntry(
        provider="massive", key=key, start="2020-02-01", end="2020-02-29", status="complete",
        retrieved_at="2024-06-15T00:00:00+00:00",
    ))

    [result] = normalize_market_symbol(config, "QQQ", dt.date(2020, 1, 1), dt.date(2020, 12, 31), manifest=manifest)

    jan_row = result[result["timestamp_utc"].dt.month == 1].iloc[0]
    feb_row = result[result["timestamp_utc"].dt.month == 2].iloc[0]
    assert jan_row["retrieval_timestamp_utc"] == dt.datetime(2020, 1, 10, tzinfo=dt.timezone.utc)
    assert feb_row["retrieval_timestamp_utc"] == dt.datetime(2024, 6, 15, tzinfo=dt.timezone.utc)
    # Not the old buggy behavior: February's (later/"latest") timestamp
    # must NOT have been applied to January's rows too.
    assert jan_row["retrieval_timestamp_utc"] != feb_row["retrieval_timestamp_utc"]
