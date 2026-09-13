"""FRED (Federal Reserve Economic Data) client.

Used for scientific validation and revision history, NOT as the
historical expectation source. Real, documented public API:
https://fred.stlouisfed.org/docs/api/fred/series_observations.html

Design (see config/data_sources.yaml `providers.fred` comments):

- Every fetch requests the FULL configured range (with a lookback
  buffer -- see `lookback_buffer_days`), never an incremental narrow
  window. These series are small (a few hundred monthly rows over a
  decade); the cost of a full refetch is trivial, and it is the only
  simple way to guarantee we (a) pick up observations released later
  but dated earlier, and (b) see revisions to already-published
  periods, without implementing per-observation ALFRED vintage queries.
- Each fetch is preserved as its OWN immutable, timestamped raw JSON
  snapshot -- never overwritten. `data/raw/fred/{series_id}/{iso_ts}.json`.
- A response with fewer observations than a prior good snapshot for the
  same series is treated with suspicion: it is saved (for diagnosis) but
  recorded FAILED, and normalize/fred.py keeps using the last verified
  snapshot -- coverage is never silently regressed by a bad/short fetch.
- Pagination uses FRED's documented `count`/`offset`/`limit` fields.

LIVE-VERIFIED (2026-09-10, using a real FRED_API_KEY): a real request
for UNRATE observations returned exactly the documented shape this
module expects (`count`/`offset`/`limit`/`observations[].{date,value,
realtime_start}`), confirming the response contract end-to-end for a
single page. Multi-page pagination itself was exercised only against a
mocked response (these series are small enough that a real fetch never
needs a second page).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..config import AppConfig
from ..manifest import Manifest, ManifestEntry, checksum_bytes
from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)


class MissingCredentialsError(RuntimeError):
    pass


class FredResponseError(RuntimeError):
    pass


def _snapshot_dir(config: AppConfig, series_id: str) -> Path:
    return config.provider_raw_dir("fred") / series_id


def _timestamp_for_filename(ts: dt.datetime) -> str:
    # Immutable snapshots must never collide: two fetches within the same
    # wall-clock second (plausible under retries or fast test runs) would
    # otherwise silently overwrite each other's "immutable" file. A short
    # uuid suffix guarantees uniqueness while the timestamp prefix keeps
    # directory listings roughly time-ordered for human inspection.
    return f"{ts.strftime('%Y%m%dT%H%M%S')}_{ts.microsecond:06d}_{uuid.uuid4().hex[:8]}Z"


def fetch_fred_series_snapshot(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    start_date: dt.date,
    end_date: dt.date,
    session: Optional[requests.Session] = None,
) -> Path:
    """Always performs a fresh full-range fetch (see module docstring) and
    writes a new immutable snapshot file. Returns the snapshot path.
    Raises on error; the manifest entry recorded reflects success/failure
    either way -- callers should not assume this always returns a usable
    path without checking the manifest status.
    """
    provider_cfg = config.provider("fred")
    api_key = config.env("FRED_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "FRED_API_KEY not set (see .env.example). Cannot fetch FRED series."
        )

    lookback_days = int(provider_cfg.get("lookback_buffer_days", 400))
    fetch_start = start_date - dt.timedelta(days=lookback_days)

    sess = session or requests.Session()
    all_observations: List[dict] = []
    offset = 0
    limit = 100000  # FRED's own max page size
    total_count: Optional[int] = None

    while True:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": fetch_start.isoformat(),
            "observation_end": end_date.isoformat(),
            "limit": str(limit),
            "offset": str(offset),
        }
        response = request_with_retry(
            "GET",
            f"{provider_cfg['base_url']}/series/observations",
            session=sess,
            max_retries=provider_cfg.get("max_retries", 5),
            params=params,
        )
        payload = response.json()
        if "observations" not in payload:
            raise FredResponseError(
                f"malformed FRED response for {series_id}: missing 'observations' "
                f"(error_message={payload.get('error_message')!r})"
            )
        batch = payload["observations"]
        total_count = payload.get("count", len(all_observations) + len(batch))

        if not batch and offset < total_count:
            # The server declared more rows exist (`count`) than we've
            # received so far, but this page came back empty -- an early
            # empty page before pagination is actually done. Silently
            # breaking here (the old behavior) would accept a truncated
            # result as if it were complete.
            raise FredResponseError(
                f"malformed FRED pagination for {series_id}: empty page at offset={offset} "
                f"but declared count={total_count} (only {len(all_observations)} received so far)"
            )

        all_observations.extend(batch)

        # Advance by the ACTUAL page size received, not the limit we
        # requested -- a server that returns fewer rows than asked for
        # must not cause us to skip observations.
        offset += len(batch)
        if offset >= total_count or not batch:
            break

    if total_count is not None and len(all_observations) != total_count:
        raise FredResponseError(
            f"incomplete FRED pagination for {series_id}: received {len(all_observations)} "
            f"observations but the server declared count={total_count}"
        )

    retrieved_at = dt.datetime.now(dt.timezone.utc)
    snapshot_dir = _snapshot_dir(config, series_id)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{_timestamp_for_filename(retrieved_at)}.json"

    raw_payload = {
        "series_id": series_id,
        "requested_observation_start": fetch_start.isoformat(),
        "requested_observation_end": end_date.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "count": total_count,
        "observations": all_observations,
    }
    raw_bytes = json.dumps(raw_payload).encode("utf-8")
    snapshot_path.write_bytes(raw_bytes)
    checksum = checksum_bytes(raw_bytes)

    prior_good = _read_latest_verified_pointer(snapshot_dir)
    n_obs = len(all_observations)

    # Reject not just a total collapse to zero, but any regression versus
    # the last verified snapshot: a response that's merely SHORTER (e.g.
    # 2 observations where there used to be 2, but now missing one) must
    # not silently become the new "latest verified" snapshot either.
    # Comparison is scoped to the overlap between this request's range
    # and the prior snapshot's own requested range, so a deliberately
    # narrower follow-up request isn't flagged as a false regression.
    regression_error = _detect_coverage_regression(snapshot_dir, prior_good, all_observations, fetch_start, end_date)

    # NOTE ON THE MANIFEST HERE: FRED always re-requests the SAME
    # [start_date, end_date] interval (by design -- see module docstring),
    # so the manifest's composite key (provider:key:start:end) collides
    # across every attempt and each `record()` call overwrites the last
    # one. That's fine for the manifest's own "was the most recent run
    # for this series OK" bookkeeping, but it means the manifest CANNOT
    # be used to recover which snapshot file was last verified-good after
    # a later attempt fails -- that's what the `latest_verified.json`
    # pointer file (written only on success, below) is for.
    if regression_error:
        logger.error("[FRED][%s] %s", series_id, regression_error)
        manifest.record(
            ManifestEntry(
                provider="fred", key=series_id,
                start=start_date.isoformat(), end=end_date.isoformat(),
                status="failed", rows=n_obs, checksum=checksum, path=str(snapshot_path), error=regression_error,
            )
        )
        return snapshot_path

    status = "complete" if n_obs > 0 else "empty"
    manifest.record(
        ManifestEntry(
            provider="fred", key=series_id,
            start=start_date.isoformat(), end=end_date.isoformat(),
            status=status, rows=n_obs, checksum=checksum, path=str(snapshot_path),
        )
    )
    _write_latest_verified_pointer(snapshot_dir, snapshot_path, retrieved_at, n_obs, fetch_start, end_date, checksum)
    logger.info("[FRED][%s] %s: %d observations -> %s", series_id, status, n_obs, snapshot_path)
    return snapshot_path


def _observation_dates_in_range(observations: List[dict], start: dt.date, end: dt.date) -> set:
    dates = set()
    for o in observations:
        try:
            d = dt.date.fromisoformat(o["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if start <= d <= end:
            dates.add(d)
    return dates


def _detect_coverage_regression(
    snapshot_dir: Path,
    prior_good: Optional[dict],
    new_observations: List[dict],
    fetch_start: dt.date,
    end_date: dt.date,
) -> Optional[str]:
    """None if the new response is acceptable; an error string if it
    looks like a regression versus the last verified snapshot."""
    if prior_good is None or prior_good.get("rows", 0) == 0:
        return None
    n_obs = len(new_observations)

    prior_start_raw = prior_good.get("observation_start")
    prior_end_raw = prior_good.get("observation_end")
    if prior_start_raw is None or prior_end_raw is None:
        # Legacy pointer written before range tracking existed -- fall
        # back to the coarser "collapsed entirely to zero" signal only.
        if n_obs == 0:
            return (
                f"empty response (0 observations) but the last verified snapshot "
                f"({prior_good.get('snapshot_file')}) had {prior_good['rows']} -- treating as failed"
            )
        return None

    prior_start, prior_end = dt.date.fromisoformat(prior_start_raw), dt.date.fromisoformat(prior_end_raw)
    overlap_start, overlap_end = max(fetch_start, prior_start), min(end_date, prior_end)
    if overlap_start > overlap_end:
        return None  # genuinely non-overlapping ranges -- a smaller/different request is not a regression

    try:
        prior_payload = json.loads((snapshot_dir / prior_good["snapshot_file"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError):
        return None  # can't compare against an unreadable prior snapshot -- don't block on it

    prior_dates = _observation_dates_in_range(prior_payload.get("observations", []), overlap_start, overlap_end)
    new_dates = _observation_dates_in_range(new_observations, overlap_start, overlap_end)
    missing = sorted(prior_dates - new_dates)
    if missing:
        preview = ", ".join(d.isoformat() for d in missing[:5])
        return (
            f"new response is missing {len(missing)} observation date(s) present in the last "
            f"verified snapshot within the overlapping range [{overlap_start}, {overlap_end}] "
            f"(e.g. {preview}) -- treating as an incomplete/regressed response, NOT advancing coverage"
        )
    return None


def _pointer_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / "latest_verified.json"


def _read_latest_verified_pointer(snapshot_dir: Path) -> Optional[dict]:
    path = _pointer_path(snapshot_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_latest_verified_pointer(
    snapshot_dir: Path,
    snapshot_path: Path,
    retrieved_at: dt.datetime,
    rows: int,
    observation_start: dt.date,
    observation_end: dt.date,
    checksum: str,
) -> None:
    payload = {
        "snapshot_file": snapshot_path.name,
        "retrieved_at": retrieved_at.isoformat(),
        "rows": rows,
        "observation_start": observation_start.isoformat(),
        "observation_end": observation_end.isoformat(),
        "checksum": checksum,
    }
    tmp_path = _pointer_path(snapshot_dir).with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(_pointer_path(snapshot_dir))


def latest_verified_snapshot_path(config: AppConfig, series_id: str) -> Optional[Path]:
    """The most recent snapshot that was actually recorded good (complete
    or verified-empty) for this series -- NOT necessarily the most
    recently written file, and deliberately independent of the manifest
    (see the note in fetch_fred_series_snapshot about why). Verifies the
    artifact's checksum (when the pointer recorded one) before trusting
    it -- a manually deleted/corrupted file, or one from a pointer
    written by older code without a checksum, is not silently accepted."""
    snapshot_dir = _snapshot_dir(config, series_id)
    pointer = _read_latest_verified_pointer(snapshot_dir)
    if pointer is None:
        return None
    path = snapshot_dir / pointer["snapshot_file"]
    if not path.exists():
        return None
    expected_checksum = pointer.get("checksum")
    if expected_checksum is not None:
        try:
            if checksum_bytes(path.read_bytes()) != expected_checksum:
                logger.error(
                    "[FRED] %s failed checksum verification against its latest_verified.json pointer "
                    "-- refusing to treat it as the current snapshot", path,
                )
                return None
        except OSError:
            return None
    return path


def unique_series_ids(config: AppConfig) -> List[str]:
    seen = []
    for item in config.provider("fred").get("official_series", []):
        sid = item["series_id"]
        if sid not in seen:
            seen.append(sid)
    return seen


def _asof_snapshot_dir(config: AppConfig, series_id: str) -> Path:
    return _snapshot_dir(config, series_id) / "asof"


def fetch_observations_as_of(
    config: AppConfig,
    manifest: Manifest,
    series_id: str,
    observation_start: dt.date,
    observation_end: dt.date,
    as_of_date: dt.date,
    session: Optional[requests.Session] = None,
) -> Path:
    """A real ALFRED vintage query: `realtime_start=realtime_end=as_of_date`
    returns every observation in [observation_start, observation_end] EXACTLY
    as it stood on that one date -- i.e. what was actually known/published
    by then, not today's revised numbers. This is the historically-honest
    input for comparing a calendar's `actual` against an official value
    (see normalize/fred.py's normalize_fred_asof_events).

    One request regardless of how many periods [observation_start,
    observation_end] covers -- but ONE request PER as_of_date, which is
    why this is NOT called automatically for every period in a full
    historical bootstrap (see module docstring). Intended for a small,
    deliberately chosen set of as-of dates (e.g. specific historical
    release dates pulled from already-normalized MQL5/Forex Factory rows).

    Writes its own immutable snapshot under `{series}/asof/`, distinct
    from the default (latest-revised) snapshots directory, and records a
    manifest entry keyed by `{series_id}:asof:{as_of_date}` so it never
    collides with or is confused for the default full-refresh checkpoint.
    """
    provider_cfg = config.provider("fred")
    api_key = config.env("FRED_API_KEY")
    if not api_key:
        raise MissingCredentialsError(
            "FRED_API_KEY not set (see .env.example). Cannot fetch FRED series."
        )

    sess = session or requests.Session()
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": observation_start.isoformat(),
        "observation_end": observation_end.isoformat(),
        "realtime_start": as_of_date.isoformat(),
        "realtime_end": as_of_date.isoformat(),
    }
    response = request_with_retry(
        "GET",
        f"{provider_cfg['base_url']}/series/observations",
        session=sess,
        max_retries=provider_cfg.get("max_retries", 5),
        params=params,
    )
    payload = response.json()
    if "observations" not in payload:
        raise FredResponseError(
            f"malformed FRED as-of response for {series_id}@{as_of_date}: missing 'observations' "
            f"(error_message={payload.get('error_message')!r})"
        )
    observations = payload["observations"]

    retrieved_at = dt.datetime.now(dt.timezone.utc)
    snapshot_dir = _asof_snapshot_dir(config, series_id)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{as_of_date.isoformat()}_{_timestamp_for_filename(retrieved_at)}.json"

    raw_payload = {
        "series_id": series_id,
        "as_of_date": as_of_date.isoformat(),
        "observation_start": observation_start.isoformat(),
        "observation_end": observation_end.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "observations": observations,
    }
    raw_bytes = json.dumps(raw_payload).encode("utf-8")
    snapshot_path.write_bytes(raw_bytes)
    checksum = checksum_bytes(raw_bytes)

    key = f"{series_id}:asof:{as_of_date.isoformat()}"
    manifest.record(
        ManifestEntry(
            provider="fred", key=key,
            start=observation_start.isoformat(), end=observation_end.isoformat(),
            status="complete" if observations else "empty",
            rows=len(observations), checksum=checksum, path=str(snapshot_path),
        )
    )
    logger.info("[FRED][%s] as-of %s: %d observations -> %s", series_id, as_of_date, len(observations), snapshot_path)
    return snapshot_path


def fetch_all_fred_series(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
) -> Dict[str, Path]:
    session = requests.Session()
    out: Dict[str, Path] = {}
    for series_id in unique_series_ids(config):
        try:
            out[series_id] = fetch_fred_series_snapshot(config, manifest, series_id, start_date, end_date, session=session)
        except MissingCredentialsError:
            raise
        except Exception as exc:  # noqa: BLE001 - already logged/recorded where raised
            logger.error("[FRED][%s] fetch failed: %s", series_id, exc)
            manifest.record(
                ManifestEntry(
                    provider="fred", key=series_id,
                    start=start_date.isoformat(), end=end_date.isoformat(),
                    status="failed", error=str(exc),
                )
            )
    return out
