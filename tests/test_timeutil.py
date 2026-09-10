import datetime as dt

from src.data.timeutil import localize, to_utc, utc_to_ny, normalize_timestamp, UNKNOWN_TZ


def test_ny_winter_is_utc_minus_5():
    naive = dt.datetime(2024, 1, 15, 8, 30)  # 8:30am ET in January (EST)
    aware = localize(naive, "America/New_York")
    utc = to_utc(aware)
    assert utc.hour == 13  # UTC-5
    assert utc.tzinfo == dt.timezone.utc


def test_ny_summer_is_utc_minus_4_dst():
    naive = dt.datetime(2024, 7, 15, 8, 30)  # 8:30am ET in July (EDT)
    aware = localize(naive, "America/New_York")
    utc = to_utc(aware)
    assert utc.hour == 12  # UTC-4 due to DST


def test_utc_to_ny_roundtrip_preserves_dst_correctly():
    utc_dt = dt.datetime(2024, 7, 15, 12, 30, tzinfo=dt.timezone.utc)
    ny = utc_to_ny(utc_dt)
    assert ny.hour == 8
    assert ny.utcoffset() == dt.timedelta(hours=-4)


def test_normalize_timestamp_with_known_timezone():
    naive = dt.datetime(2024, 1, 15, 8, 30)
    result = normalize_timestamp(naive, "America/New_York")
    assert result.trusted is True
    assert result.utc.hour == 13


def test_normalize_timestamp_with_unknown_timezone_is_not_trusted():
    naive = dt.datetime(2024, 1, 15, 8, 30)
    result = normalize_timestamp(naive, UNKNOWN_TZ)
    assert result.trusted is False
    assert result.utc is None


def test_to_utc_requires_aware_datetime():
    import pytest

    with pytest.raises(ValueError):
        to_utc(dt.datetime(2024, 1, 1))
