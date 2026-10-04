"""Cross-provider comparison: alignment, discrepancy metrics kept separate
from hard integrity failures, refusal of incompatible pairs, and the
comparison CLI over real stored provider datasets. Also the HTTP-error
credential redaction that protects manifests/logs."""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.data.http_utils import FetchError, request_with_retry
from src.data.validation.cross_provider import DatasetSide, IncompatibleComparisonError, compare_market_bars
from tests._market_fixtures import full_session_bars
from tests.test_market_providers import make_config
import compare_market_providers as cli

DAY = dt.date(2024, 1, 2)  # regular session 14:30-21:00 UTC
CONS = DatasetSide("massive", "MASSIVE", "consolidated", "consolidated", "raw", "1min")
IEX = DatasetSide("alpaca-iex", "ALPACA", "iex", "single_venue", "raw", "1min")


def frame(bars, price=lambda i: 100 + 0.01 * i, volume=1000.0):
    rows = []
    for i, b in enumerate(bars):
        p = price(i)
        rows.append({"timestamp_utc": b["timestamp_utc"], "open": p, "high": p + 0.05, "low": p - 0.05, "close": p,
                     "volume": volume, "vwap": p, "transactions": 1})
    df = pd.DataFrame(rows)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


def ext_bar(hour_utc, minute=0):
    return {"timestamp_utc": dt.datetime(2024, 1, 2, hour_utc, minute, tzinfo=dt.timezone.utc)}


def test_alignment_and_coverage_split_by_session():
    session = full_session_bars(DAY, DAY)
    a = frame(session + [ext_bar(13, 0), ext_bar(13, 1)])          # + 2 premarket bars
    b = frame([x for i, x in enumerate(session) if i % 2 == 0])     # IEX-like: every other minute
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", DAY, DAY)
    assert r["integrity_ok"]
    cov = r["descriptive"]["coverage"]
    assert cov["bars"]["massive"] == {"total": 392, "regular_session": 390, "extended_hours": 2}
    assert cov["in_both"]["total"] == 195
    assert cov["only_in_massive"] == {"total": 197, "regular_session": 195, "extended_hours": 2}
    assert cov["only_in_alpaca-iex"]["total"] == 0
    assert cov["expected_regular_session_bars"] == 390
    assert cov["regular_session_fill_alpaca-iex"] == pytest.approx(0.5)


def test_price_volume_and_return_metrics_are_descriptive_not_failures():
    session = full_session_bars(DAY, DAY)
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.002, len(session))
    a = frame(session)
    b = frame(session, price=lambda i: 100 + 0.01 * i + noise[i], volume=25.0)  # ~2.5% of consolidated volume
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", DAY, DAY)
    assert r["integrity_ok"] and r["hard_integrity_failures"] == {"massive": [], "alpaca-iex": []}
    d = r["descriptive"]
    close = d["price_differences"]["close"]["abs_diff"]["all"]
    assert close["n"] == 390 and 0 < close["median"] <= close["p95"] <= close["max"]
    assert d["price_differences"]["close"]["abs_diff"]["extended_hours"]["n"] == 0
    v = d["volume_differences"]
    assert v["comparable_as_full_market"] is False
    assert v["ratio_massive_over_alpaca-iex"]["median"] == pytest.approx(40.0)
    assert any("single-venue" in n for n in r["compatibility"]["notes"])
    corr = d["return_correlation"]["regular_session"]
    assert corr["n"] == 389 and corr["corr"] is not None
    assert len(d["largest_close_differences"]) == 10


def test_returns_only_between_consecutive_minutes_present_in_both():
    session = full_session_bars(DAY, DAY)[:10]
    a = frame(session)
    b = frame([x for i, x in enumerate(session) if i != 5])  # gap at minute 5
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", DAY, DAY)
    # 9 shared bars; pairs (4,5) and (5,6) are unavailable -> 9 - 1 (first) - 1 (6 has no shared predecessor) = 7
    assert r["descriptive"]["return_correlation"]["all"]["n"] == 7


def test_session_presence_differences_are_reported():
    two_days = full_session_bars(dt.date(2024, 1, 2), dt.date(2024, 1, 3))
    a = frame(two_days)
    b = frame([x for x in two_days if x["timestamp_utc"].date() == dt.date(2024, 1, 2)])
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", dt.date(2024, 1, 2), dt.date(2024, 1, 3))
    sp = r["descriptive"]["session_presence"]
    assert sp["sessions_with_regular_bars_only_in_massive"] == ["2024-01-03"]
    assert sp["nyse_sessions"] == 2 and sp["sessions_with_no_regular_bars_in_either"] == []


