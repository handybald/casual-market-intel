"""Generic date-range chunking.

This is the one utility that lets every fetcher accept a single
(start_date, end_date) from the user while internally issuing whatever
provider-specific request windows it needs (monthly pages, yearly
partitions, etc). Provider chunking policy lives in each fetch module /
config/data_sources.yaml, not here -- this module only knows how to
slice a date range.
"""
from __future__ import annotations

import datetime as dt
from calendar import monthrange
from dataclasses import dataclass
from typing import Iterator, Literal

Frequency = Literal["day", "month", "year"]


@dataclass(frozen=True)
class DateChunk:
    start: dt.date
    end: dt.date  # inclusive

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"chunk end {self.end} before start {self.start}")

    @property
    def label(self) -> str:
        if self.start.year == self.end.year and self.start.month == self.end.month:
            return f"{self.start:%Y-%m}"
        return f"{self.start.isoformat()}_{self.end.isoformat()}"


def _month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _month_end(d: dt.date) -> dt.date:
    last_day = monthrange(d.year, d.month)[1]
    return d.replace(day=last_day)


def _next_month_start(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year + 1, 1, 1)
    return dt.date(d.year, d.month + 1, 1)


def _year_start(d: dt.date) -> dt.date:
    return dt.date(d.year, 1, 1)


def _year_end(d: dt.date) -> dt.date:
    return dt.date(d.year, 12, 31)


def _next_year_start(d: dt.date) -> dt.date:
    return dt.date(d.year + 1, 1, 1)


def iter_date_chunks(
    start: dt.date, end: dt.date, frequency: Frequency = "month"
) -> Iterator[DateChunk]:
    """Yield inclusive (start, end) DateChunks covering [start, end].

    Chunk boundaries always align to calendar months/years (so a
    provider that wants "one request per month" gets full, well-formed
    months), but the first and last chunk are clipped to the requested
    range. Leap years and year boundaries are handled via
    `calendar.monthrange`, never hard-coded day counts.
    """
    if end < start:
        raise ValueError(f"end {end} before start {start}")

    if frequency == "day":
        cursor = start
        while cursor <= end:
            yield DateChunk(cursor, cursor)
            cursor += dt.timedelta(days=1)
        return

    if frequency == "month":
        cursor = start
        while cursor <= end:
            chunk_end = min(_month_end(cursor), end)
            yield DateChunk(cursor, chunk_end)
            cursor = _next_month_start(cursor)
        return

    if frequency == "year":
        cursor = start
        while cursor <= end:
            chunk_end = min(_year_end(cursor), end)
            yield DateChunk(cursor, chunk_end)
            cursor = _next_year_start(cursor)
        return

    raise ValueError(f"unknown frequency: {frequency!r}")


def iter_year_month_pairs(start: dt.date, end: dt.date) -> Iterator[tuple]:
    """Convenience: yield (year, month) integer pairs covering [start, end]."""
    for chunk in iter_date_chunks(start, end, frequency="month"):
        yield chunk.start.year, chunk.start.month
