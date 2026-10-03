"""Provider-independent market-bar acquisition contract + shared orchestration.

Each market-data provider (Massive, Alpaca, later EODHD/...) implements a
small `MarketDataProvider` subclass that knows ONLY provider-specific
things:

  - how to fetch the bars for one [start, end] window (HTTP, auth,
    pagination, response-contract checks, field mapping),
  - which credentials it needs,
  - an explicit `ProviderCapabilities` declaration (feed scope, bar
    density, history start, optional fields, ...),
  - its storage identity (raw/interim dataset root, manifest key).

Everything that must behave IDENTICALLY across providers lives here, once:
monthly chunking, manifest skip/resume, NYSE-session-aware validation
before finalization, provisional (revision-overlap) windows, durable
year-parquet merge, artifact checksums, sibling-checkpoint reconciliation,
persisted validation reports. A new provider therefore cannot bypass the
manifest/integrity guarantees by accident -- it never touches them.

Canonical raw bar dict (what `fetch_window` returns; provider-native
fields mapped onto these names, nothing synthesized):
    timestamp_utc  tz-aware UTC datetime, START of the bar interval
    open/high/low/close/volume
    vwap           None if the provider did not supply it
    transactions   None if the provider did not supply it

Dataset identity. A stored dataset is identified by (provider, feed,
symbol, timeframe, adjustment). Every axis that changes the meaning of the
numbers is part of the manifest key and the storage path, so e.g. Alpaca
IEX bars can never be checkpointed as, merged into, or read back as Alpaca
SIP or Massive bars. Massive keeps its historical layout/key exactly
(`data/raw/massive/{SYM}/{tf}/{raw|adjusted}/{year}.parquet`,
`"{SYM}:{tf}:{raw|adjusted}"`) so existing checkpoints remain valid.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Type
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..manifest import Manifest, ManifestEntry, checksum_file, utcnow_iso
from ..redaction import redact_secrets
from ..validation.market import (
    BAR_DENSITY_DENSE,
    BAR_DENSITY_SPARSE,
    MarketValidationReport,
    UnsupportedTimeframeError,
    timeframe_minutes_from_parts,
    validate_market_bars,
)

logger = logging.getLogger(__name__)

BAR_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume", "vwap", "transactions"]

# Canonical feed-scope categories. `feed` is the provider's own label
# ("iex", "sip", ...); `feed_scope` is what downstream code may compare on.
FEED_SCOPE_CONSOLIDATED = "consolidated"    # all US venues (SIP-equivalent volume/prices)
FEED_SCOPE_SINGLE_VENUE = "single_venue"    # one exchange's prints only (e.g. IEX)


class MissingCredentialsError(RuntimeError):
    """Required provider credentials are not set -- raised before any
    network call, never turned into an empty-but-successful fetch."""


class ProviderResponseError(RuntimeError):
    """Malformed or error provider response -- never silently swallowed
    into an empty-but-"successful" chunk."""


class MonthResult(NamedTuple):
    symbol: str
    year: int
    month: int
    status: str  # "complete" | "provisional" | "empty" | "failed" | "skipped_cached"
    rows: int
    error: Optional[str] = None


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicit, centrally-declared provider/feed properties. Downstream
    code branches on THESE fields (feed_scope, bar_density, ...), never on
    `provider == "..."` string checks.

    `basis` records where the declaration comes from (official docs vs.
    live-verified) -- a capability is a claim about the provider, and its
    evidence level must stay visible."""

    provider: str                       # registry/manifest name, e.g. "alpaca"
    source_label: str                   # canonical `source` column value, e.g. "ALPACA"
    feed: str                           # provider's own feed label, e.g. "iex"
    feed_scope: str                     # FEED_SCOPE_*
    bar_density: str                    # BAR_DENSITY_DENSE | BAR_DENSITY_SPARSE (see validation/market.py)
    adjustment: str                     # adjustment policy label actually requested
    supported_timespans: Tuple[str, ...]
    supported_adjustments: Tuple[str, ...]
    history_start: Optional[dt.date]    # documented earliest available date (None = not documented)
    extended_hours: bool                # bars outside 09:30-16:00 ET are returned
    bar_timestamp: str                  # "interval_start" for every provider implemented so far
    provides_vwap: bool
    provides_transactions: bool
    credential_env_vars: Tuple[str, ...]
    basis: str
    # Higher-resolution data types. None of the implemented providers is
    # used for these yet; declared so a future event-window microstructure
    # source (e.g. Databento) has an explicit place to say what it offers.
    trades: bool = False
    quotes: bool = False
    bbo: bool = False
    l2: bool = False
    l3: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["history_start"] = self.history_start.isoformat() if self.history_start else None
        d["supported_timespans"] = list(self.supported_timespans)
        d["supported_adjustments"] = list(self.supported_adjustments)
        d["credential_env_vars"] = list(self.credential_env_vars)
        return d


