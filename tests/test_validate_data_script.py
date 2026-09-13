"""Tests for scripts/validate_data.py -- specifically that macro-release-
window coverage (a precision-sensitive, minute-level join) only trusts
CONFIRMED-quality timestamps, never ASSUMED ones (second review item #9)."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_data as validate_script  # noqa: E402  (path inserted above)

from src.data.config import AppConfig
from src.data.manifest import Manifest, ManifestEntry
from src.data.schemas import MacroEvent, MacroSource, TimestampQuality


def _event(quality: TimestampQuality, ts: dt.datetime) -> MacroEvent:
    return MacroEvent(
        event_id=f"ff:{quality.value}",
        event_family="CPI_MOM",
        indicator="CPI MoM",
        release_timestamp_utc=ts,
        timestamp_quality=quality,
        source=MacroSource.FOREX_FACTORY,
        source_timestamp=ts,
        source_timezone="America/New_York",
        retrieval_timestamp_utc=dt.datetime.now(dt.timezone.utc),
    )


def make_config(tmp_path) -> AppConfig:
    raw = {
        "historical": {"start_date": "2020-01-01", "end_date": None},
        "macro": {"country": "US", "currency": "USD"},
        "market": {"provider": "massive", "timeframe": "1min", "symbols": ["QQQ"]},
        "storage": {
            "raw_root": str(tmp_path / "raw"), "interim_root": str(tmp_path / "interim"),
            "processed_root": str(tmp_path / "processed"), "manifest_path": str(tmp_path / "manifests" / "fetch_manifest.json"),
        },
        "providers": {
            "massive": {"raw_dir": str(tmp_path / "raw" / "massive")},
        },
    }
    return AppConfig(raw, tmp_path / "config.yaml")


def test_no_massive_data_returns_false_and_does_not_crash(tmp_path):
    config = make_config(tmp_path)
    report_dir = tmp_path / "reports"
    result = validate_script.validate_market(config, [], report_dir)
    assert result is False


def test_assumed_quality_events_excluded_from_macro_window_check(tmp_path, monkeypatch):
    """Regression: an ASSUMED (unverified) timestamp must NOT be promoted
    into the precision-sensitive macro-release-window check."""
    config = make_config(tmp_path)
    raw_root = config.provider_raw_dir("massive")
    (raw_root / "QQQ" / "1min" / "raw").mkdir(parents=True)

    ts = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    df = pd.DataFrame([{
        "timestamp_utc": ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "vwap": 1, "transactions": 1,
    }])
    df.to_parquet(raw_root / "QQQ" / "1min" / "raw" / "2020.parquet", index=False)

    # `validate_market` now scopes each stored year-parquet to its
    # manifest-recorded requested coverage rather than the full calendar
    # year -- a manifest entry is required for the file to be validated
    # at all (see Phase 1B fix, scripts/validate_data.py).
    manifest = Manifest(config.manifest_path)
    manifest.record(ManifestEntry(
        provider="massive", key="QQQ:1min:raw", start="2020-01-01", end="2020-01-04",
        status="complete", rows=1,
    ))

    captured = {}
    real_validate = validate_script.validate_market_bars

    def capturing_validate(df_arg, symbol, start, end, macro_release_timestamps_utc=None, **kwargs):
        captured["release_timestamps"] = macro_release_timestamps_utc
        return real_validate(df_arg, symbol, start, end, macro_release_timestamps_utc=macro_release_timestamps_utc, **kwargs)

    monkeypatch.setattr(validate_script, "validate_market_bars", capturing_validate)

    macro_events = [
        _event(TimestampQuality.CONFIRMED, dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)),
        _event(TimestampQuality.ASSUMED, dt.datetime(2020, 1, 3, 14, 30, tzinfo=dt.timezone.utc)),
        _event(TimestampQuality.TENTATIVE, dt.datetime(2020, 1, 4, 14, 30, tzinfo=dt.timezone.utc)),
    ]

    validate_script.validate_market(config, macro_events, tmp_path / "reports")

    passed_timestamps = captured["release_timestamps"]
    assert len(passed_timestamps) == 1  # only the CONFIRMED one
    assert passed_timestamps[0] == dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
