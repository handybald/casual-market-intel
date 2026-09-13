"""Market OHLCV validation.

Never auto-fills missing bars -- the point is to make gaps/anomalies
observable, not paper over them. All checks are read-only reports.

Session expectations (which calendar days should have data, and what
each session's actual open/close boundaries are, including early
closes) come from `pandas_market_calendars`' NYSE calendar rather than
a hand-rolled "9:30-16:00, no holidays" assumption -- that hand-rolled
version could not tell "no rows because it's a Sunday" from "no rows
because collection failed on a real trading day", and always reported
390 expected minutes even on an early-close day.

Validation is scoped to an explicit [start, end] range, NOT always a
full calendar year: a fetch of Sept 1-10 must only be judged against
the NYSE sessions that actually fall in Sept 1-10, not against the
entire year (which would spuriously report every other month's
sessions as "missing" from a 10-day fetch). `scripts/validate_data.py`
derives its own requested range from the manifest (see that module);
`fetch/massive.py` passes the exact requested chunk.

COMPLETENESS, not just presence: a session with even one bar used to be
excluded from `missing_sessions` and therefore counted "clean" --
0.26% coverage (1 of 390 expected minutes) is not complete. Every
session in range gets an EXPLICIT expected-minute grid (via
`timeframe_minutes`) and is classified into exactly one of:
  - future            : hasn't started yet as of `as_of` -- not
                         expected at all yet, not a failure.
  - provisional        : currently in progress as of `as_of` -- only
                         the ELAPSED portion is checked; the rest is
                         simply not due yet, never flagged missing.
  - missing            : fully elapsed, zero bars.
  - incomplete         : fully elapsed, some bars, but below
                         `min_session_completeness_ratio` of elapsed
                         expected minutes.
  - complete           : fully elapsed and at/above the completeness
                         threshold (small gaps -- e.g. genuine
                         no-qualifying-trade minutes -- are tolerated
                         below that threshold; this is a deliberate,
                         configurable line, not "every missing bar is a
                         failure").
Session-open/close EDGE minutes are checked explicitly in addition to
the aggregate ratio, since a threshold alone could still pass a session
missing exactly its open or close print.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")

# Default macro-release-window requirement: the release minute itself,
# one minute of pre-release context, and the immediate post-release
# reaction window. Explicit and documented -- NOT "some N bars nearby".
DEFAULT_MACRO_REQUIRED_OFFSETS_MINUTES = [-1, 0, 1, 2, 5]

DEFAULT_MIN_SESSION_COMPLETENESS_RATIO = 0.98


@dataclass
class MarketValidationReport:
    symbol: str
    start: str  # ISO date, inclusive
    end: str  # ISO date, inclusive
    as_of: str = ""  # ISO datetime the validation was evaluated against
    total_rows: int = 0
    is_sorted: bool = True
    duplicate_timestamp_count: int = 0
    null_or_nonfinite_count: int = 0
    invalid_ohlc_count: int = 0
    non_positive_price_count: int = 0
    negative_volume_count: int = 0
    suspicious_volume_count: int = 0  # > 10x the symbol-window median volume

    gap_count: int = 0
    largest_gap_minutes: float = 0.0

    extended_hours_rows: int = 0
    regular_hours_rows: int = 0

    expected_trading_sessions: int = 0  # sessions that have at least started as of `as_of` (future ones excluded)
    trading_sessions_with_data: int = 0
    missing_sessions: List[str] = field(default_factory=list)  # fully elapsed, zero rows
    incomplete_sessions: List[str] = field(default_factory=list)  # fully elapsed, some rows, below completeness threshold
    provisional_sessions: List[str] = field(default_factory=list)  # currently in progress as of `as_of`
    sessions_missing_open_edge: List[str] = field(default_factory=list)
    sessions_missing_close_edge: List[str] = field(default_factory=list)
    min_session_completeness_ratio: float = DEFAULT_MIN_SESSION_COMPLETENESS_RATIO
    expected_regular_minutes: int = 0  # sum of ELAPSED expected minutes only (early-close and as-of aware)
    regular_hours_coverage_ratio: float = 0.0

    macro_windows_checked: int = 0
    macro_required_offsets_minutes: List[int] = field(default_factory=lambda: list(DEFAULT_MACRO_REQUIRED_OFFSETS_MINUTES))
    macro_windows_missing_coverage: List[str] = field(default_factory=list)  # release ISO datetimes missing >=1 required offset
    macro_windows_missing_offsets: Dict[str, List[int]] = field(default_factory=dict)  # release ISO -> which offsets (minutes) were missing

    issues: List[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return (
            self.is_sorted
            and self.duplicate_timestamp_count == 0
            and self.null_or_nonfinite_count == 0
            and self.invalid_ohlc_count == 0
            and self.non_positive_price_count == 0
            and self.negative_volume_count == 0
            and not self.missing_sessions
            and not self.incomplete_sessions
            and not self.sessions_missing_open_edge
            and not self.sessions_missing_close_edge
            and not self.macro_windows_missing_coverage
        )

    @property
    def is_hard_failure(self) -> bool:
        """Structural/statistical/completeness problems severe enough
        that this data should not be trusted as a finalized checkpoint
        -- distinct from `is_clean`, which also counts a missing macro
        window (a real but softer concern that depends on external
        macro data being available) as "not clean". Callers deciding
        whether to FINALIZE a fetch should use this, not `is_clean`.
        A nearly-empty (incomplete) session is a hard failure -- it must
        never be finalized as complete. A provisional (still in
        progress) session is NOT a hard failure."""
        return (
            not self.is_sorted
            or self.duplicate_timestamp_count > 0
            or self.null_or_nonfinite_count > 0
            or self.invalid_ohlc_count > 0
            or self.non_positive_price_count > 0
            or self.negative_volume_count > 0
            or bool(self.missing_sessions)
            or bool(self.incomplete_sessions)
            or bool(self.sessions_missing_open_edge)
            or bool(self.sessions_missing_close_edge)
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "start": self.start,
            "end": self.end,
            "as_of": self.as_of,
            "total_rows": self.total_rows,
            "is_clean": self.is_clean,
            "is_hard_failure": self.is_hard_failure,
            "is_sorted": self.is_sorted,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "null_or_nonfinite_count": self.null_or_nonfinite_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "non_positive_price_count": self.non_positive_price_count,
            "negative_volume_count": self.negative_volume_count,
            "suspicious_volume_count": self.suspicious_volume_count,
            "gap_count": self.gap_count,
            "largest_gap_minutes": self.largest_gap_minutes,
            "extended_hours_rows": self.extended_hours_rows,
            "regular_hours_rows": self.regular_hours_rows,
            "expected_trading_sessions": self.expected_trading_sessions,
            "trading_sessions_with_data": self.trading_sessions_with_data,
            "missing_sessions": self.missing_sessions,
            "incomplete_sessions": self.incomplete_sessions,
            "provisional_sessions": self.provisional_sessions,
            "sessions_missing_open_edge": self.sessions_missing_open_edge,
            "sessions_missing_close_edge": self.sessions_missing_close_edge,
            "min_session_completeness_ratio": self.min_session_completeness_ratio,
            "expected_regular_minutes": self.expected_regular_minutes,
            "regular_hours_coverage_ratio": self.regular_hours_coverage_ratio,
            "macro_windows_checked": self.macro_windows_checked,
            "macro_required_offsets_minutes": self.macro_required_offsets_minutes,
            "macro_windows_missing_coverage": self.macro_windows_missing_coverage,
            "macro_windows_missing_offsets": self.macro_windows_missing_offsets,
            "issues": self.issues,
        }


def nyse_sessions(start: dt.date, end: dt.date) -> pd.DataFrame:
    """Real NYSE sessions in [start, end] with UTC-aware open/close per
    day -- handles weekends, holidays, and early closes automatically."""
    return _NYSE.schedule(start_date=start.isoformat(), end_date=end.isoformat())


def year_range(year: int) -> tuple:
    return dt.date(year, 1, 1), dt.date(year, 12, 31)


class UnsupportedTimeframeError(ValueError):
    """Raised when a (multiplier, timespan) pair cannot be validated by
    this pipeline's per-minute intraday session-completeness model --
    callers MUST reject the request before issuing any network call,
    never silently fall back to treating it as 1-minute bars."""


def timeframe_minutes_from_parts(multiplier: int, timespan: str) -> int:
    """The ONE place that converts a (multiplier, timespan) pair -- as
    returned by fetch/massive.py's `parse_timeframe` -- into the integer
    number of minutes `validate_market_bars`'s per-minute expected-grid
    model needs.

    Both fetch-time validation (`_validate_window` in
    fetch/massive.py) and the standalone `scripts/validate_data.py` call
    this SAME function so they can never diverge on what a given
    timeframe means. Previously `scripts/validate_data.py` did this
    conversion itself (with its own inline ternary) while fetch-time
    validation didn't do it at all -- `_validate_window` never passed
    `timeframe_minutes` through, so `validate_market_bars` silently used
    its 1-minute default regardless of the actually-configured
    timeframe. A complete, correctly-spaced 5-minute response (78 bars
    for a full regular session) was then validated against a
    390-minute-expected grid and always failed.

    Only "minute" and "hour" are supported -- both genuinely intraday
    and exactly expressible as a whole number of minutes. Daily (or
    coarser) bars need a fundamentally different, per-session (not
    per-minute) completeness model that this pipeline does not
    implement; that is a deliberate scope decision, not an oversight --
    such timeframes are explicitly rejected here rather than silently
    validated as if they were 1-minute bars.
    """
    if multiplier <= 0:
        raise UnsupportedTimeframeError(
            f"invalid timeframe multiplier {multiplier!r}: must be a positive integer"
        )
    if timespan == "minute":
        return multiplier
    if timespan == "hour":
        return multiplier * 60
    raise UnsupportedTimeframeError(
        f"timeframe unit {timespan!r} is not supported by this pipeline's per-minute session "
        f"validation model -- only intraday minute/hour timeframes are supported. Daily (or "
        f"coarser) bars would need a dedicated per-session validator, which does not exist, and "
        f"must never be silently validated as if they were 1-minute bars."
    )


def validate_market_bars(
    df: pd.DataFrame,
    symbol: str,
    start: dt.date,
    end: dt.date,
    timeframe_minutes: int = 1,
    gap_threshold_minutes: float = 5.0,
    macro_release_timestamps_utc: Optional[List[dt.datetime]] = None,
    macro_required_offsets_minutes: Optional[List[int]] = None,
    as_of: Optional[dt.datetime] = None,
    min_session_completeness_ratio: float = DEFAULT_MIN_SESSION_COMPLETENESS_RATIO,
) -> MarketValidationReport:
    """Validate `df` (expected to already be clipped to [start, end], but
    not required to be -- rows outside the range simply don't match any
    session and show up in `extended_hours_rows`) against real NYSE
    session expectations for exactly that range.

    `as_of` is the acquisition/evaluation instant: sessions entirely
    after it are not "expected" yet (not a failure); a session in
    progress as of `as_of` is `provisional` and only checked against its
    ELAPSED minutes. Defaults to `now()` (UTC) -- pass an explicit value
    for deterministic tests or to validate stored historical data
    without today's real wall-clock time being involved.

    `macro_required_offsets_minutes` (default:
    DEFAULT_MACRO_REQUIRED_OFFSETS_MINUTES) are EXPLICIT minute offsets
    from each release timestamp that must ALL have a bar present -- not
    "at least N bars somewhere nearby". A window is only satisfied when
    every required offset is covered.

    COARSE-BAR PRECISION NOTE: each offset's "hit" check looks for a bar
    starting in [release_ts + offset, release_ts + offset + timeframe) --
    this is exact for 1-minute bars, but for a coarser `timeframe_minutes`
    (e.g. 5-minute bars) several nearby offsets can be satisfied by the
    SAME underlying bar. This is used as-is (not redesigned into a
    dedicated coarse-bar model), but callers must never read a satisfied
    coarse-bar macro-window check as proving minute-level precision
    around the release -- it only proves a bar exists somewhere in that
    (possibly multi-minute-wide) window.
    """
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    required_offsets = (
        list(macro_required_offsets_minutes)
        if macro_required_offsets_minutes is not None
        else list(DEFAULT_MACRO_REQUIRED_OFFSETS_MINUTES)
    )

    report = MarketValidationReport(
        symbol=symbol, start=start.isoformat(), end=end.isoformat(), as_of=as_of.isoformat(),
        total_rows=len(df), min_session_completeness_ratio=min_session_completeness_ratio,
        macro_required_offsets_minutes=required_offsets,
    )

    ts = pd.to_datetime(df["timestamp_utc"], utc=True) if not df.empty else pd.Series([], dtype="datetime64[ns, UTC]")

    if not df.empty:
        report.is_sorted = bool(ts.is_monotonic_increasing)
        if not report.is_sorted:
            report.issues.append("timestamps not sorted")

        report.duplicate_timestamp_count = int(ts.duplicated().sum())
        if report.duplicate_timestamp_count:
            report.issues.append(f"{report.duplicate_timestamp_count} duplicate timestamps")

        o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
        finite_mask = (
            np.isfinite(pd.to_numeric(o, errors="coerce"))
            & np.isfinite(pd.to_numeric(h, errors="coerce"))
            & np.isfinite(pd.to_numeric(l, errors="coerce"))
            & np.isfinite(pd.to_numeric(c, errors="coerce"))
            & np.isfinite(pd.to_numeric(v, errors="coerce"))
        )
        report.null_or_nonfinite_count = int((~finite_mask).sum())
        if report.null_or_nonfinite_count:
            report.issues.append(f"{report.null_or_nonfinite_count} rows with null/non-finite OHLCV values")

        # Every check below runs ONLY on finite rows -- comparing NaN with
        # `<`/`>` silently evaluates False in pandas, which previously let
        # a null-valued row pass every other check undetected.
        sub = df.loc[finite_mask]
        if not sub.empty:
            so, sh, sl, sc, sv = sub["open"], sub["high"], sub["low"], sub["close"], sub["volume"]
            invalid_ohlc = (sh < sl) | (sh < so) | (sh < sc) | (sl > so) | (sl > sc)
            report.invalid_ohlc_count = int(invalid_ohlc.sum())
            if report.invalid_ohlc_count:
                report.issues.append(f"{report.invalid_ohlc_count} rows with invalid OHLC relationships")

            non_positive = (so <= 0) | (sh <= 0) | (sl <= 0) | (sc <= 0)
            report.non_positive_price_count = int(non_positive.sum())
            if report.non_positive_price_count:
                report.issues.append(f"{report.non_positive_price_count} rows with zero/negative price")

            report.negative_volume_count = int((sv < 0).sum())
            if report.negative_volume_count:
                report.issues.append(f"{report.negative_volume_count} rows with negative volume")

            positive_vol = sv[sv > 0]
            if not positive_vol.empty:
                median_vol = positive_vol.median()
                report.suspicious_volume_count = int((sv > median_vol * 10).sum())

    # -- session coverage: real NYSE calendar, scoped to [start, end],
    # with EXPLICIT expected-minute grids and as-of-aware elapsed windows --
    sessions = nyse_sessions(start, end)
    timeframe = dt.timedelta(minutes=timeframe_minutes)

    regular_hits = 0
    gaps_all: List[float] = []
    expected_sessions_count = 0

    for session_date, row in sessions.iterrows():
        open_utc, close_utc = row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime()
        date_str = session_date.date().isoformat() if hasattr(session_date, "date") else str(session_date)

        if open_utc > as_of:
            # Hasn't started yet -- not expected at all, not a failure.
            continue

        elapsed_close = min(close_utc, as_of)
        is_in_progress = as_of < close_utc

        in_session = ts[(ts >= open_utc) & (ts < close_utc)] if not df.empty else pd.Series([], dtype="datetime64[ns, UTC]")
        elapsed_bars = in_session[in_session < elapsed_close]

        expected_sessions_count += 1
        regular_hits += len(in_session)

        # Explicit expected-minute grid for the ELAPSED portion only.
        expected_minutes = []
        t = open_utc
        while t < elapsed_close:
            expected_minutes.append(t)
            t += timeframe
        expected_count = len(expected_minutes)
        report.expected_regular_minutes += expected_count

        present_minutes = set(elapsed_bars)
        # A bar "covers" an expected minute if it falls in [minute, minute+timeframe).
        # For timeframe_minutes=1 this is exact-timestamp matching; expressed
        # generally so a coarser configured timeframe still works.
        covered = 0
        first_missing_open = False
        last_missing_close = False
        if expected_minutes:
            for i, minute in enumerate(expected_minutes):
                next_minute = minute + timeframe
                hit = any(minute <= p < next_minute for p in present_minutes)
                if hit:
                    covered += 1
                elif i == 0:
                    first_missing_open = True
                elif i == len(expected_minutes) - 1:
                    last_missing_close = True

        completeness = (covered / expected_count) if expected_count else 1.0

        if is_in_progress:
            report.provisional_sessions.append(date_str)
        elif expected_count == 0:
            # Elapsed session with zero expected minutes (shouldn't
            # normally happen for a real NYSE session, but keep this
            # branch honest rather than dividing by zero).
            continue
        elif covered == 0:
            report.missing_sessions.append(date_str)
        elif completeness < min_session_completeness_ratio:
            report.incomplete_sessions.append(date_str)
        else:
            report.trading_sessions_with_data += 1

        if not is_in_progress:
            if first_missing_open:
                report.sessions_missing_open_edge.append(date_str)
            if last_missing_close:
                report.sessions_missing_close_edge.append(date_str)

        sorted_session = in_session.sort_values()
        diffs = sorted_session.diff().dt.total_seconds().div(60.0).dropna()
        session_gaps = diffs[diffs > gap_threshold_minutes]
        gaps_all.extend(session_gaps.tolist())

    report.expected_trading_sessions = expected_sessions_count
    report.regular_hours_rows = regular_hits
    report.extended_hours_rows = report.total_rows - regular_hits
    report.gap_count = len(gaps_all)
    report.largest_gap_minutes = float(max(gaps_all)) if gaps_all else 0.0
    report.regular_hours_coverage_ratio = (
        report.regular_hours_rows / report.expected_regular_minutes if report.expected_regular_minutes else 0.0
    )
    if report.missing_sessions:
        report.issues.append(
            f"{len(report.missing_sessions)} expected NYSE trading session(s) have ZERO rows "
            f"(first: {report.missing_sessions[0]})"
        )
    if report.incomplete_sessions:
        report.issues.append(
            f"{len(report.incomplete_sessions)} session(s) below {min_session_completeness_ratio:.0%} "
            f"completeness (first: {report.incomplete_sessions[0]})"
        )
    if report.sessions_missing_open_edge:
        report.issues.append(f"{len(report.sessions_missing_open_edge)} session(s) missing their opening minute")
    if report.sessions_missing_close_edge:
        report.issues.append(f"{len(report.sessions_missing_close_edge)} session(s) missing their closing minute")

    # -- macro-event-window coverage: explicit required offsets, not "N bars nearby" --
    if macro_release_timestamps_utc:
        checked = 0
        for release_ts in macro_release_timestamps_utc:
            if release_ts.date() < start or release_ts.date() > end:
                continue
            if release_ts > as_of:
                continue  # hasn't happened yet -- not expected
            checked += 1
            missing_offsets = []
            for offset in required_offsets:
                target = release_ts + dt.timedelta(minutes=offset)
                target_next = target + timeframe
                hit = not ts.empty and bool(((ts >= target) & (ts < target_next)).any())
                if not hit:
                    missing_offsets.append(offset)
            if missing_offsets:
                report.macro_windows_missing_coverage.append(release_ts.isoformat())
                report.macro_windows_missing_offsets[release_ts.isoformat()] = missing_offsets
        report.macro_windows_checked = checked
        if report.macro_windows_missing_coverage:
            report.issues.append(
                f"{len(report.macro_windows_missing_coverage)} macro release window(s) missing one or "
                f"more required offset(s) {required_offsets}"
            )

    return report
