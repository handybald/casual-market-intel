"""Versioned descriptor + deterministic fingerprint of a canonical market dataset.

A descriptor (committed JSON under metadata/datasets/) identifies exactly which
local, gitignored parquet data a research run used. It records provenance,
source policy, coverage, the known-exception registry, per-year artifacts and
checksums, plus one overall `fingerprint`.

WHAT ENTERS THE FINGERPRINT (`fingerprint_payload`), and nothing else:
  - descriptor_schema_version, dataset_name
  - source: provider, feed, feed_scope, timeframe, adjustment, bar_timestamp
  - source_policy (OHLC / volume / VWAP / reference-provider roles)
  - benchmark: requested start, evaluation end
  - known-exception registry: registry_version + content_sha256 of the
    parsed entries (format/comment/path independent)
  - per symbol: first/last timestamp, total rows, expected and represented
    NYSE sessions, provisional windows, and per year: rows, data
    content_sha256, sessions, regular-session expected/observed/known-gap/
    excluded/unexplained minutes
NOT in the fingerprint: created_at_utc, artifact paths, file-byte SHA-256s
(normalized files embed `normalized_at_utc`, a wall-clock value, so their
bytes change on every re-normalization even when the data does not), the
manifest's operational statuses, and any formatting. The canonical
serialization is JSON with sorted keys and no whitespace.

`content_sha256` of a yearly frame hashes the scientifically relevant columns
in a fixed order and dtype: timestamp_utc (int64 ns UTC), open/high/low/
close/volume/vwap (float64, NaN normalized), transactions (int64, -1 for
null), and the single-valued provenance columns source/feed/feed_scope/
adjustment/timeframe. Rows are sorted by timestamp first.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from .config import AppConfig
from .fetch.market_provider import MarketDataProvider
from .manifest import Manifest, checksum_file
from .market_exceptions import MarketExceptionRegistry
from .validation.market import nyse_sessions, validate_market_bars

DESCRIPTOR_SCHEMA_VERSION = 1
NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "vwap")
PROVENANCE_COLUMNS = ("source", "feed", "feed_scope", "adjustment", "timeframe")

SOURCE_POLICY = {
    "ohlc": {"canonical": "alpaca/sip", "note": "raw (unadjusted) consolidated-tape 1-minute bars"},
    "volume": {"canonical": "alpaca/sip",
               "note": "provider volume definitions differ (auction/block prints); never mix another provider's volume into features"},
    "vwap": {"canonical": "alpaca/sip",
             "note": "Massive's provider-reported vw is a different, provider-native quantity; never interchanged"},
    "reference_providers": {
        "massive": "validation/reference only",
        "alpaca/iex": "sparse single-venue sanity check only",
    },
}


class DescriptorMismatch(ValueError):
    """Local data no longer matches a committed descriptor."""


def content_sha256(df: pd.DataFrame) -> str:
    """Deterministic hash of a canonical bar frame's scientific content."""
    df = df.sort_values("timestamp_utc", kind="mergesort").reset_index(drop=True)
    h = hashlib.sha256()
    ts = pd.to_datetime(df["timestamp_utc"], utc=True)
    h.update(b"timestamp_utc\0" + ts.astype("int64").to_numpy(dtype="<i8").tobytes())
    for col in NUMERIC_COLUMNS:
        arr = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype="<f8", na_value=np.nan).copy() if col in df else np.full(len(df), np.nan)
        arr[np.isnan(arr)] = np.nan  # one canonical NaN bit pattern
        h.update(col.encode() + b"\0" + arr.astype("<f8").tobytes())
    tx = pd.to_numeric(df["transactions"], errors="coerce") if "transactions" in df else pd.Series([np.nan] * len(df))
    h.update(b"transactions\0" + tx.fillna(-1).astype("int64").to_numpy(dtype="<i8").tobytes())
    for col in PROVENANCE_COLUMNS:
        values = sorted(df[col].dropna().astype(str).unique()) if col in df else []
        h.update(col.encode() + b"\0" + json.dumps(values).encode())
    return h.hexdigest()


