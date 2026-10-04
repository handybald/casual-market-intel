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
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from ..config import AppConfig
from ..http_utils import request_with_retry
from ..manifest import Manifest
from ..validation.market import BAR_DENSITY_DENSE
from .market_provider import (  # noqa: F401 - re-exported for existing callers
    BAR_COLUMNS,
    FEED_SCOPE_CONSOLIDATED,
    MarketDataProvider,
    MissingCredentialsError,
    MonthResult,
    ProviderCapabilities,
    ProviderResponseError,
    fetch_market_data,
    fetch_market_symbol,
    merge_year_parquet,
    parse_timeframe,
    register_market_provider,
    validation_report_dir,
)

logger = logging.getLogger(__name__)

_OK_STATUSES = {"OK", "DELAYED"}


class MassiveResponseError(ProviderResponseError):
    """Raised for a malformed/error API response -- never silently
    swallowed into an empty-but-"successful" chunk."""


def cache_key(symbol: str, timeframe: str, adjusted: bool) -> str:
    """Cache/manifest identity: MUST include timeframe and adjustment
    policy, or a 1min/raw checkpoint could be silently reused to answer
    a 5min/adjusted request. Unchanged from before the provider
    abstraction, so existing manifest checkpoints stay valid."""
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
            parsed = pd.Timestamp(ts_raw)
            if parsed.tzinfo is None:
                raise MassiveResponseError(f"bar timestamp {ts_raw!r} has no UTC offset -- refusing to assume UTC")
            timestamp = parsed.tz_convert("UTC").to_pydatetime()
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


@register_market_provider
class MassiveProvider(MarketDataProvider):
    """Massive v2 aggregates behind the common market-provider contract.

    Storage layout and manifest key are exactly the pre-abstraction ones
    (`data/raw/massive/{SYM}/{tf}/{raw|adjusted}/{year}.parquet`,
    `"{SYM}:{tf}:{raw|adjusted}"`)."""

    name = "massive"
    # data/interim/massive files normalized before this abstraction carry
    # source/adjustment/timeframe but no feed columns.
    legacy_unlabeled_interim_source = "MASSIVE"

    def __init__(self, config: AppConfig, adjusted: Optional[bool] = None):
        super().__init__(config)
        self.adjusted = bool(self.settings.get("adjusted", False)) if adjusted is None else bool(adjusted)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            source_label="MASSIVE",
            # Massive's stock aggregates are built from the consolidated
            # tape. This repo has not live-verified a 1-minute range
            # response (see module docstring), so this is the provider's
            # documented scope, recorded as such in `basis`.
            feed="consolidated",
            feed_scope=FEED_SCOPE_CONSOLIDATED,
            bar_density=BAR_DENSITY_DENSE,
            adjustment="adjusted" if self.adjusted else "raw",
            supported_timespans=("minute", "hour"),
            supported_adjustments=("raw", "adjusted"),
            # Depends on the account's plan tier; not declared rather than guessed.
            history_start=None,
            extended_hours=True,
            bar_timestamp="interval_start",
            provides_vwap=True,
            provides_transactions=True,
            credential_env_vars=("MASSIVE_API_KEY",),
            basis="provider documentation; field mapping live-verified 2026-09-10, "
                  "1-minute range retrieval not live-verified (plan-limited key)",
        )

    @property
    def adjustment_label(self) -> str:
        return "adjusted" if self.adjusted else "raw"

    def credentials(self) -> Dict[str, str]:
        api_key = self.config.env("MASSIVE_API_KEY")
        if not api_key:
            raise MissingCredentialsError(
                "MASSIVE_API_KEY not set (see .env.example). Cannot fetch Massive market data."
            )
        return {"api_key": api_key}

    def fetch_window(self, symbol, start, end, multiplier, timespan, session, credentials) -> List[dict]:
        # Module-level lookup at call time (not a bound reference) so
        # tests can substitute the transport via monkeypatch.
        return fetch_window_bars(
            self.config, symbol, start, end, multiplier, timespan, self.adjusted, session, credentials["api_key"],
        )

    def cache_key(self, symbol: str, timeframe: str, adjustment_label: Optional[str] = None) -> str:
        label = adjustment_label or self.adjustment_label
        return cache_key(symbol, timeframe, label == "adjusted")

    def request_meta(self, timeframe: str) -> dict:
        # Kept identical to the pre-abstraction audit metadata.
        return {"timeframe": timeframe, "adjusted": self.adjusted}

    # Report/output names predate the provider abstraction and are kept for
    # the default dataset (1min, raw); any other timeframe/adjustment gets
    # fully-qualified names so it cannot overwrite the default's artifacts.
    def _is_legacy_default(self, timeframe: str = "1min") -> bool:
        return timeframe == "1min" and not self.adjusted

    def report_name(self, symbol: str, timeframe: str, start: dt.date, end: dt.date) -> str:
        if self._is_legacy_default(timeframe):
            return f"massive_{symbol}_{start.isoformat()}_{end.isoformat()}.json"
        return super().report_name(symbol, timeframe, start, end)

    @property
    def response_output_suffix(self) -> str:
        return "" if self._is_legacy_default() else super().response_output_suffix

    @property
    def stored_report_prefix(self) -> str:
        return "market"


def _year_parquet_path(config: AppConfig, symbol: str, timeframe: str, adjusted: bool, year: int) -> Path:
    return MassiveProvider(config, adjusted=adjusted).year_path(symbol, timeframe, year)


def _merge_year_parquet(path: Path, new_rows: List[dict]) -> pd.DataFrame:
    return merge_year_parquet(path, new_rows, provider_label="Massive")


_validation_report_dir = validation_report_dir


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
    """Backward-compatible entry point; the orchestration itself is the
    provider-independent `market_provider.fetch_market_symbol`."""
    return fetch_market_symbol(
        config, manifest, MassiveProvider(config), symbol, start_date, end_date, timeframe,
        force=force, session=session, today=today,
    )


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
    return fetch_market_data(
        config, manifest, MassiveProvider(config), symbols, start_date, end_date, timeframe, force=force, today=today,
    )
