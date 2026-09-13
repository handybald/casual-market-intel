"""BLS (Bureau of Labor Statistics) client.

Real, documented public API v2:
    POST https://api.bls.gov/publicAPI/v2/timeseries/data/
    Body: {"seriesid": [...], "startyear": "YYYY", "endyear": "YYYY",
           "registrationkey": "..."}  (registrationkey optional but raises
           rate limits and the per-request year/series limits)

FRED (fetch/fred.py) already covers the currently-required official
validation series (config/data_sources.yaml `providers.fred.official_series`),
so by default `providers.bls.series` is an EMPTY list and this adapter
fetches nothing -- that is an honest "0 series configured", not a stub
silently doing nothing while claiming to be finished. The adapter itself
IS implemented and tested: chunking by year-window (BLS limits how many
years a single request can cover), real response-shape validation,
immutable snapshots, and manifest tracking, matching the pattern used
by fetch/fred.py. Add series to `providers.bls.series` in
config/data_sources.yaml to actually use it.

NOT live-verified in this environment (no BLS_API_KEY exercised against
the real endpoint here) -- response parsing is built against the
documented shape only.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import requests

from ..config import AppConfig
from ..manifest import Manifest, ManifestEntry, checksum_bytes
from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)

_OK_STATUSES = {"REQUEST_SUCCEEDED", "REQUEST_SUCCEEDED_PARTIAL"}


class BlsResponseError(RuntimeError):
    pass


class Observation(NamedTuple):
    year: int
    period: str  # "M01".."M12" for monthly series
    period_name: str
    value: Optional[float]


def _snapshot_dir(config: AppConfig, series_id: str) -> Path:
    return config.provider_raw_dir("bls") / series_id


def _timestamp_for_filename(ts: dt.datetime) -> str:
    return f"{ts.strftime('%Y%m%dT%H%M%S')}_{ts.microsecond:06d}_{uuid.uuid4().hex[:8]}Z"


def year_windows(start_year: int, end_year: int, max_years: int) -> List[Tuple[int, int]]:
    """Split [start_year, end_year] into consecutive windows of at most
    `max_years` years each -- BLS v2 caps a single request's year span
    (10 years unregistered, 20 with a registration key; configured via
    `providers.bls.max_years_per_request`)."""
    if start_year > end_year:
        raise ValueError(f"start_year {start_year} after end_year {end_year}")
    windows = []
    y = start_year
    while y <= end_year:
        window_end = min(y + max_years - 1, end_year)
        windows.append((y, window_end))
        y = window_end + 1
    return windows


def fetch_bls_series_window(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    start_year: int,
    end_year: int,
    session: Optional[requests.Session] = None,
) -> Path:
    """One request covering [start_year, end_year] (already assumed to
    respect the year-window limit -- see year_windows). Writes an
    immutable snapshot and records a manifest entry keyed by series_id
    with the window expressed as Jan 1 start_year .. Dec 31 end_year."""
    provider_cfg = config.provider("bls")
    api_key = config.env("BLS_API_KEY")  # optional -- public low-volume use works without one

    sess = session or requests.Session()
    body = {"seriesid": [series_id], "startyear": str(start_year), "endyear": str(end_year)}
    if api_key:
        body["registrationkey"] = api_key

    start_iso = dt.date(start_year, 1, 1).isoformat()
    end_iso = dt.date(end_year, 12, 31).isoformat()

    try:
        response = request_with_retry(
            "POST",
            f"{provider_cfg['base_url']}/timeseries/data/",
            session=sess,
            max_retries=provider_cfg.get("max_retries", 5),
            json=body,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        logger.error("[BLS][%s] %s-%s FAILED: %s", series_id, start_year, end_year, exc)
        manifest.record(
            ManifestEntry(provider="bls", key=series_id, start=start_iso, end=end_iso, status="failed", error=str(exc))
        )
        raise

    payload = response.json()
    status = payload.get("status")
    if status not in _OK_STATUSES:
        error = f"BLS API status={status!r}: {payload.get('message')}"
        logger.error("[BLS][%s] %s-%s FAILED: %s", series_id, start_year, end_year, error)
        manifest.record(
            ManifestEntry(provider="bls", key=series_id, start=start_iso, end=end_iso, status="failed", error=error)
        )
        raise BlsResponseError(error)

    series_list = payload.get("Results", {}).get("series", [])
    series_data = next((s for s in series_list if s.get("seriesID") == series_id), None)
    if series_data is None:
        raise BlsResponseError(f"malformed BLS response for {series_id}: series not found in Results.series")
    observations = series_data.get("data", [])

    retrieved_at = dt.datetime.now(dt.timezone.utc)
    snapshot_dir = _snapshot_dir(config, series_id)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{_timestamp_for_filename(retrieved_at)}.json"

    raw_payload = {
        "series_id": series_id,
        "start_year": start_year,
        "end_year": end_year,
        "retrieved_at": retrieved_at.isoformat(),
        "observations": observations,
    }
    raw_bytes = json.dumps(raw_payload).encode("utf-8")
    snapshot_path.write_bytes(raw_bytes)
    checksum = checksum_bytes(raw_bytes)

    manifest.record(
        ManifestEntry(
            provider="bls", key=series_id, start=start_iso, end=end_iso,
            status="complete" if observations else "empty",
            rows=len(observations), checksum=checksum, path=str(snapshot_path),
        )
    )
    logger.info("[BLS][%s] %s-%s: %d observations -> %s", series_id, start_year, end_year, len(observations), snapshot_path)
    return snapshot_path


def fetch_bls_series(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    start_date: dt.date,
    end_date: dt.date,
    session: Optional[requests.Session] = None,
) -> List[Path]:
    """Chunks [start_date, end_date] into year-windows respecting
    `providers.bls.max_years_per_request` and fetches each. Returns the
    snapshot paths written (one per window)."""
    provider_cfg = config.provider("bls")
    max_years = int(provider_cfg.get("max_years_per_request", 10))
    windows = year_windows(start_date.year, end_date.year, max_years)

    sess = session or requests.Session()
    paths = []
    for start_year, end_year in windows:
        paths.append(fetch_bls_series_window(config, manifest, series_id, start_year, end_year, session=sess))
    return paths


def configured_series_ids(config: AppConfig) -> List[str]:
    return list(config.provider("bls").get("series", []))


def fetch_all_bls_series(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
) -> Dict[str, List[Path]]:
    session = requests.Session()
    out: Dict[str, List[Path]] = {}
    for series_id in configured_series_ids(config):
        try:
            out[series_id] = fetch_bls_series(config, manifest, series_id, start_date, end_date, session=session)
        except Exception as exc:  # noqa: BLE001 - already logged/recorded where raised
            logger.error("[BLS][%s] fetch failed: %s", series_id, exc)
    return out


def parse_observations(raw_payload: dict) -> List[Observation]:
    out = []
    for o in raw_payload.get("observations", []):
        raw_value = o.get("value")
        try:
            value = float(raw_value) if raw_value not in (None, "") else None
        except ValueError:
            value = None
        try:
            year = int(o["year"])
        except (KeyError, ValueError):
            continue
        out.append(Observation(year=year, period=o.get("period", ""), period_name=o.get("periodName", ""), value=value))
    return out
