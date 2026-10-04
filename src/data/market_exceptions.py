"""Known market-data exception registry (session / minute exceptions).

A machine-readable, versioned record of intervals in which canonical 1-minute
bars are *legitimately or knowingly* absent, kept in
`config/market_exceptions.yaml` (path set by config `market.exception_registry`).
It replaces what would otherwise be special-case `if date == ...` logic.

Classifications are deliberately NOT equivalent:

  exchange_closed               the exchange had no session (beyond what the
                                NYSE calendar already knows, e.g. an unscheduled
                                closure). No bars are expected.
  market_wide_halt              trading was halted market-wide (e.g. a
                                circuit-breaker). No bars are expected; the data
                                is not corrupt. Returns spanning it are real but
                                cover more clock time than traded time.
  legitimate_no_trade_interval  no qualifying trades occurred for this symbol /
                                feed in the interval (e.g. a single-stock halt).
                                No bars are expected.
  provider_gap                  trading DID happen but the provider's archive
                                has no bars. A scientific data gap: it stays
                                visible, is never filled, and invalidates event
                                windows that need it.
  temporary_fetch_failure       an operational failure record. Always
                                retryable; never explains missing data away.

Interval convention: [start_utc, end_utc) over 1-minute BAR START times,
timezone-aware UTC. Each entry also states the session `date` and the
`start_local`/`end_local` wall-clock times in `timezone`; the loader verifies
they convert exactly to the UTC values (guards against DST mistakes).

Operational vs scientific state (see fetch/market_provider.py and
validation/market.py): missing minutes covered by a `provider_gap` entry with
`retry_appropriate: false` are *explained* -- they no longer make a chunk a
validation hard failure, so it is finalized as `complete_with_known_gaps`
(operationally resolved, not re-fetched on every update) -- but they are
still counted, reported and attached to the manifest entry by id
(scientifically incomplete). Minutes under `market_wide_halt`,
`exchange_closed` or `legitimate_no_trade_interval` are removed from the
expected-minute grid instead (nothing was due).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import yaml

WILDCARD = "*"
REGISTRY_SCHEMA_VERSION = 1


class ExceptionClass(str, Enum):
    EXCHANGE_CLOSED = "exchange_closed"
    MARKET_WIDE_HALT = "market_wide_halt"
    PROVIDER_GAP = "provider_gap"
    TEMPORARY_FETCH_FAILURE = "temporary_fetch_failure"
    LEGITIMATE_NO_TRADE_INTERVAL = "legitimate_no_trade_interval"


# Classes whose minutes are not expected to have bars at all.
EXCLUDED_FROM_EXPECTED = frozenset({
    ExceptionClass.EXCHANGE_CLOSED, ExceptionClass.MARKET_WIDE_HALT, ExceptionClass.LEGITIMATE_NO_TRADE_INTERVAL,
})

# Evidence level of an entry. Never upgraded implicitly.
STATUSES = (
    "observed",        # seen once in an acquisition; not independently confirmed
    "reobserved",      # identical result on a later, independent re-fetch
    "authoritative",   # backed by an authoritative external record (e.g. exchange halt notice)
    "retired",         # kept for history; no longer applied
)

WINDOW_TAGS = {
    ExceptionClass.PROVIDER_GAP: "provider_gap_in_window",
    ExceptionClass.MARKET_WIDE_HALT: "market_halt_in_window",
    ExceptionClass.EXCHANGE_CLOSED: "exchange_closed_in_window",
    ExceptionClass.LEGITIMATE_NO_TRADE_INTERVAL: "no_trade_interval_in_window",
    ExceptionClass.TEMPORARY_FETCH_FAILURE: "temporary_fetch_failure_in_window",
}

UTC = dt.timezone.utc


class RegistryError(ValueError):
    """The registry file is malformed or internally inconsistent."""


def _utc(value, what: str) -> dt.datetime:
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, dt.datetime):
        raise RegistryError(f"{what}: expected an ISO-8601 datetime, got {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise RegistryError(f"{what}: timestamp {value!r} must be timezone-aware")
    return value.astimezone(UTC)


def require_aware_utc(value: dt.datetime, what: str = "timestamp") -> dt.datetime:
    """Query inputs must be timezone-aware; naive datetimes are never assumed UTC."""
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime, got {value!r}")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MarketException:
    id: str
    classification: ExceptionClass
    status: str
    symbols: Tuple[str, ...]
    provider: str
    feed: str
    date: dt.date
    timezone: str
    start_local: str
    end_local: str
    start_utc: dt.datetime
    end_utc: dt.datetime
    retry_appropriate: bool
    invalidates_event_window: bool
    evidence: str
    source: str
    notes: str = ""

    @property
    def active(self) -> bool:
        return self.status != "retired"

    @property
    def explains_missing_data(self) -> bool:
        """A provider gap only explains missing minutes once no further
        retry is considered appropriate; until then it stays a failure."""
        return self.classification is ExceptionClass.PROVIDER_GAP and not self.retry_appropriate

    def applies_to(self, symbol: str, provider: str, feed: str) -> bool:
        return (
            self.active
            and (WILDCARD in self.symbols or symbol in self.symbols)
            and self.provider in (WILDCARD, provider)
            and self.feed in (WILDCARD, feed)
        )

    def overlaps(self, start_utc: dt.datetime, end_utc: dt.datetime) -> bool:
        return self.start_utc < end_utc and start_utc < self.end_utc

    def to_canonical(self) -> dict:
        d = asdict(self)
        d["classification"] = self.classification.value
        d["symbols"] = list(self.symbols)
        d["date"] = self.date.isoformat()
        d["start_utc"] = self.start_utc.isoformat()
        d["end_utc"] = self.end_utc.isoformat()
        return d


@dataclass(frozen=True)
class ExceptionHit:
    """One registry entry overlapping a queried window."""
    entry: MarketException
    overlap_start_utc: dt.datetime
    overlap_end_utc: dt.datetime

    @property
    def overlap_minutes(self) -> int:
        return int((self.overlap_end_utc - self.overlap_start_utc) / dt.timedelta(minutes=1))


@dataclass(frozen=True)
class WindowExceptionReport:
    """Answer to "does this market window intersect a known exception?"."""
    symbol: str
    start_utc: dt.datetime
    end_utc: dt.datetime
    hits: Tuple[ExceptionHit, ...] = field(default_factory=tuple)

    @property
    def any(self) -> bool:
        return bool(self.hits)

    @property
    def classifications(self) -> frozenset:
        return frozenset(h.entry.classification for h in self.hits)

    def has(self, classification: ExceptionClass) -> bool:
        return classification in self.classifications

    @property
    def has_provider_gap(self) -> bool:
        return self.has(ExceptionClass.PROVIDER_GAP)

    @property
    def has_market_halt(self) -> bool:
        return self.has(ExceptionClass.MARKET_WIDE_HALT)

    @property
    def has_exchange_closed(self) -> bool:
        return self.has(ExceptionClass.EXCHANGE_CLOSED)

    @property
    def has_no_trade_interval(self) -> bool:
        return self.has(ExceptionClass.LEGITIMATE_NO_TRADE_INTERVAL)

    @property
    def invalidates_event_window(self) -> bool:
        return any(h.entry.invalidates_event_window for h in self.hits)

    @property
    def tags(self) -> Tuple[str, ...]:
        """Data-quality tags for downstream rows, e.g. ("market_halt_in_window",)."""
        return tuple(sorted({WINDOW_TAGS[c] for c in self.classifications}))

    @property
    def entry_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(h.entry.id for h in self.hits))


class MarketExceptionRegistry:
    def __init__(self, entries: Sequence[MarketException], registry_version: int, source_path: Optional[str] = None):
        ids = [e.id for e in entries]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise RegistryError(f"duplicate registry ids: {dupes}")
        self.entries: Tuple[MarketException, ...] = tuple(sorted(entries, key=lambda e: (e.start_utc, e.id)))
        self.registry_version = registry_version
        self.source_path = source_path

    @classmethod
    def empty(cls) -> "MarketExceptionRegistry":
        return cls([], registry_version=0)

    # -- loading --------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict, source_path: Optional[str] = None) -> "MarketExceptionRegistry":
        if not isinstance(raw, dict):
            raise RegistryError("registry must be a mapping")
        if raw.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryError(f"unsupported registry schema_version {raw.get('schema_version')!r}")
        version = raw.get("registry_version")
        if not isinstance(version, int) or version < 1:
            raise RegistryError("registry_version must be a positive integer")
        return cls([_parse_entry(e) for e in raw.get("entries") or []], version, source_path)

    @classmethod
    def load(cls, path: Path, source_label: Optional[str] = None) -> "MarketExceptionRegistry":
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return cls.from_dict(raw, source_label or str(path))

    # -- identity -------------------------------------------------------
    def content_sha256(self) -> str:
        """Hash of the parsed registry (version + entries), independent of
        YAML formatting, comments, ordering or file location."""
        payload = {"registry_version": self.registry_version,
                   "entries": [e.to_canonical() for e in sorted(self.entries, key=lambda e: e.id)]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # -- queries --------------------------------------------------------
    def for_dataset(self, symbol: str, provider: str, feed: str) -> List[MarketException]:
        return [e for e in self.entries if e.applies_to(symbol, provider, feed)]

    def query_window(self, symbol: str, start: dt.datetime, end: dt.datetime,
                     provider: str = "alpaca", feed: str = "sip") -> WindowExceptionReport:
        """Known exceptions intersecting the half-open UTC window [start, end)
        for one dataset. Inputs must be timezone-aware (any offset)."""
        s, e = require_aware_utc(start, "start"), require_aware_utc(end, "end")
        if e <= s:
            raise ValueError(f"empty or inverted window: {start} .. {end}")
        hits = tuple(
            ExceptionHit(x, max(s, x.start_utc), min(e, x.end_utc))
            for x in self.for_dataset(symbol, provider, feed) if x.overlaps(s, e)
        )
        return WindowExceptionReport(symbol, s, e, hits)

    def entries_for_session(self, symbol: str, session_date: dt.date,
                            provider: str = "alpaca", feed: str = "sip") -> List[MarketException]:
        return [x for x in self.for_dataset(symbol, provider, feed) if x.date == session_date]


def _parse_entry(raw: dict) -> MarketException:
    if not isinstance(raw, dict):
        raise RegistryError(f"registry entry must be a mapping: {raw!r}")
    rid = raw.get("id")
    if not rid:
        raise RegistryError(f"registry entry without id: {raw!r}")
    try:
        cls = ExceptionClass(raw.get("classification"))
    except ValueError:
        raise RegistryError(f"{rid}: unknown classification {raw.get('classification')!r}") from None
    status = raw.get("status")
    if status not in STATUSES:
        raise RegistryError(f"{rid}: status must be one of {STATUSES}, got {status!r}")
    symbols = tuple(raw.get("symbols") or ())
    if not symbols:
        raise RegistryError(f"{rid}: symbols must list at least one symbol (or '*')")
    provider, feed = str(raw.get("provider", "")), str(raw.get("feed", ""))
    if not provider or not feed:
        raise RegistryError(f"{rid}: provider and feed are required ('*' for any)")
    if cls is ExceptionClass.PROVIDER_GAP and WILDCARD in (provider, feed):
        raise RegistryError(f"{rid}: a provider_gap belongs to one specific provider and feed")
    retry = raw.get("retry_appropriate")
    invalidates = raw.get("invalidates_event_window")
    if not isinstance(retry, bool) or not isinstance(invalidates, bool):
        raise RegistryError(f"{rid}: retry_appropriate and invalidates_event_window must be booleans")
    if cls is ExceptionClass.TEMPORARY_FETCH_FAILURE and not retry:
        raise RegistryError(f"{rid}: a temporary_fetch_failure is always retry_appropriate")
    for key in ("evidence", "source"):
        if not str(raw.get(key) or "").strip():
            raise RegistryError(f"{rid}: '{key}' is required")
    date = raw.get("date")
    date = dt.date.fromisoformat(date) if isinstance(date, str) else date
    if not isinstance(date, dt.date):
        raise RegistryError(f"{rid}: date must be YYYY-MM-DD")
    start_utc, end_utc = _utc(raw.get("start_utc"), f"{rid}.start_utc"), _utc(raw.get("end_utc"), f"{rid}.end_utc")
    if end_utc <= start_utc:
        raise RegistryError(f"{rid}: end_utc must be after start_utc")
    tz = str(raw.get("timezone") or "")
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        raise RegistryError(f"{rid}: unknown timezone {tz!r}") from None
    start_local, end_local = str(raw.get("start_local") or ""), str(raw.get("end_local") or "")
    for label, local, utc in (("start", start_local, start_utc), ("end", end_local, end_utc)):
        try:
            t = dt.time.fromisoformat(local)
        except ValueError:
            raise RegistryError(f"{rid}: {label}_local must be HH:MM") from None
        expected = dt.datetime.combine(date, t, tzinfo=zone).astimezone(UTC)
        if expected != utc:
            raise RegistryError(
                f"{rid}: {label}_local {local} {tz} on {date} is {expected.isoformat()}, "
                f"but {label}_utc says {utc.isoformat()} -- local and UTC must agree")
    return MarketException(
        id=str(rid), classification=cls, status=status, symbols=symbols, provider=provider, feed=feed,
        date=date, timezone=tz, start_local=start_local, end_local=end_local, start_utc=start_utc, end_utc=end_utc,
        retry_appropriate=retry, invalidates_event_window=invalidates,
        evidence=str(raw["evidence"]).strip(), source=str(raw["source"]).strip(), notes=str(raw.get("notes") or "").strip(),
    )


_CACHE: Dict[Tuple[str, float], MarketExceptionRegistry] = {}


def load_registry_for_config(config) -> MarketExceptionRegistry:
    """The registry named by config `market.exception_registry`; an empty
    registry when the key is absent (so a config without a registry keeps
    the strict, exception-free validation behaviour)."""
    path = config.market_exception_registry_path
    if path is None:
        return MarketExceptionRegistry.empty()
    if not path.exists():
        raise RegistryError(f"configured market exception registry not found: {path}")
    key = (str(path), path.stat().st_mtime)
    if key not in _CACHE:
        rel = config.relative_to_repo(path)
        _CACHE[key] = MarketExceptionRegistry.load(path, source_label=rel)
    return _CACHE[key]


def intervals_by_class(entries: Iterable[MarketException]) -> Dict[ExceptionClass, List[Tuple[dt.datetime, dt.datetime, str]]]:
    out: Dict[ExceptionClass, List[Tuple[dt.datetime, dt.datetime, str]]] = {}
    for e in entries:
        out.setdefault(e.classification, []).append((e.start_utc, e.end_utc, e.id))
    return out