def fingerprint_payload(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """Exactly the scientifically relevant subset of a descriptor (see module docstring)."""
    symbols = {}
    for sym, s in descriptor["symbols"].items():
        symbols[sym] = {
            "first_timestamp_utc": s["first_timestamp_utc"], "last_timestamp_utc": s["last_timestamp_utc"],
            "total_rows": s["total_rows"], "expected_sessions": s["expected_sessions"],
            "represented_sessions": s["represented_sessions"], "provisional_windows": s["provisional_windows"],
            "years": {y: {k: v[k] for k in (
                "rows", "content_sha256", "expected_sessions", "represented_sessions",
                "regular_expected_minutes", "regular_observed_minutes", "known_gap_minutes",
                "excluded_minutes", "unexplained_missing_minutes")} for y, v in s["years"].items()},
        }
    reg = descriptor["known_exception_registry"]
    return {
        "descriptor_schema_version": descriptor["descriptor_schema_version"],
        "dataset_name": descriptor["dataset_name"],
        "source": descriptor["source"],
        "source_policy": descriptor["source_policy"],
        "benchmark": descriptor["benchmark"],
        "known_exception_registry": {"registry_version": reg["registry_version"], "content_sha256": reg["content_sha256"]},
        "symbols": symbols,
    }


def compute_fingerprint(descriptor: Dict[str, Any]) -> str:
    canonical = json.dumps(fingerprint_payload(descriptor), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def build_descriptor(
    config: AppConfig,
    provider: MarketDataProvider,
    registry: MarketExceptionRegistry,
    dataset_name: str,
    symbols: Iterable[str],
    benchmark_start: dt.date,
    evaluation_end: dt.date,
    created_at: Optional[dt.datetime] = None,
    as_of: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Describe the locally stored canonical dataset (reads files only)."""
    caps = provider.capabilities()
    timeframe = config.market_timeframe
    as_of = as_of or dt.datetime.combine(evaluation_end + dt.timedelta(days=1), dt.time(12), tzinfo=dt.timezone.utc)
    manifest = Manifest(config.manifest_path)
    reg_rel = config.relative_to_repo(config.market_exception_registry_path) if config.market_exception_registry_path else None
    out: Dict[str, Any] = {
        "descriptor_schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "dataset_name": dataset_name,
        "created_at_utc": (created_at or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0).isoformat(),
        "source": {"provider": caps.provider, "feed": caps.feed, "feed_scope": caps.feed_scope, "timeframe": timeframe,
                   "adjustment": caps.adjustment, "bar_timestamp": caps.bar_timestamp},
        "source_policy": SOURCE_POLICY,
        "benchmark": {"requested_start": benchmark_start.isoformat(), "evaluation_end": evaluation_end.isoformat()},
        "known_exception_registry": {"path": reg_rel, "registry_version": registry.registry_version,
                                     "content_sha256": registry.content_sha256(), "entries": len(registry.entries)},
        "symbols": {},
    }
    for sym in symbols:
        exceptions = registry.for_dataset(sym, provider.name, caps.feed)
        key = provider.cache_key(sym, timeframe)
        years: Dict[str, Any] = {}
        first = last = None
        totals = {"rows": 0, "expected_sessions": 0, "represented_sessions": 0}
        for year in range(benchmark_start.year, evaluation_end.year + 1):
            norm_path, raw_path = provider.interim_year_path(sym, timeframe, year), provider.year_path(sym, timeframe, year)
            if not norm_path.exists():
                raise FileNotFoundError(f"canonical normalized file missing: {config.relative_to_repo(norm_path)}")
            df = pd.read_parquet(norm_path)
            ts = pd.to_datetime(df["timestamp_utc"], utc=True)
            lo = max(benchmark_start, dt.date(year, 1, 1))
            hi = min(evaluation_end, dt.date(year, 12, 31))
            report = validate_market_bars(df, sym, lo, hi, timeframe_minutes=1, as_of=as_of,
                                          bar_density=caps.bar_density, exceptions=exceptions)
            sched = nyse_sessions(lo, hi)
            represented = sum(bool(((ts >= o) & (ts < c)).any()) for o, c in zip(sched.market_open, sched.market_close))
            raw_df = pd.read_parquet(raw_path)
            norm_hash = content_sha256(df)
            raw_hash = content_sha256(raw_df.assign(**{c: df[c].iloc[0] for c in PROVENANCE_COLUMNS})) if len(df) else None
            years[str(year)] = {
                "rows": int(len(df)), "content_sha256": norm_hash,
                "raw_content_matches_normalized": raw_hash == norm_hash,
                "expected_sessions": report.expected_trading_sessions, "represented_sessions": int(represented),
                "regular_expected_minutes": report.expected_regular_minutes,
                "regular_observed_minutes": report.regular_hours_rows - _bars_in_excluded(ts, exceptions),
                "regular_coverage": round((report.regular_hours_rows - _bars_in_excluded(ts, exceptions))
                                          / report.expected_regular_minutes, 6) if report.expected_regular_minutes else None,
                "known_gap_minutes": report.known_gap_minutes, "known_gap_sessions": report.known_gap_sessions,
                "excluded_minutes": report.excluded_minutes_by_class,
                "unexplained_missing_minutes": report.unexplained_missing_minutes,
                "applied_exception_ids": report.applied_exception_ids,
                "validation": ("hard_failure" if report.is_hard_failure else "known_provider_gaps" if report.known_gap_minutes
                               else "clean" if report.is_clean else "issues"),
                "artifacts": {
                    "normalized": {"path": config.relative_to_repo(norm_path), "file_sha256": checksum_file(norm_path)},
                    "raw": {"path": config.relative_to_repo(raw_path), "file_sha256": checksum_file(raw_path)},
                },
            }
            if len(df):
                first = min(first, ts.min()) if first is not None else ts.min()
                last = max(last, ts.max()) if last is not None else ts.max()
            totals["rows"] += len(df)
            totals["expected_sessions"] += report.expected_trading_sessions
            totals["represented_sessions"] += int(represented)
        entries = [e for e in manifest.entries_for(provider.name, key)
                   if benchmark_start.isoformat() <= e.start <= evaluation_end.isoformat()]
        status_counts: Dict[str, int] = {}
        for e in entries:
            status_counts[e.status] = status_counts.get(e.status, 0) + 1
        out["symbols"][sym] = {
            "first_timestamp_utc": first.isoformat() if first is not None else None,
            "last_timestamp_utc": last.isoformat() if last is not None else None,
            "total_rows": totals["rows"], "expected_sessions": totals["expected_sessions"],
            "represented_sessions": totals["represented_sessions"],
            # Recent windows still inside the provider revision horizon: the
            # data may still change, so this state is part of the identity.
            "provisional_windows": sorted(f"{e.start}..{e.end}" for e in entries if e.status == "provisional"),
            "manifest_operational_state": {
                "status_counts": dict(sorted(status_counts.items())),
                "failed_windows": sorted(f"{e.start}..{e.end}" for e in entries if e.status == "failed"),
                "complete_with_known_gaps_windows": sorted(f"{e.start}..{e.end}" for e in entries
                                                           if e.status == "complete_with_known_gaps"),
                "note": "operational acquisition state; not part of the fingerprint",
            },
            "years": years,
        }
    out["fingerprint"] = compute_fingerprint(out)
    return out


def _bars_in_excluded(ts: pd.Series, exceptions) -> int:
    """Bars inside registered no-trade intervals (should be 0) are not counted as observed tradable minutes."""
    n = 0
    for e in exceptions:
        if e.classification.value in ("market_wide_halt", "exchange_closed", "legitimate_no_trade_interval"):
            n += int(((ts >= e.start_utc) & (ts < e.end_utc)).sum())
    return n


def write_descriptor(descriptor: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_against(committed: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Differences between a committed descriptor and the current local data
    (empty list = same scientific dataset)."""
    problems = []
    if committed.get("fingerprint") != compute_fingerprint(committed):
        problems.append("committed descriptor's fingerprint does not match its own contents (edited by hand?)")
    if committed.get("fingerprint") != current.get("fingerprint"):
        a, b = fingerprint_payload(committed), fingerprint_payload(current)
        for sym in sorted(set(a["symbols"]) | set(b["symbols"])):
            sa, sb = a["symbols"].get(sym, {}), b["symbols"].get(sym, {})
            for y in sorted(set(sa.get("years", {})) | set(sb.get("years", {}))):
                if sa.get("years", {}).get(y) != sb.get("years", {}).get(y):
                    problems.append(f"{sym} {y}: yearly data/coverage differs")
            for k in ("first_timestamp_utc", "last_timestamp_utc", "total_rows", "provisional_windows"):
                if sa.get(k) != sb.get(k):
                    problems.append(f"{sym}: {k} differs ({sa.get(k)} -> {sb.get(k)})")
        for k in ("source", "source_policy", "benchmark", "known_exception_registry", "dataset_name"):
            if a[k] != b[k]:
                problems.append(f"{k} differs")
        if not problems:
            problems.append("fingerprint differs")
    return problems