class MarketDataProvider:
    """Base class for one market-bar provider, bound to one AppConfig.

    Subclasses MUST define `name`, `capabilities()`, `credentials()`,
    `fetch_window()`, and MAY override the storage/identity hooks."""

    name: str = ""
    # Interim files written before row-level feed provenance existed are
    # only admitted for a provider that sets this (to its `source` label);
    # see features/market_response.check_market_provenance.
    legacy_unlabeled_interim_source: Optional[str] = None

    def __init__(self, config: AppConfig):
        self.config = config
        self.settings: Dict[str, Any] = config.provider(self.name)

    @classmethod
    def stored_datasets(cls, config: AppConfig) -> Tuple[List["MarketDataProvider"], List[Path]]:
        """Provider instances for every dataset of this provider ACTUALLY
        STORED on disk -- independent of which variant (e.g. feed) the
        fetch config currently selects -- plus storage directories that
        cannot be attributed to a known dataset. Used by stored-data
        validation, so corruption in a non-active variant is never hidden
        by the active config."""
        provider = cls(config)
        return ([provider] if provider.raw_dataset_root().exists() else []), []

    # -- declaration -------------------------------------------------------
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @property
    def adjustment_label(self) -> str:
        return self.capabilities().adjustment

    @property
    def dataset_label(self) -> str:
        """Short, filename-safe identity for reports, e.g. "massive" or
        "alpaca-iex"."""
        return self.name

    # -- credentials / transport ------------------------------------------
    def credentials(self) -> Dict[str, str]:
        """Return the credentials `fetch_window` needs, or raise
        MissingCredentialsError. Called once, before any network call."""
        raise NotImplementedError

    def fetch_window(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        multiplier: int,
        timespan: str,
        session: requests.Session,
        credentials: Dict[str, str],
    ) -> List[dict]:
        """Every bar for [start, end] (inclusive NYSE calendar dates) as
        canonical raw bar dicts (see module docstring). Must raise on any
        error/malformed response -- returning [] means "the provider
        positively reported no bars"."""
        raise NotImplementedError

    def validation_as_of(self, now: dt.datetime) -> dt.datetime:
        """Instant fetch-time validation is evaluated against. Providers
        whose entitlement excludes the most recent data (e.g. a delayed
        feed) return an earlier instant, so not-yet-available minutes are
        treated as "not due" rather than "missing"."""
        return now

    # -- storage / identity -------------------------------------------------
    def raw_dataset_root(self) -> Path:
        return self.config.provider_raw_dir(self.name)

    def interim_dataset_root(self) -> Path:
        return self.config.interim_root / self.name

    def cache_key(self, symbol: str, timeframe: str, adjustment_label: Optional[str] = None) -> str:
        return f"{symbol}:{timeframe}:{adjustment_label or self.adjustment_label}"

    def year_path(self, symbol: str, timeframe: str, year: int, adjustment_label: Optional[str] = None) -> Path:
        return self.raw_dataset_root() / symbol / timeframe / (adjustment_label or self.adjustment_label) / f"{year}.parquet"

    def interim_year_path(self, symbol: str, timeframe: str, year: int) -> Path:
        return self.interim_dataset_root() / symbol / timeframe / self.adjustment_label / f"{year}.parquet"

    def request_meta(self, timeframe: str) -> Dict[str, Any]:
        caps = self.capabilities()
        return {
            "timeframe": timeframe,
            "adjustment": caps.adjustment,
            "feed": caps.feed,
            "feed_scope": caps.feed_scope,
            "bar_density": caps.bar_density,
        }

    def report_name(self, symbol: str, timeframe: str, start: dt.date, end: dt.date) -> str:
        """Fetch-time validation report filename. Carries every dataset
        axis (provider/feed via dataset_label, timeframe, adjustment) so
        semantically distinct datasets never overwrite each other's reports."""
        return f"{self.dataset_label}_{symbol}_{timeframe}_{self.adjustment_label}_{start.isoformat()}_{end.isoformat()}.json"

    @property
    def response_output_suffix(self) -> str:
        """Suffix for market-response output files built from this dataset."""
        return f"_{self.dataset_label}_{self.adjustment_label}"

    @property
    def stored_report_prefix(self) -> str:
        """Filename prefix for scripts/validate_data.py stored-data reports."""
        return f"market_{self.dataset_label}"


