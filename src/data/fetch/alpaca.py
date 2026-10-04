"""Alpaca Market Data API v2 -- historical stock bars.

Implements the documented endpoint (official reference, checked 2026-10-03:
https://docs.alpaca.markets/reference/stockbars):

    GET https://data.alpaca.markets/v2/stocks/bars
        ?symbols=<SYM>&timeframe=<n>Min|<n>Hour&start=<RFC-3339>&end=<RFC-3339>
         &limit=<1..10000>&adjustment=raw|split|dividend|spin-off|all
         &feed=iex|sip|...&sort=asc&page_token=<token>
    headers: APCA-API-KEY-ID, APCA-API-SECRET-KEY

    200 -> {"bars": {"<SYM>": [{"t": RFC-3339, "o","h","l","c","v","n","vw"}, ...]},
            "next_page_token": "<token>"|null, "currency": "USD"}

Documented facts this adapter relies on (Alpaca docs, not blog posts):
  - Bar timestamp is the LEFT edge of the interval ("a trade at 14:52:28
    belongs to the 14:52:00 minute bar") -- same convention as Massive,
    so no shifting is applied.
  - "the bar is only emitted if none of its fields (open, high, low,
    close, volume) are 0" -- minutes without eligible trades have NO bar.
    Nothing here fills them.
  - Feeds: `iex` is "a single US exchange that accounts for approximately
    ~2.5% of the market volume" and "the only feed that can be used
    without a subscription"; `sip` "covers all US exchanges". IEX volume
    and prices are therefore NOT consolidated full-market values and are
    declared `single_venue` / `sparse` (see ProviderCapabilities).
  - The `feed` default is "the best available feed based on the user's
    subscription" -- so this adapter ALWAYS sends `feed` explicitly; the
    feed of stored data must never depend on whichever account ran it.
  - Basic plan: "Historical data timeframe: Since 2016"; for SIP, "the
    `end` parameter must be at least 15 minutes old to query SIP data
    without a subscription" (error otherwise: "subscription does not
    permit querying recent SIP data"). Basic API limit: 200 calls/min.
  - `limit` applies to the total across symbols; this adapter requests
    one symbol per call and follows `next_page_token`.
  - `asof` symbol-rename mapping is enabled by default on historical
    endpoints (e.g. querying META before 2022-06-09 returns FB bars).
    Massive does not do this; it is irrelevant for QQQ/SPY but must be
    kept in mind when comparing renamed tickers.

NOT LIVE-VERIFIED in this repository: no Alpaca credentials were
available when this adapter was written, so request/response handling is
verified only against fixtures built from the documented contract.
Whether the Basic plan's SIP history is actually usable for a given
account (the docs say yes, with the 15-minute restriction) must be
confirmed with a real key before relying on it.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from ..config import AppConfig
from ..http_utils import request_with_retry
from ..validation.market import BAR_DENSITY_DENSE, BAR_DENSITY_SPARSE
from .market_provider import (
    FEED_SCOPE_CONSOLIDATED,
    FEED_SCOPE_SINGLE_VENUE,
    MarketDataProvider,
    MissingCredentialsError,
    ProviderCapabilities,
    ProviderResponseError,
    register_market_provider,
)

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

KEY_ID_ENV = "APCA_API_KEY_ID"
SECRET_ENV = "APCA_API_SECRET_KEY"

# feed -> (feed_scope, bar_density). Only feeds whose semantics are
# documented and understood are accepted; anything else is a config error.
FEEDS: Dict[str, Tuple[str, str]] = {
    "iex": (FEED_SCOPE_SINGLE_VENUE, BAR_DENSITY_SPARSE),
    "sip": (FEED_SCOPE_CONSOLIDATED, BAR_DENSITY_DENSE),
}
ADJUSTMENTS = ("raw", "split", "dividend", "spin-off", "all")
DOCUMENTED_HISTORY_START = dt.date(2016, 1, 1)  # "Since 2016" (both plans)
SIP_BASIC_RECENT_RESTRICTION = dt.timedelta(minutes=15)
_REQUIRED_BAR_FIELDS = ("t", "o", "h", "l", "c", "v")


class AlpacaResponseError(ProviderResponseError):
    pass


def timeframe_param(multiplier: int, timespan: str) -> str:
    """Documented forms: [1-59]Min, [1-23]Hour."""
    if timespan == "minute" and 1 <= multiplier <= 59:
        return f"{multiplier}Min"
    if timespan == "hour" and 1 <= multiplier <= 23:
        return f"{multiplier}Hour"
    raise ValueError(f"Alpaca does not document a {multiplier} {timespan} bar timeframe")


def ny_dates_to_utc_window(start: dt.date, end: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    """[start 00:00 New York, end+1 00:00 New York) as UTC instants. Our
    chunk dates are NYSE calendar dates; a bare YYYY-MM-DD sent to the API
    would leave the day boundary's timezone up to the provider."""
    lo = dt.datetime.combine(start, dt.time(0), tzinfo=NY).astimezone(UTC)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=NY).astimezone(UTC)
    return lo, hi


