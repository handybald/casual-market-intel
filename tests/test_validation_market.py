import datetime as dt

import pandas as pd

from src.data.validation.market import validate_market_bars


def _bar(ts, o, h, l, c, v):
    return {"timestamp_utc": ts, "open": o, "high": h, "low": l, "close": c, "volume": v, "vwap": o, "transactions": 1}


def test_clean_data_reports_no_issues():
    base = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)  # 9:30am ET
    rows = [_bar(base + dt.timedelta(minutes=i), 100, 101, 99, 100.5, 1000) for i in range(5)]
    df = pd.DataFrame(rows)
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.is_clean
    assert report.duplicate_timestamp_count == 0
    assert report.invalid_ohlc_count == 0


def test_detects_duplicate_timestamps():
    ts = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    df = pd.DataFrame([_bar(ts, 100, 101, 99, 100, 1000), _bar(ts, 100, 101, 99, 100, 1000)])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.duplicate_timestamp_count == 1
    assert not report.is_clean


def test_detects_invalid_ohlc_relationship():
    ts = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    # high < low is invalid
    df = pd.DataFrame([_bar(ts, 100, 90, 99, 95, 1000)])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.invalid_ohlc_count == 1


def test_detects_non_positive_prices():
    ts = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    df = pd.DataFrame([_bar(ts, 0, 1, -1, 0.5, 1000)])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.non_positive_price_count == 1


def test_detects_unsorted_timestamps():
    t1 = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2020, 1, 2, 14, 29, tzinfo=dt.timezone.utc)
    df = pd.DataFrame([_bar(t1, 100, 101, 99, 100, 1000), _bar(t2, 100, 101, 99, 100, 1000)])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.is_sorted is False


def test_detects_intraday_gap():
    base = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)
    rows = [
        _bar(base, 100, 101, 99, 100, 1000),
        _bar(base + dt.timedelta(minutes=1), 100, 101, 99, 100, 1000),
        _bar(base + dt.timedelta(minutes=30), 100, 101, 99, 100, 1000),  # 29-minute gap
    ]
    df = pd.DataFrame(rows)
    report = validate_market_bars(df, "QQQ", 2020, gap_threshold_minutes=5.0)
    assert report.gap_count == 1
    assert report.largest_gap_minutes == 29.0


def test_regular_vs_extended_hours_classification():
    # 9:30am ET regular session bar, and a 4am ET pre-market bar (same UTC day)
    regular = dt.datetime(2020, 1, 2, 14, 30, tzinfo=dt.timezone.utc)  # 9:30am ET
    premarket = dt.datetime(2020, 1, 2, 9, 0, tzinfo=dt.timezone.utc)  # 4:00am ET
    df = pd.DataFrame([_bar(premarket, 100, 101, 99, 100, 1000), _bar(regular, 100, 101, 99, 100, 1000)])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.regular_hours_rows == 1
    assert report.extended_hours_rows == 1


def test_empty_dataframe_does_not_crash():
    df = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"])
    report = validate_market_bars(df, "QQQ", 2020)
    assert report.total_rows == 0
    assert "no rows" in report.issues
