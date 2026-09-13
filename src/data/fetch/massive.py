"""Massive market data client (1-minute OHLCV bars).

Implements the documented Massive v2 aggregates endpoint:
https://www.massive.com/docs/rest/stocks/aggregates/custom-bars

    GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
    ?adjusted={true|false}&sort=asc&limit=<n>&apiKey=<key>

Response contract (as documented): {"status": "OK"|"DELAYED"|"ERROR"|...,
"results": [{"t": epoch_ms, "o","h","l","c","v","vw","n"}, ...],
"next_url": "<full url, no apiKey>"|absent}. A response whose "status" is
missing or not OK/DELAYED is treated as an error, never a successful
empty chunk. Pagination follows `next_url` (re-adding the API key, since
the documented `next_url` omits it) with a seen-URL set to detect a
provider bug that returns the same page forever.

LIVE-VERIFIED (2026-09-10, using a real but plan-limited MASSIVE_API_KEY):
- `_parse_bars_response`'s field mapping (t/o/h/l/c/v/vw/n) is CONFIRMED
  correct against a real 200 OK response (GET .../ticker/QQQ/prev).
- `_validate_response_payload`'s status/error handling is CONFIRMED
  correct against a real error response: requesting 1-minute (and even
  daily) range aggregates on this key returned HTTP 403 with
  {"status": "NOT_AUTHORIZED", "message": "Your plan doesn't include
  this data timeframe..."} -- i.e. the endpoint URL, auth parameter, and
  response contract are all right; that specific key's plan tier simply
  doesn't include historical range aggregates. This is an account/plan
  limitation, not a code defect -- a full historical bootstrap has NOT
  been run and 1-minute bar retrieval is NOT end-to-end live-verified.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import pandas as pd
import requests

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..manifest import Manifest, ManifestEntry, checksum_file, utcnow_iso
from ..http_utils import request_with_retry
from ..validation.market import (
    MarketValidationReport,
    UnsupportedTimeframeError,
    timeframe_minutes_from_parts,
    validate_market_bars,
)

logger = logging.getLogger(__name__)

BAR_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"]
_OK_STATUSES = {"OK", "DELAYED"}


class MissingCredentialsError(RuntimeError):
    pass


class MassiveResponseError(RuntimeError):
    """Raised for a malformed/error API response -- never silently
    swallowed into an empty-but-"successful" chunk."""


class MonthResult(NamedTuple):
    symbol: str
    year: int
    month: int
    status: str  # "complete" | "provisional" | "empty" | "failed" | "skipped_cached"
    rows: int
    error: Optional[str] = None


def parse_timeframe(timeframe: str) -> Tuple[int, str]:
    """"1min" -> (1, "minute"); "5min" -> (5, "minute"); "1h" -> (1, "hour");
    "1day"/"1d" -> (1, "day")."""
    match = re.match(r"^(\d+)\s*(min|minute|h|hour|day|d)$", timeframe.strip().lower())
    if not match:
        raise ValueError(f"unrecognized timeframe: {timeframe!r}")
    n = int(match.group(1))
    unit = match.group(2)
    timespan = {"min": "minute", "minute": "minute", "h": "hour", "hour": "hour", "day": "day", "d": "day"}[unit]
    return n, timespan


def cache_key(symbol: str, timeframe: str, adjusted: bool) -> str:
    """Cache/manifest identity: MUST include timeframe and adjustment
    policy, or a 1min/raw checkpoint could be silently reused to answer
    a 5min/adjusted request."""
    return f"{symbol}:{timeframe}:{'adjusted' if adjusted else 'raw'}"


def _validate_response_payload(payload: dict) -> None:
    status = payload.get("status")
    if status is None:
        raise MassiveResponseError("malformed response: missing 'status' field")
    if status not in _OK_STATUSES:
        detail = payload.get("error") or payload.get("message") or "no detail provided"
        raise MassiveResponseError(f"API error status={status!r}: {detail}")


def _parse_bars_response(payload: dict) -> List[dict]:
    raw_bars = payload.get("results") or []
    out = []
    for b in raw_bars:
        ts_raw = b.get("t")
        if ts_raw is None:
            continue
        if isinstance(ts_raw, (int, float)):
            timestamp = dt.datetime.fromtimestamp(ts_raw / 1000.0, tz=dt.timezone.utc)
        else:
            timestamp = pd.to_datetime(ts_raw, utc=True).to_pydatetime()
        out.append(
            {
                "timestamp_utc": timestamp,
                "open": b.get("o"),
                "high": b.get("h"),
                "low": b.get("l"),
                "close": b.get("c"),
                "volume": b.get("v"),
                "vwap": b.get("vw"),
                "transactions": b.get("n"),
            }
        )
    return out


def fetch_window_bars(
    config: AppConfig,
    symbol: str,
    start: dt.date,
    end: dt.date,
    multiplier: int,
    timespan: str,
    adjusted: bool,
    session: requests.Session,
    api_key: str,
) -> List[dict]:
    """Fetch every bar in [start, end] (inclusive), following documented
    `next_url` pagination while preserving auth, with a loop guard against
    a repeated/duplicate pagination link."""
    provider_cfg = config.provider("massive")
    base_url = provider_cfg["base_url"]
    page_limit = provider_cfg.get("page_limit", 50000)
    max_retries = provider_cfg.get("max_retries", 5)
    request_delay = provider_cfg.get("request_delay_seconds", 0.25)
    sort = provider_cfg.get("sort", "asc")

    url = f"{base_url}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}"
    params = {
        "adjusted": "true" if adjusted else "false",
        "sort": sort,
        "limit": str(page_limit),
        "apiKey": api_key,
    }

    all_bars: List[dict] = []
    seen_urls: set = set()
    next_url: Optional[str] = url

    while next_url:
        if next_url in seen_urls:
            raise MassiveResponseError(f"repeated pagination link detected (possible loop): {next_url}")
        seen_urls.add(next_url)

        request_params = params if next_url == url else {"apiKey": api_key}
        response = request_with_retry(
            "GET",
            next_url,
            session=session,
            max_retries=max_retries,
            request_delay_seconds=request_delay,
            params=request_params,
        )
        payload = response.json()
        _validate_response_payload(payload)
        all_bars.extend(_parse_bars_response(payload))

        # The documented `next_url` does not include the API key -- it
        # must be re-added on every follow-up request or auth is lost.
        next_url = payload.get("next_url")

    return all_bars


def _year_parquet_path(config: AppConfig, symbol: str, timeframe: str, adjusted: bool, year: int) -> Path:
    adj_label = "adjusted" if adjusted else "raw"
    return config.provider_raw_dir("massive") / symbol / timeframe / adj_label / f"{year}.parquet"


def _merge_year_parquet(path: Path, new_rows: List[dict]) -> pd.DataFrame:
    new_df = pd.DataFrame(new_rows, columns=BAR_COLUMNS)
    existing = None
    if path.exists():
        try:
            existing = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - a corrupt/unreadable file is not fatal
            # Corruption should already have been caught (and the entries
            # backed by it invalidated) by
            # Manifest.invalidate_entries_for_missing_or_corrupt_path
            # before this runs -- but if we still can't read the bytes on
            # disk, the only safe move is to treat it as absent (rebuild
            # from this chunk's fresh data) rather than crash the whole
            # fetch run.
            logger.error("[Massive] %s is unreadable (%s) -- rebuilding from scratch", path, exc)
            existing = None
    combined = pd.concat([existing, new_df], ignore_index=True) if existing is not None else new_df

    combined = combined.drop_duplicates(subset=["timestamp_utc"], keep="last")
    combined = combined.sort_values("timestamp_utc").reset_index(drop=True)
    return combined


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_path, index=False)
    tmp_path.replace(path)


def _reverify_and_refresh_siblings(
    manifest: Manifest, key: str, year_path: Path, checksum: str, merged_df: pd.DataFrame
) -> None:
    """After writing/rewriting a shared year file, do NOT blindly bless
    every entry that happens to reference this path -- that was the
    original bug (`update_checksum_for_path` called unconditionally would
    silently "recover" a sibling checkpoint's trust even when its rows
    were no longer anywhere in the rebuilt file). A "complete" entry is
    only refreshed if its claimed date range is still actually
    represented in the current data; otherwise it is invalidated instead
    of blessed. An "empty" entry has no rows to check by construction, so
    row-presence can neither confirm nor contradict it -- it is refreshed
    as long as the artifact it points at is this one (missing/corrupt-file
    detection already happened up front, before this function runs, via
    `Manifest.invalidate_entries_for_missing_or_corrupt_path`).
    """
    path_str = str(year_path)
    ts = (
        pd.to_datetime(merged_df["timestamp_utc"], utc=True)
        if not merged_df.empty
        else pd.Series([], dtype="datetime64[ns, UTC]")
    )
    for e in manifest.entries_for("massive", key):
        if e.path != path_str or e.status not in ("complete", "empty"):
            continue
        if e.status == "complete":
            start_ts = pd.Timestamp(e.start_date(), tz="UTC")
            end_ts = pd.Timestamp(e.end_date(), tz="UTC") + pd.Timedelta(days=1)
            has_rows = bool(((ts >= start_ts) & (ts < end_ts)).any()) if not ts.empty else False
            if not has_rows:
                logger.warning(
                    "[Massive] %s..%s claimed complete but its rows are absent from the "
                    "rebuilt shared artifact -- invalidating, NOT refreshing its checksum",
                    e.start, e.end,
                )
                manifest.record(
                    ManifestEntry(
                        provider="massive", key=key, start=e.start, end=e.end,
                        status="failed", error="claimed rows absent from rebuilt shared artifact",
                        request_meta=e.request_meta,
                    )
                )
                continue
        if e.checksum != checksum:
            # Checksum-only reconciliation: this entry's OWN date range
            # was not refetched -- only the shared file it points at was
            # rewritten by a DIFFERENT chunk's fetch. `retrieved_at` must
            # keep reflecting when this entry's data was actually
            # acquired, not "now" -- `ManifestEntry`'s default factory
            # would otherwise silently overwrite it with the moment the
            # sibling chunk happened to run. `verified_at` records this
            # reconciliation event separately.
            manifest.record(
                ManifestEntry(
                    provider="massive", key=key, start=e.start, end=e.end,
                    status=e.status, rows=e.rows, retrieved_at=e.retrieved_at,
                    verified_at=utcnow_iso(), checksum=checksum, path=path_str,
                    error=e.error, request_meta=e.request_meta,
                )
            )


def _validate_window(
    bars: List[dict], symbol: str, start: dt.date, end: dt.date, timeframe_minutes: int = 1
) -> MarketValidationReport:
    """Validate just-fetched bars, scoped to the exact requested window.
    Macro-release-window coverage is intentionally NOT checked here --
    that needs normalized macro events from other providers, which this
    fetch call has no access to; scripts/validate_data.py covers that
    layer separately, against already-stored data.

    `timeframe_minutes` MUST be derived via
    `validation.market.timeframe_minutes_from_parts` (see
    `fetch_massive_symbol`) -- passing it through here is the fix for a
    real bug: this used to call `validate_market_bars` with no
    `timeframe_minutes` at all, silently defaulting to 1-minute
    expectations regardless of what timeframe was actually configured,
    so a complete 5-minute response was validated against a 390-minute
    grid and always failed.
    """
    df = pd.DataFrame(bars, columns=BAR_COLUMNS)
    return validate_market_bars(df, symbol, start, end, timeframe_minutes=timeframe_minutes)


def _validation_report_dir(config: AppConfig) -> Path:
    return config.manifest_path.parent / "validation_reports"


def _persist_validation_report(
    config: AppConfig, report: MarketValidationReport, symbol: str, start: dt.date, end: dt.date
) -> None:
    """Validation outcomes must be durable/inspectable, not just an
    in-memory decision that vanishes after this process exits."""
    out_dir = _validation_report_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"massive_{symbol}_{start.isoformat()}_{end.isoformat()}.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


def _is_provisional_window(chunk_end: dt.date, today: dt.date, revision_overlap_days: int) -> bool:
    """A window is provisional (never checkpointed "complete", always
    retried) if it touches "today" or falls within the trailing
    revision-overlap horizon -- late prints/corrections to recent
    sessions are expected, and an open/current month must stay
    refreshable rather than being finalized the moment it's first
    fetched."""
    return chunk_end >= (today - dt.timedelta(days=revision_overlap_days))


def fetch_massive_symbol(
    config: AppConfig,
    manifest: Manifest,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    timeframe: str,
    force: bool = False,
    session: Optional[requests.Session] = None,
    today: Optional[dt.date] = None,
) -> List[MonthResult]:
    api_key = config.env("MASSIVE_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "MASSIVE_API_KEY not set (see .env.example). Cannot fetch Massive market data."
        )

    provider_cfg = config.provider("massive")
    adjusted = bool(provider_cfg.get("adjusted", False))
    multiplier, timespan = parse_timeframe(timeframe)
    # Reject an unsupported/invalid timeframe BEFORE issuing any network
    # call -- never silently validate daily (or zero/negative-interval)
    # bars as if they were 1-minute bars. Raises UnsupportedTimeframeError.
    timeframe_minutes = timeframe_minutes_from_parts(multiplier, timespan)
    revision_overlap_days = int(provider_cfg.get("revision_overlap_days", 3))
    today = today or dt.date.today()

    key = cache_key(symbol, timeframe, adjusted)
    sess = session or requests.Session()
    results: List[MonthResult] = []
    invalidated_years: set = set()

    for chunk in iter_date_chunks(start_date, end_date, frequency="month"):
        # The exact requested (clipped) window -- NOT always the full
        # calendar month. This is the fix for the original bug: a
        # request for Sept 1-10 must checkpoint Sept 1-10, not Sept 1-30.
        req_start, req_end = chunk.start, chunk.end
        start_iso, end_iso = req_start.isoformat(), req_end.isoformat()
        provisional = _is_provisional_window(req_end, today, revision_overlap_days)

        year_path = _year_parquet_path(config, symbol, timeframe, adjusted, req_start.year)
        if req_start.year not in invalidated_years:
            # MUST happen before any is_complete() check or write this
            # run touches this year's file: if the shared artifact is
            # missing/corrupt, every checkpoint backed by it is
            # untrustworthy right now, not just the one we're about to
            # refetch. See Manifest.invalidate_entries_for_missing_or_corrupt_path.
            invalidated = manifest.invalidate_entries_for_missing_or_corrupt_path("massive", key, year_path)
            if invalidated:
                logger.warning(
                    "[Massive][%s] %s: backing artifact missing/corrupt -- invalidated %d checkpoint(s): %s",
                    symbol, year_path, len(invalidated),
                    ", ".join(f"{e.start}..{e.end}" for e in invalidated),
                )
            invalidated_years.add(req_start.year)

        if not force and not provisional and manifest.is_complete("massive", key, start_iso, end_iso):
            logger.info("[Massive][%s] %s already fetched, skipping", symbol, chunk.label)
            results.append(MonthResult(symbol, req_start.year, req_start.month, "skipped_cached", 0))
            continue

        try:
            bars = fetch_window_bars(config, symbol, req_start, req_end, multiplier, timespan, adjusted, sess, api_key)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            logger.error("[Massive][%s] %s FAILED: %s", symbol, chunk.label, exc)
            manifest.record(
                ManifestEntry(
                    provider="massive", key=key, start=start_iso, end=end_iso,
                    status="failed", error=str(exc),
                    request_meta={"timeframe": timeframe, "adjusted": adjusted},
                )
            )
            results.append(MonthResult(symbol, req_start.year, req_start.month, "failed", 0, error=str(exc)))
            continue

        # Validate BEFORE finalizing -- scoped to exactly this requested
        # window, not the whole year (a 10-day fetch must not be judged
        # against 11 other months' worth of "missing" sessions). The raw
        # bars are still written either way (never discard fetched data
        # over a validation concern -- see docstring), but a hard failure
        # or a suspicious empty response prevents the checkpoint from
        # being finalized as "complete"/"empty".
        window_report = _validate_window(bars, symbol, req_start, req_end, timeframe_minutes=timeframe_minutes)
        _persist_validation_report(config, window_report, symbol, req_start, req_end)

        if not bars:
            if window_report.expected_trading_sessions == 0:
                status = "empty"  # verified: genuinely no NYSE sessions in this window
                error = None
            else:
                # A normal trading window with ZERO bars is NOT the same
                # thing as a verified-empty window -- that was the bug:
                # an empty response used to be accepted as "empty" no
                # matter what the window actually covered.
                status = "failed"
                error = (
                    f"expected {window_report.expected_trading_sessions} NYSE trading session(s) "
                    f"in this window but received 0 bars -- treating as an incomplete response, "
                    f"not verified-empty"
                )
        elif window_report.is_hard_failure:
            status = "failed"
            error = f"validation hard failure: {'; '.join(window_report.issues)}"
        else:
            status = "provisional" if provisional else "complete"
            error = None

        # Durable, immediate write: merge+persist THIS chunk before moving
        # on, rather than batching a whole year in memory -- an
        # interruption after this point must not lose already-fetched
        # chunks. Raw bars are preserved even when `status` ends up
        # "failed" above -- the checkpoint isn't trusted, but the data
        # isn't thrown away either, so it stays available for diagnosis
        # and gets merged/superseded cleanly once a corrected fetch lands.
        merged = _merge_year_parquet(year_path, bars)
        _validate_chronological(merged, symbol, req_start.year)
        _atomic_write_parquet(merged, year_path)
        checksum = checksum_file(year_path)

        manifest.record(
            ManifestEntry(
                provider="massive", key=key, start=start_iso, end=end_iso,
                status=status, rows=len(bars), checksum=checksum, path=str(year_path), error=error,
                request_meta={"timeframe": timeframe, "adjusted": adjusted},
            )
        )
        # Now that THIS chunk's entry reflects the rebuilt file, check
        # every OTHER entry sharing the path individually -- never bless
        # a sibling just because the file we happened to write now has a
        # valid checksum (see docstring on _reverify_and_refresh_siblings).
        _reverify_and_refresh_siblings(manifest, key, year_path, checksum, merged)

        logger.info("[Massive][%s] %s %s: %d bars", symbol, chunk.label, status, len(bars))
        results.append(MonthResult(symbol, req_start.year, req_start.month, status, len(bars)))

    return results


def _validate_chronological(df: pd.DataFrame, symbol: str, year: int) -> None:
    if df.empty:
        return
    if not df["timestamp_utc"].is_monotonic_increasing:
        raise ValueError(f"[Massive][{symbol}] {year}: timestamps not sorted after merge")
    if df["timestamp_utc"].duplicated().any():
        raise ValueError(f"[Massive][{symbol}] {year}: duplicate timestamps after merge")


def fetch_massive_market_data(
    config: AppConfig,
    manifest: Manifest,
    symbols: List[str],
    start_date: dt.date,
    end_date: dt.date,
    timeframe: str,
    force: bool = False,
    today: Optional[dt.date] = None,
) -> Dict[str, List[MonthResult]]:
    session = requests.Session()
    out: Dict[str, List[MonthResult]] = {}
    for symbol in symbols:
        out[symbol] = fetch_massive_symbol(
            config, manifest, symbol, start_date, end_date, timeframe, force=force, session=session, today=today
        )
    return out
