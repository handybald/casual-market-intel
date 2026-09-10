import datetime as dt

import pytest

from src.data.dates import iter_date_chunks, DateChunk


def test_month_chunks_basic():
    chunks = list(iter_date_chunks(dt.date(2020, 1, 1), dt.date(2020, 3, 31), "month"))
    assert [c.label for c in chunks] == ["2020-01", "2020-02", "2020-03"]
    assert chunks[0].start == dt.date(2020, 1, 1)
    assert chunks[0].end == dt.date(2020, 1, 31)
    assert chunks[1].end == dt.date(2020, 2, 29)  # 2020 is a leap year


def test_month_chunks_clip_partial_first_and_last_month():
    chunks = list(iter_date_chunks(dt.date(2020, 1, 15), dt.date(2020, 2, 10), "month"))
    assert chunks[0].start == dt.date(2020, 1, 15)
    assert chunks[0].end == dt.date(2020, 1, 31)
    assert chunks[1].start == dt.date(2020, 2, 1)
    assert chunks[1].end == dt.date(2020, 2, 10)


def test_month_chunks_cross_year_boundary():
    chunks = list(iter_date_chunks(dt.date(2019, 11, 1), dt.date(2020, 2, 29), "month"))
    labels = [c.label for c in chunks]
    assert labels == ["2019-11", "2019-12", "2020-01", "2020-02"]


def test_leap_year_february_29_included():
    chunks = list(iter_date_chunks(dt.date(2020, 2, 1), dt.date(2020, 2, 29), "month"))
    assert len(chunks) == 1
    assert chunks[0].end == dt.date(2020, 2, 29)


def test_non_leap_year_february_ends_28():
    chunks = list(iter_date_chunks(dt.date(2021, 2, 1), dt.date(2021, 2, 28), "month"))
    assert chunks[0].end == dt.date(2021, 2, 28)


def test_year_frequency():
    chunks = list(iter_date_chunks(dt.date(2016, 6, 1), dt.date(2018, 3, 1), "year"))
    assert [c.label for c in chunks] == [
        "2016-06-01_2016-12-31",
        "2017-01-01_2017-12-31",
        "2018-01-01_2018-03-01",
    ]


def test_day_frequency():
    chunks = list(iter_date_chunks(dt.date(2020, 1, 30), dt.date(2020, 2, 2), "day"))
    assert [c.start for c in chunks] == [
        dt.date(2020, 1, 30),
        dt.date(2020, 1, 31),
        dt.date(2020, 2, 1),
        dt.date(2020, 2, 2),
    ]


def test_single_day_range():
    chunks = list(iter_date_chunks(dt.date(2020, 1, 1), dt.date(2020, 1, 1), "month"))
    assert len(chunks) == 1
    assert chunks[0].start == dt.date(2020, 1, 1)
    assert chunks[0].end == dt.date(2020, 1, 1)


def test_end_before_start_raises():
    with pytest.raises(ValueError):
        list(iter_date_chunks(dt.date(2020, 2, 1), dt.date(2020, 1, 1), "month"))


def test_date_chunk_invalid_order_raises():
    with pytest.raises(ValueError):
        DateChunk(dt.date(2020, 1, 2), dt.date(2020, 1, 1))


def test_unknown_frequency_raises():
    with pytest.raises(ValueError):
        list(iter_date_chunks(dt.date(2020, 1, 1), dt.date(2020, 1, 2), "fortnight"))
