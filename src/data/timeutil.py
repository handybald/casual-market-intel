"""Timezone-aware timestamp normalization.

Canonical storage is always UTC. Original provider timestamps and their
(claimed) timezone are always preserved alongside so mistakes are
auditable rather than silently baked in.

DST is handled via zoneinfo, never a fixed UTC offset -- "America/New_York"
is UTC-5 in January and UTC-4 in July, and hard-coding either is wrong
half the year.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
NY_TZ = ZoneInfo("America/New_York")

UNKNOWN_TZ = "UNKNOWN"


def localize(naive: dt.datetime, tz_name: str) -> dt.datetime:
    """Attach a named IANA timezone to a naive datetime."""
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def to_utc(aware: dt.datetime) -> dt.datetime:
    if aware.tzinfo is None:
        raise ValueError("to_utc requires a timezone-aware datetime")
    return aware.astimezone(UTC)


def utc_to_ny(aware_utc: dt.datetime) -> dt.datetime:
    return to_utc(aware_utc).astimezone(NY_TZ)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def normalize_timestamp(
    naive_or_aware: dt.datetime, source_timezone: str
) -> "TimestampNormalizationResult":
    """Normalize a provider timestamp to UTC, given the claimed source
    timezone. If source_timezone is UNKNOWN_TZ, returns trusted=False and
    utc=None rather than guessing -- callers must not silently assume UTC.
    """
    if source_timezone == UNKNOWN_TZ:
        return TimestampNormalizationResult(utc=None, trusted=False)

    if naive_or_aware.tzinfo is not None:
        aware = naive_or_aware
    else:
        aware = localize(naive_or_aware, source_timezone)
    return TimestampNormalizationResult(utc=to_utc(aware), trusted=True)


class TimestampNormalizationResult:
    __slots__ = ("utc", "trusted")

    def __init__(self, utc, trusted: bool):
        self.utc = utc
        self.trusted = trusted