# --------------------------------------------------------------------------- registry
_REGISTRY: Dict[str, Type[MarketDataProvider]] = {}


def register_market_provider(cls: Type[MarketDataProvider]) -> Type[MarketDataProvider]:
    _REGISTRY[cls.name] = cls
    return cls


def _ensure_builtin_providers_loaded() -> None:
    # Imported lazily: the provider modules import this one.
    from . import alpaca, massive  # noqa: F401 - imported for their @register_market_provider side effect


def market_provider_names() -> List[str]:
    _ensure_builtin_providers_loaded()
    return sorted(_REGISTRY)


def market_provider_class(name: str) -> Type[MarketDataProvider]:
    _ensure_builtin_providers_loaded()
    if name not in _REGISTRY:
        raise KeyError(f"unknown market provider {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def get_market_provider(name: str, config: AppConfig) -> MarketDataProvider:
    """Instantiate a registered provider. Raises KeyError for an unknown
    name -- there is no fallback to another provider."""
    _ensure_builtin_providers_loaded()
    if name not in _REGISTRY:
        raise KeyError(f"unknown market provider {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name](config)


# --------------------------------------------------------------------------- timeframe
def parse_timeframe(timeframe: str) -> Tuple[int, str]:
    """"1min" -> (1, "minute"); "5min" -> (5, "minute"); "1h" -> (1, "hour");
    "1day"/"1d" -> (1, "day")."""
    import re

    match = re.match(r"^(\d+)\s*(min|minute|h|hour|day|d)$", timeframe.strip().lower())
    if not match:
        raise ValueError(f"unrecognized timeframe: {timeframe!r}")
    n = int(match.group(1))
    unit = match.group(2)
    timespan = {"min": "minute", "minute": "minute", "h": "hour", "hour": "hour", "day": "day", "d": "day"}[unit]
    return n, timespan


# --------------------------------------------------------------------------- storage helpers
def merge_year_parquet(path: Path, new_rows: List[dict], provider_label: str = "market") -> pd.DataFrame:
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
            logger.error("[%s] %s is unreadable (%s) -- rebuilding from scratch", provider_label, path, exc)
            existing = None
    combined = pd.concat([existing, new_df], ignore_index=True) if existing is not None else new_df

    combined = combined.drop_duplicates(subset=["timestamp_utc"], keep="last")
    combined = combined.sort_values("timestamp_utc").reset_index(drop=True)
    return combined


def atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_path, index=False)
    tmp_path.replace(path)


def validate_chronological(df: pd.DataFrame, label: str, symbol: str, year: int) -> None:
    if df.empty:
        return
    if not df["timestamp_utc"].is_monotonic_increasing:
        raise ValueError(f"[{label}][{symbol}] {year}: timestamps not sorted after merge")
    if df["timestamp_utc"].duplicated().any():
        raise ValueError(f"[{label}][{symbol}] {year}: duplicate timestamps after merge")


def _rows_in_ny_dates(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Rows whose bar start falls on NY calendar dates [start, end] -- the
    same day convention chunks are requested and validated in."""
    if df.empty:
        return df
    ny = ZoneInfo("America/New_York")
    lo = pd.Timestamp(dt.datetime.combine(start, dt.time(0), tzinfo=ny))
    hi = pd.Timestamp(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=ny))
    ts = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df[(ts >= lo) & (ts < hi)]


def sibling_still_valid(
    entry: ManifestEntry,
    provider: "MarketDataProvider",
    merged_df: pd.DataFrame,
    timeframe_minutes: int,
) -> Tuple[bool, str]:
    """Would `entry`'s claimed state still be granted to the rows now in
    the shared artifact? Re-runs the SAME window validation a fresh fetch
    is finalized with -- provider bar density, configured timeframe,
    exact claimed date range -- never mere row presence.

    The evaluation instant is the entry's own acquisition time
    (`retrieved_at`, passed through the provider's `validation_as_of`):
    a provisional entry is only held to what was already due when it was
    fetched (an in-progress session is checked on its elapsed part
    only), yet a month truncated to one bar fails exactly as it would
    have at fetch time. Returns (ok, reason)."""
    start, end = entry.start_date(), entry.end_date()
    rows = _rows_in_ny_dates(merged_df, start, end)
    as_of = provider.validation_as_of(dt.datetime.fromisoformat(entry.retrieved_at))
    report = validate_market_bars(
        rows.reset_index(drop=True), "sibling", start, end,
        timeframe_minutes=timeframe_minutes, bar_density=provider.capabilities().bar_density, as_of=as_of,
    )
    if entry.status == "empty":
        if report.expected_trading_sessions == 0 and rows.empty:
            return True, ""
        return False, (
            f"claimed verified-empty but window has {report.expected_trading_sessions} expected NYSE "
            f"session(s) and {len(rows)} stored row(s)"
        )
    if rows.empty:
        return False, "claimed rows absent from rebuilt shared artifact"
    if report.is_hard_failure:
        return False, f"sibling re-validation failed: {'; '.join(report.issues)}"
    return True, ""


def reverify_and_refresh_siblings(
    manifest: Manifest,
    provider: "MarketDataProvider",
    key: str,
    year_path: Path,
    checksum: str,
    merged_df: pd.DataFrame,
    timeframe_minutes: int,
) -> None:
    """After writing/rewriting a shared year file, re-establish trust in
    every OTHER complete/provisional/empty entry backed by it -- or
    revoke it. An entry is only re-blessed (checksum refreshed) when
    `sibling_still_valid` proves its claimed range still passes the
    finalization contract against the rows actually in the rebuilt file;
    otherwise it is invalidated ("failed") and will be refetched.

    History: this used to bless any sibling with at least one row in its
    range, so a provisional month truncated to a single bar regained
    trust when a neighbouring month was rewritten. "provisional" siblings
    must be reconciled at all because two provisional chunks can share
    one year file (on Oct 1-3 both Sept and Oct are inside the
    revision-overlap horizon); without reconciliation, writing Oct left
    Sept's checksum stale and `classify_gaps` reported it "failed".

    Entries whose checksum already equals the new file's (the entry
    recorded for the chunk just written) were validated moments ago and
    are skipped. Missing/corrupt-file detection happens up front, before
    any write, via `Manifest.invalidate_entries_for_missing_or_corrupt_path`.
    """
    path_str = str(year_path)
    for e in manifest.entries_for(provider.name, key):
        if e.path != path_str or e.status not in ("complete", "empty", "provisional"):
            continue
        if e.checksum == checksum:
            continue
        ok, reason = sibling_still_valid(e, provider, merged_df, timeframe_minutes)
        if not ok:
            logger.warning(
                "[%s] %s..%s claimed %s but no longer passes validation against the rebuilt shared "
                "artifact (%s) -- invalidating, NOT refreshing its checksum",
                provider.name, e.start, e.end, e.status, reason,
            )
            manifest.record(ManifestEntry(
                provider=provider.name, key=key, start=e.start, end=e.end,
                status="failed", error=reason, request_meta=e.request_meta,
            ))
            continue
        # Checksum-only reconciliation: this entry's OWN date range was
        # not refetched. `retrieved_at` keeps the original acquisition
        # time; `verified_at` records this re-validation.
        manifest.record(ManifestEntry(
            provider=provider.name, key=key, start=e.start, end=e.end,
            status=e.status, rows=e.rows, retrieved_at=e.retrieved_at,
            verified_at=utcnow_iso(), checksum=checksum, path=path_str,
            error=e.error, request_meta=e.request_meta,
        ))


def validate_window(
    bars: List[dict],
    symbol: str,
    start: dt.date,
    end: dt.date,
    timeframe_minutes: int = 1,
    bar_density: str = BAR_DENSITY_DENSE,
    as_of: Optional[dt.datetime] = None,
) -> MarketValidationReport:
    """Validate just-fetched bars, scoped to the exact requested window.
    Macro-release-window coverage is intentionally NOT checked here --
    that needs normalized macro events from other providers, which this
    fetch call has no access to; scripts/validate_data.py covers that
    layer separately, against already-stored data.

    `timeframe_minutes` MUST be derived via
    `validation.market.timeframe_minutes_from_parts` -- never defaulted
    (a complete 5-minute response validated against a 390-minute grid
    always fails). `bar_density` comes from the provider's declared
    capabilities (see validation/market.py)."""
    df = pd.DataFrame(bars, columns=BAR_COLUMNS)
    return validate_market_bars(
        df, symbol, start, end, timeframe_minutes=timeframe_minutes, bar_density=bar_density, as_of=as_of,
    )


def validation_report_dir(config: AppConfig) -> Path:
    return config.manifest_path.parent / "validation_reports"


def persist_validation_report(
    config: AppConfig,
    provider: MarketDataProvider,
    report: MarketValidationReport,
    symbol: str,
    start: dt.date,
    end: dt.date,
    timeframe: str = "1min",
) -> None:
    """Validation outcomes must be durable/inspectable, not just an
    in-memory decision that vanishes after this process exits."""
    out_dir = validation_report_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / provider.report_name(symbol, timeframe, start, end)
    payload = report.to_dict()
    payload["provider_capabilities"] = provider.capabilities().to_dict()
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


def is_provisional_window(chunk_end: dt.date, today: dt.date, revision_overlap_days: int) -> bool:
    """A window is provisional (never checkpointed "complete", always
    retried) if it touches "today" or falls within the trailing
    revision-overlap horizon -- late prints/corrections to recent
    sessions are expected, and an open/current month must stay
    refreshable rather than being finalized the moment it's first
    fetched."""
    return chunk_end >= (today - dt.timedelta(days=revision_overlap_days))


# --------------------------------------------------------------------------- orchestration
def fetch_market_symbol(
    config: AppConfig,
    manifest: Manifest,
    provider: MarketDataProvider,
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    timeframe: str,
    force: bool = False,
    session: Optional[requests.Session] = None,
    today: Optional[dt.date] = None,
    now: Optional[dt.datetime] = None,
) -> List[MonthResult]:
    """Fetch + validate + durably store one symbol's bars for [start_date,
    end_date] from `provider`, checkpointing each calendar-month chunk in
    the manifest. A chunk is finalized ("complete"/"empty") only after
    NYSE-session-aware validation passes -- an HTTP 200 alone is never
    enough."""
    caps = provider.capabilities()
    label = caps.source_label
    credentials = provider.credentials()  # raises MissingCredentialsError before any network call

    multiplier, timespan = parse_timeframe(timeframe)
    # Reject an unsupported/invalid timeframe BEFORE issuing any network
    # call -- never silently validate daily (or zero/negative-interval)
    # bars as if they were 1-minute bars. Raises UnsupportedTimeframeError.
    timeframe_minutes = timeframe_minutes_from_parts(multiplier, timespan)
    if timespan not in caps.supported_timespans:
        raise UnsupportedTimeframeError(
            f"{caps.provider} does not declare support for timespan {timespan!r} "
            f"(supported: {list(caps.supported_timespans)})"
        )
    revision_overlap_days = int(provider.settings.get("revision_overlap_days", 3))
    today = today or dt.date.today()

    key = provider.cache_key(symbol, timeframe)
    request_meta = provider.request_meta(timeframe)
    sess = session or requests.Session()
    results: List[MonthResult] = []
    invalidated_years: set = set()

    for chunk in iter_date_chunks(start_date, end_date, frequency="month"):
        # The exact requested (clipped) window -- NOT always the full
        # calendar month: a request for Sept 1-10 checkpoints Sept 1-10.
        req_start, req_end = chunk.start, chunk.end
        start_iso, end_iso = req_start.isoformat(), req_end.isoformat()
        provisional = is_provisional_window(req_end, today, revision_overlap_days)

        year_path = provider.year_path(symbol, timeframe, req_start.year)
        if req_start.year not in invalidated_years:
            # MUST happen before any is_complete() check or write this
            # run touches this year's file: if the shared artifact is
            # missing/corrupt, every checkpoint backed by it is
            # untrustworthy right now, not just the one we're about to
            # refetch. See Manifest.invalidate_entries_for_missing_or_corrupt_path.
            invalidated = manifest.invalidate_entries_for_missing_or_corrupt_path(provider.name, key, year_path)
            if invalidated:
                logger.warning(
                    "[%s][%s] %s: backing artifact missing/corrupt -- invalidated %d checkpoint(s): %s",
                    label, symbol, year_path, len(invalidated),
                    ", ".join(f"{e.start}..{e.end}" for e in invalidated),
                )
            invalidated_years.add(req_start.year)

        if not force and not provisional and manifest.is_complete(provider.name, key, start_iso, end_iso):
            logger.info("[%s][%s] %s already fetched, skipping", label, symbol, chunk.label)
            results.append(MonthResult(symbol, req_start.year, req_start.month, "skipped_cached", 0))
            continue

        if caps.history_start is not None and req_end < caps.history_start:
            # Entirely before the provider's documented coverage: a
            # truthful, explicit gap -- not a request that "succeeds" empty.
            error = (
                f"requested window precedes {caps.provider}/{caps.feed} documented history start "
                f"{caps.history_start.isoformat()} -- this provider cannot cover it"
            )
            logger.error("[%s][%s] %s: %s", label, symbol, chunk.label, error)
            manifest.record(ManifestEntry(
                provider=provider.name, key=key, start=start_iso, end=end_iso,
                status="failed", error=error, request_meta=request_meta,
            ))
            results.append(MonthResult(symbol, req_start.year, req_start.month, "failed", 0, error=error))
            continue

        try:
            bars = provider.fetch_window(symbol, req_start, req_end, multiplier, timespan, sess, credentials)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            # Provider exception text is untrusted (it can echo payloads,
            # headers, URLs): sanitized once, before it is logged, stored
            # in the manifest, or returned to the CLI.
            error = redact_secrets(f"{type(exc).__name__}: {exc}")
            logger.error("[%s][%s] %s FAILED: %s", label, symbol, chunk.label, error)
            manifest.record(ManifestEntry(
                provider=provider.name, key=key, start=start_iso, end=end_iso,
                status="failed", error=error, request_meta=request_meta,
            ))
            results.append(MonthResult(symbol, req_start.year, req_start.month, "failed", 0, error=error))
            continue

        # Validate BEFORE finalizing -- scoped to exactly this requested
        # window. The raw bars are still written either way (never
        # discard fetched data over a validation concern), but a hard
        # failure or a suspicious empty response prevents the checkpoint
        # from being finalized as "complete"/"empty".
        as_of = provider.validation_as_of(now or dt.datetime.now(dt.timezone.utc))
        window_report = validate_window(
            bars, symbol, req_start, req_end,
            timeframe_minutes=timeframe_minutes, bar_density=caps.bar_density, as_of=as_of,
        )
        persist_validation_report(config, provider, window_report, symbol, req_start, req_end, timeframe)

        if not bars:
            if window_report.expected_trading_sessions == 0:
                status = "empty"  # verified: genuinely no NYSE sessions in this window
                error = None
            else:
                # A normal trading window with ZERO bars is NOT a
                # verified-empty window -- it is an incomplete response.
                status = "failed"
                error = (
                    f"expected {window_report.expected_trading_sessions} NYSE trading session(s) "
                    f"in this window but received 0 bars -- treating as an incomplete response, "
                    f"not verified-empty"
                )
        elif window_report.is_hard_failure:
            status = "failed"
            error = redact_secrets(f"validation hard failure: {'; '.join(window_report.issues)}")
        else:
            status = "provisional" if provisional else "complete"
            error = None

        # Durable, immediate write: merge+persist THIS chunk before moving
        # on. Raw bars are preserved even when `status` ends up "failed".
        merged = merge_year_parquet(year_path, bars, provider_label=label)
        validate_chronological(merged, label, symbol, req_start.year)
        atomic_write_parquet(merged, year_path)
        checksum = checksum_file(year_path)

        manifest.record(ManifestEntry(
            provider=provider.name, key=key, start=start_iso, end=end_iso,
            status=status, rows=len(bars), checksum=checksum, path=str(year_path), error=error,
            request_meta=request_meta,
        ))
        # Now that THIS chunk's entry reflects the rebuilt file, check
        # every OTHER entry sharing the path individually.
        reverify_and_refresh_siblings(manifest, provider, key, year_path, checksum, merged, timeframe_minutes)

        logger.info("[%s][%s] %s %s: %d bars", label, symbol, chunk.label, status, len(bars))
        results.append(MonthResult(symbol, req_start.year, req_start.month, status, len(bars), error=error))

    return results


def fetch_market_data(
    config: AppConfig,
    manifest: Manifest,
    provider: MarketDataProvider,
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
        out[symbol] = fetch_market_symbol(
            config, manifest, provider, symbol, start_date, end_date, timeframe,
            force=force, session=session, today=today,
        )
    return out


__all__ = [
    "BAR_COLUMNS", "BAR_DENSITY_DENSE", "BAR_DENSITY_SPARSE", "FEED_SCOPE_CONSOLIDATED", "FEED_SCOPE_SINGLE_VENUE",
    "MarketDataProvider", "MissingCredentialsError", "MonthResult", "ProviderCapabilities", "ProviderResponseError",
    "fetch_market_data", "fetch_market_symbol", "get_market_provider", "market_provider_names", "parse_timeframe",
    "register_market_provider",
]