def _rfc3339(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(raw) -> dt.datetime:
    if not isinstance(raw, str):
        raise AlpacaResponseError(f"malformed bar timestamp {raw!r}: expected an RFC-3339 string")
    ts = pd.Timestamp(raw)
    if ts.tzinfo is None:
        # Never guess a timezone for a naive provider timestamp.
        raise AlpacaResponseError(f"bar timestamp {raw!r} has no UTC offset")
    return ts.tz_convert("UTC").to_pydatetime()


def parse_bars_payload(
    payload, symbol: str, window_start: dt.datetime, window_end_exclusive: dt.datetime,
) -> Tuple[List[dict], Optional[str]]:
    """Map one documented response page onto canonical raw bar dicts.
    Anything that does not match the documented contract raises -- an
    unexpected shape is never read as "no data"."""
    if not isinstance(payload, dict):
        raise AlpacaResponseError(f"malformed response: expected a JSON object, got {type(payload).__name__}")
    if "bars" not in payload:
        detail = payload.get("message") or "missing 'bars' field"
        raise AlpacaResponseError(f"malformed response: {detail}")
    bars_by_symbol = payload["bars"]
    if bars_by_symbol is None:
        bars_by_symbol = {}
    if not isinstance(bars_by_symbol, dict):
        raise AlpacaResponseError("malformed response: 'bars' is not an object keyed by symbol")
    unexpected = set(bars_by_symbol) - {symbol}
    if unexpected:
        raise AlpacaResponseError(f"response contains bars for unrequested symbol(s) {sorted(unexpected)}")
    raw_bars = bars_by_symbol.get(symbol) or []
    if not isinstance(raw_bars, list):
        raise AlpacaResponseError(f"malformed response: bars for {symbol} is not a list")

    out: List[dict] = []
    for b in raw_bars:
        if not isinstance(b, dict):
            raise AlpacaResponseError(f"malformed bar {b!r}")
        missing = [f for f in _REQUIRED_BAR_FIELDS if b.get(f) is None]
        if missing:
            raise AlpacaResponseError(f"bar missing required field(s) {missing}: {b!r}")
        ts = _parse_timestamp(b["t"])
        if ts.second or ts.microsecond:
            raise AlpacaResponseError(f"bar timestamp {b['t']!r} is not minute-aligned")
        if not (window_start <= ts < window_end_exclusive):
            raise AlpacaResponseError(
                f"bar timestamp {ts.isoformat()} outside the requested window "
                f"[{window_start.isoformat()}, {window_end_exclusive.isoformat()})"
            )
        out.append({
            "timestamp_utc": ts,
            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": b["v"],
            # Optional fields: absent stays None, never synthesized.
            "vwap": b.get("vw"),
            "transactions": b.get("n"),
        })

    token = payload.get("next_page_token")
    if token is not None and not isinstance(token, str):
        raise AlpacaResponseError(f"malformed next_page_token {token!r}")
    return out, (token or None)


def fetch_window_bars(
    config: AppConfig,
    symbol: str,
    start: dt.date,
    end: dt.date,
    multiplier: int,
    timespan: str,
    feed: str,
    adjustment: str,
    session: requests.Session,
    key_id: str,
    secret_key: str,
    end_cap_utc: Optional[dt.datetime] = None,
) -> List[dict]:
    """Every bar for NYSE dates [start, end], following `next_page_token`
    with a loop guard. `end_cap_utc` (if earlier than the window end)
    caps the request, e.g. to honour the Basic plan's 15-minute SIP
    restriction; a window starting after the cap returns []."""
    settings = config.provider("alpaca")
    window_start, window_end = ny_dates_to_utc_window(start, end)
    request_end = window_end
    if end_cap_utc is not None and end_cap_utc < request_end:
        request_end = end_cap_utc
    if request_end <= window_start:
        return []

    url = f"{settings['base_url'].rstrip('/')}/v2/stocks/bars"
    base_params = {
        "symbols": symbol,
        "timeframe": timeframe_param(multiplier, timespan),
        "start": _rfc3339(window_start),
        # `end` is inclusive in the API; one second before the exclusive
        # bound keeps the next NY day's 00:00 bar out of this window.
        "end": _rfc3339(request_end - dt.timedelta(seconds=1)),
        "limit": str(int(settings.get("page_limit", 10000))),
        "adjustment": adjustment,
        "feed": feed,
        "sort": "asc",
    }
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}

    all_bars: List[dict] = []
    seen_tokens: set = set()
    token: Optional[str] = None
    while True:
        params = dict(base_params)
        if token is not None:
            params["page_token"] = token
        response = request_with_retry(
            "GET", url, session=session,
            max_retries=int(settings.get("max_retries", 5)),
            request_delay_seconds=float(settings.get("request_delay_seconds", 0.35)),
            params=params, headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AlpacaResponseError(f"response body is not JSON: {exc}") from exc
        bars, token = parse_bars_payload(payload, symbol, window_start, window_end)
        all_bars.extend(bars)
        if token is None:
            break
        if token in seen_tokens:
            raise AlpacaResponseError(f"repeated next_page_token detected (possible pagination loop): {token}")
        seen_tokens.add(token)
    return all_bars


@register_market_provider
class AlpacaProvider(MarketDataProvider):
    """Alpaca historical bars. Feed and adjustment come from config
    (`providers.alpaca.feed` / `.adjustment`) and are part of the dataset
    identity: storage `data/raw/alpaca/{feed}/{SYM}/{tf}/{adjustment}/`,
    manifest key `"{SYM}:{tf}:{adjustment}:{feed}"`."""

    name = "alpaca"

    def __init__(self, config: AppConfig, feed: Optional[str] = None):
        """`feed` overrides config only for stored-data discovery
        (`stored_datasets`); fetching always uses the configured feed."""
        super().__init__(config)
        self.feed = str(feed if feed is not None else self.settings.get("feed", "")).lower()
        if self.feed not in FEEDS:
            raise ValueError(
                f"providers.alpaca.feed must be explicitly one of {sorted(FEEDS)} "
                f"(got {self.settings.get('feed')!r}); the API's implicit default depends on the account's "
                f"subscription and is never relied on"
            )
        self.adjustment = str(self.settings.get("adjustment", "raw"))
        if self.adjustment not in ADJUSTMENTS:
            raise ValueError(f"providers.alpaca.adjustment must be one of {ADJUSTMENTS}, got {self.adjustment!r}")
        self.entitlement = str(self.settings.get("entitlement", "basic")).lower()

    @classmethod
    def stored_datasets(cls, config: AppConfig):
        """One provider per stored feed directory under providers.alpaca.raw_dir
        (iex AND sip if both exist), regardless of the configured feed.
        Unrecognized directories are returned separately, never guessed."""
        root = config.provider_raw_dir(cls.name)
        if not root.exists():
            return [], []
        known, unknown = [], []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            (known if d.name in FEEDS else unknown).append(d)
        return [cls(config, feed=d.name) for d in known], unknown

    def capabilities(self) -> ProviderCapabilities:
        scope, density = FEEDS[self.feed]
        return ProviderCapabilities(
            provider=self.name,
            source_label="ALPACA",
            feed=self.feed,
            feed_scope=scope,
            bar_density=density,
            adjustment=self.adjustment,
            supported_timespans=("minute", "hour"),
            supported_adjustments=ADJUSTMENTS,
            history_start=DOCUMENTED_HISTORY_START,
            extended_hours=True,  # extended-hours trades (condition T) update minute bars per Alpaca's rules table
            bar_timestamp="interval_start",
            provides_vwap=True,
            provides_transactions=True,
            credential_env_vars=(KEY_ID_ENV, SECRET_ENV),
            basis="official Alpaca docs (stockbars reference, market-data FAQ, subscription plans), "
                  "checked 2026-10-03; not live-verified in this repository",
        )

    @property
    def adjustment_label(self) -> str:
        return self.adjustment

    @property
    def dataset_label(self) -> str:
        return f"alpaca-{self.feed}"

    def raw_dataset_root(self):
        return self.config.provider_raw_dir(self.name) / self.feed

    def interim_dataset_root(self):
        return self.config.interim_root / self.name / self.feed

    def cache_key(self, symbol: str, timeframe: str, adjustment_label: Optional[str] = None) -> str:
        return f"{symbol}:{timeframe}:{adjustment_label or self.adjustment}:{self.feed}"

    def request_meta(self, timeframe: str) -> dict:
        meta = super().request_meta(timeframe)
        meta["entitlement"] = self.entitlement
        # Alpaca's historical endpoints apply rename mapping by default
        # (`asof`); it is deliberately NOT set here (see README known
        # limitations), and the audit trail says so.
        meta["symbol_mapping"] = "alpaca_default_asof_not_sent"
        return meta

    def credentials(self) -> Dict[str, str]:
        key_id, secret = self.config.env(KEY_ID_ENV), self.config.env(SECRET_ENV)
        missing = [name for name, v in ((KEY_ID_ENV, key_id), (SECRET_ENV, secret)) if not v]
        if missing:
            raise MissingCredentialsError(
                f"{', '.join(missing)} not set (see .env.example). Cannot fetch Alpaca market data."
            )
        return {"key_id": key_id, "secret_key": secret}

    def _sip_recent_cap(self, now: dt.datetime) -> Optional[dt.datetime]:
        if self.feed == "sip" and self.entitlement == "basic":
            # one extra minute of margin over the documented 15
            return now - SIP_BASIC_RECENT_RESTRICTION - dt.timedelta(minutes=1)
        return None

    def validation_as_of(self, now: dt.datetime) -> dt.datetime:
        cap = self._sip_recent_cap(now)
        return cap if cap is not None else now

    def fetch_window(self, symbol, start, end, multiplier, timespan, session, credentials) -> List[dict]:
        return fetch_window_bars(
            self.config, symbol, start, end, multiplier, timespan, self.feed, self.adjustment, session,
            credentials["key_id"], credentials["secret_key"],
            end_cap_utc=self._sip_recent_cap(dt.datetime.now(UTC)),
        )
