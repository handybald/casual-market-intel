"""FRED (Federal Reserve Economic Data) client.

Used for scientific validation and revision history, NOT as the
historical expectation source. Real, documented public API:
https://api.stlouisfed.org/fred/series/observations

Kept deliberately simple for v0 (per architecture notes): one request
per configured series covering the full requested range. FRED responses
for the monthly macro series we care about (CPI, NFP, unemployment,
average hourly earnings) are a few hundred rows even over a decade, so
no pagination/chunking is needed here -- unlike Forex Factory/Massive/MQL5.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..config import AppConfig
from ..manifest import Manifest, ManifestEntry, checksum_bytes
from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)


class MissingCredentialsError(RuntimeError):
    pass


def fetch_fred_series(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    start_date: dt.date,
    end_date: dt.date,
    force: bool = False,
    session: Optional[requests.Session] = None,
) -> Path:
    provider_cfg = config.provider("fred")
    api_key = config.env("FRED_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "FRED_API_KEY not set (see .env.example). Cannot fetch FRED series."
        )

    raw_dir = config.provider_raw_dir("fred")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{series_id}.json"

    start = start_date.isoformat()
    end = end_date.isoformat()

    if not force and manifest.is_complete("fred", series_id, start, end) and path.exists():
        logger.info("[FRED] %s already downloaded, skipping (force=False)", series_id)
        return path

    sess = session or requests.Session()
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
        "observation_end": end,
    }
    try:
        response = request_with_retry(
            "GET",
            f"{provider_cfg['base_url']}/series/observations",
            session=sess,
            max_retries=provider_cfg.get("max_retries", 5),
            params=params,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[FRED] %s FAILED: %s", series_id, exc)
        manifest.record(
            ManifestEntry(provider="fred", key=series_id, start=start, end=end, status="failed", error=str(exc))
        )
        raise

    payload = response.json()
    observations = payload.get("observations", [])
    raw_bytes = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw_bytes)

    manifest.record(
        ManifestEntry(
            provider="fred",
            key=series_id,
            start=start,
            end=end,
            status="complete" if observations else "empty",
            rows=len(observations),
            checksum=checksum_bytes(raw_bytes),
            path=str(path),
        )
    )
    logger.info("[FRED] %s: %d observations", series_id, len(observations))
    return path


def fetch_fred_series_map(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
    force: bool = False,
) -> Dict[str, Path]:
    provider_cfg = config.provider("fred")
    series_map: Dict[str, str] = provider_cfg.get("series", {})
    session = requests.Session()

    out: Dict[str, Path] = {}
    for series_id in series_map:
        try:
            out[series_id] = fetch_fred_series(
                config, manifest, series_id, start_date, end_date, force=force, session=session
            )
        except MissingCredentialsError:
            raise
        except Exception:  # noqa: BLE001 - already logged/recorded above
            continue
    return out