def test_integrity_failures_are_separate_and_suppress_metrics():
    session = full_session_bars(DAY, DAY)
    a = frame(session)
    b = pd.concat([frame(session), frame(session[:1])])  # duplicate timestamp
    b.loc[b.index[3], "high"] = 50.0                       # high < low
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", DAY, DAY)
    assert not r["integrity_ok"] and r["descriptive"] is None
    issues = r["hard_integrity_failures"]["alpaca-iex"]
    assert any("duplicate" in i for i in issues) and any("invalid OHLC" in i for i in issues)
    assert r["hard_integrity_failures"]["massive"] == []


def test_naive_timestamps_are_an_integrity_failure_not_silently_localized():
    a = frame(full_session_bars(DAY, DAY))
    b = a.copy()
    b["timestamp_utc"] = b["timestamp_utc"].dt.tz_localize(None)
    r = compare_market_bars(a, b, CONS, IEX, "QQQ", DAY, DAY)
    assert any("naive" in i for i in r["hard_integrity_failures"]["alpaca-iex"])


@pytest.mark.parametrize("other", [
    DatasetSide("alpaca-iex", "ALPACA", "iex", "single_venue", "split", "1min"),
    DatasetSide("alpaca-iex", "ALPACA", "iex", "single_venue", "raw", "5min"),
    DatasetSide("massive2", "MASSIVE", "consolidated", "consolidated", "raw", "1min"),
])
def test_incompatible_pairs_are_refused(other):
    a = frame(full_session_bars(DAY, DAY))
    with pytest.raises(IncompatibleComparisonError):
        compare_market_bars(a, a, CONS, other, "QQQ", DAY, DAY)


# --------------------------------------------------------------------------- CLI over stored datasets
def _store(path, df):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def test_compare_cli_end_to_end(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    session = full_session_bars(DAY, DAY)
    _store(tmp_path / "raw/massive/QQQ/1min/raw/2024.parquet", frame(session))
    _store(tmp_path / "raw/alpaca/iex/QQQ/1min/raw/2024.parquet", frame(session[::3], volume=20.0))
    rc = cli.main(["--providers", "massive,alpaca", "--symbols", "QQQ", "--start", "2024-01-02", "--end", "2024-01-02",
                   "--report-dir", str(tmp_path / "rep")])
    assert rc == 0
    rep = json.loads((tmp_path / "rep/provider_comparison_massive_vs_alpaca-iex_QQQ_1min_raw_2024-01-02_2024-01-02.json").read_text())
    assert rep["providers"]["b"]["feed"] == "iex" and rep["descriptive"]["coverage"]["in_both"]["total"] == 130
    # nothing was fetched for either side, so the manifest truthfully shows the window as never fetched
    assert rep["manifest_coverage_gaps"]["alpaca-iex"][0]["reason"] == "missing"
    assert "comparable as full-market: False" in capsys.readouterr().out


def test_compare_cli_reports_missing_side_and_bad_args(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    _store(tmp_path / "raw/massive/QQQ/1min/raw/2024.parquet", frame(full_session_bars(DAY, DAY)))
    assert cli.main(["--providers", "massive,alpaca", "--symbols", "QQQ", "--start", "2024-01-02", "--end", "2024-01-02",
                     "--report-dir", str(tmp_path / "rep")]) == 1
    assert "alpaca-iex: NO STORED FILE" in capsys.readouterr().out
    assert cli.main(["--providers", "massive", "--start", "2024-01-02", "--end", "2024-01-02"]) == 2
    assert cli.main(["--providers", "massive,eodhd", "--start", "2024-01-02", "--end", "2024-01-02"]) == 2


# --------------------------------------------------------------------------- credential redaction
class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: "
                                     f"https://api.massive.com/v2/aggs?adjusted=false&apiKey=SUPERSECRET")


class _Sess:
    def __init__(self, behavior):
        self.behavior = behavior

    def request(self, *a, **k):
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def test_http_error_message_redacts_query_credentials_and_keeps_provider_reason():
    with pytest.raises(requests.HTTPError) as ei:
        request_with_retry("GET", "https://api.massive.com/v2/aggs?apiKey=SUPERSECRET", session=_Sess(
            _Resp(403, '{"status":"NOT_AUTHORIZED","message":"plan does not include this timeframe"}')), max_retries=1)
    msg = str(ei.value)
    assert "SUPERSECRET" not in msg and "apiKey=REDACTED" in msg and "plan does not include" in msg
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_connection_error_after_retries_is_redacted(monkeypatch):
    monkeypatch.setattr("src.data.http_utils.time.sleep", lambda s: None)
    err = requests.ConnectionError("Max retries exceeded with url: /v2/aggs?limit=5&apiKey=SUPERSECRET")
    with pytest.raises(FetchError) as ei:
        request_with_retry("GET", "https://api.massive.com/v2/aggs", session=_Sess(err), max_retries=2)
    assert "SUPERSECRET" not in str(ei.value) and "REDACTED" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None
