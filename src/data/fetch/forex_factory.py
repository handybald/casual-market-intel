"""Forex Factory historical calendar downloader.

Canonical pre-release forecast/expectation source. Plain HTTP + parser
(no browser automation, no paid API/key -- scraping the public calendar
pages is the intended acquisition method): one GET per calendar month,
raw HTML cached to disk, parsed separately by normalize/forex_factory.py.

IMPORTANT: `forecast` values scraped here are stored as `provider_forecast`
with `forecast_source="FOREX_FACTORY"`, never as `economist_consensus` --
Forex Factory does not publish enough survey methodology to justify that
label. See config/event_mapping.yaml and src/data/schemas.py.

The month-page markup (calendar__row / calendar__time / calendar__currency
/ calendar__impact / calendar__event / calendar__actual / calendar__forecast
/ calendar__previous) reflects Forex Factory's long-standing calendar table
structure, used here via tests/fixtures/forex_factory_sample.html (a
hand-built fixture modeled on that documented structure -- NOT a captured
live page; see its own comment). It has not been re-verified against a
live fetch in this environment.

Checkpoint completion is validated, not assumed: `fetch_forex_factory_month`
parses the page (via normalize.forex_factory.parse_forex_factory_html) as
part of deciding whether to record "complete", not after blindly trusting
an HTTP 200. A page that downloads fine but yields zero parsed rows is
recorded FAILED, never silently checkpointed as done with rows=0.

RAW IMMUTABILITY: every attempt for a given month -- successful or
failed -- is written to its OWN uniquely-timestamped file under
`{raw_dir}/{year}-{month}/attempt_*.html`, never overwriting a prior
attempt (a previous version of this module reused one fixed filename per
month, so a failed retry would silently destroy the last good page).
`latest_verified.json` in that same directory points at whichever
attempt is currently trusted (updated only on complete/provisional
success, never on failure) -- normalize/fred.py's FRED snapshot design
follows the same pattern for the same reason.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from calendar import monthrange
from pathlib import Path
from typing import List, NamedTuple, Optional

import requests

from ..config import AppConfig
from ..dates import iter_date_chunks
from ..http_utils import request_with_retry
from ..manifest import Manifest, ManifestEntry, checksum_bytes

logger = logging.getLogger(__name__)

_MONTH_ABBR = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; causal-market-intel-research-bot/0.1; "
    "+https://github.com/) research data collection"
)

# Manifest key: one calendar month page covers every country/currency, so
# the cached raw artifact identity does not vary by currency filter --
# only the requested month does.
MANIFEST_KEY = "ALL"


def month_query_param(year: int, month: int) -> str:
    return f"{_MONTH_ABBR[month - 1]}.{year}"


def _month_dir(raw_dir: Path, year: int, month: int) -> Path:
    return raw_dir / f"{year:04d}-{month:02d}"


def _timestamp_for_filename(ts: dt.datetime) -> str:
    # Matches fetch/fred.py's scheme: timestamp prefix for rough
    # time-ordering, uuid suffix so two attempts in the same wall-clock
    # second (retries, fast tests) never collide and silently overwrite.
    return f"{ts.strftime('%Y%m%dT%H%M%S')}_{ts.microsecond:06d}_{uuid.uuid4().hex[:8]}Z"


def _attempt_path(raw_dir: Path, year: int, month: int, retrieved_at: dt.datetime) -> Path:
    return _month_dir(raw_dir, year, month) / f"attempt_{_timestamp_for_filename(retrieved_at)}.html"


def _pointer_path(raw_dir: Path, year: int, month: int) -> Path:
    return _month_dir(raw_dir, year, month) / "latest_verified.json"


def _read_pointer(raw_dir: Path, year: int, month: int) -> Optional[dict]:
    path = _pointer_path(raw_dir, year, month)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_pointer(raw_dir: Path, year: int, month: int, attempt_path: Path, retrieved_at: dt.datetime, checksum: str, rows: int) -> None:
    payload = {
        "attempt_file": attempt_path.name,
        "retrieved_at": retrieved_at.isoformat(),
        "checksum": checksum,
        "rows": rows,
    }
    pointer_path = _pointer_path(raw_dir, year, month)
    tmp_path = pointer_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(pointer_path)


def latest_verified_forex_factory_path(raw_dir: Path, year: int, month: int) -> Optional[Path]:
    """The attempt file currently trusted for this month, with checksum
    verification (a manually deleted/corrupted artifact is not silently
    accepted) -- analogous to fetch.fred.latest_verified_snapshot_path."""
    pointer = _read_pointer(raw_dir, year, month)
    if pointer is None:
        return None
    path = _month_dir(raw_dir, year, month) / pointer["attempt_file"]
    if not path.exists():
        return None
    expected_checksum = pointer.get("checksum")
    if expected_checksum is not None:
        try:
            if checksum_bytes(path.read_bytes()) != expected_checksum:
                logger.error("[ForexFactory] %s failed checksum verification -- refusing to use it", path)
                return None
        except OSError:
            return None
    return path


def latest_verified_retrieved_at(raw_dir: Path, year: int, month: int) -> Optional[dt.datetime]:
    """The ACTUAL acquisition time of the currently-trusted attempt for
    this month -- used so normalization never claims "acquired now" for
    data that was actually fetched on some earlier run."""
    pointer = _read_pointer(raw_dir, year, month)
    if pointer is None or "retrieved_at" not in pointer:
        return None
    try:
        return dt.datetime.fromisoformat(pointer["retrieved_at"])
    except ValueError:
        return None


class MonthFetchResult(NamedTuple):
    year: int
    month: int
    path: Path
    status: str  # "complete" | "provisional" | "empty" | "failed" | "skipped_cached" | "skipped_future"
    bytes_len: int
    parsed_rows: int = 0
    error: Optional[str] = None
    retrieved_at: Optional[dt.datetime] = None


class ParserFailureError(RuntimeError):
    """Raised when a page downloads fine but looks structurally unparsable
    (e.g. site markup changed) -- distinct from an empty-but-valid page."""


_CAPTCHA_PHRASES = (
    "solve this captcha", "solve the captcha", "complete this captcha", "complete the captcha",
    "verify you are human", "prove you are human", "i'm not a robot",
)


def _cheap_sanity_check(html: str) -> Optional[str]:
    """Fast structural checks that don't require running the real parser
    -- catch obviously-wrong responses (bot-block/challenge pages,
    truncated bodies) before spending time parsing.

    A bare "captcha" substring check was tried and rejected: a genuine,
    successfully-rendered Forex Factory calendar page embeds its own
    (unrelated, presumably used by some OTHER form on the site) reCAPTCHA
    site-key config (`recaptchasitekey: '...'`) on every page -- matching
    that substring flagged a REAL captured page as a block page. The
    checks below key on actual block-page LANGUAGE (a natural-language
    phrase like "solve this captcha", not the bare word) and on
    Cloudflare's specific challenge-page marker (confirmed against a real
    403 response from this exact site: `<title>Just a moment...</title>`)
    instead.
    """
    if len(html) < 500:
        return "response body suspiciously short"
    lowered = html.lower()
    if "calendar" not in lowered:
        return "response does not look like a Forex Factory calendar page"
    if "<title>just a moment" in lowered:
        return "response looks like a Cloudflare bot-challenge page (title: 'Just a moment...')"
    if any(phrase in lowered for phrase in _CAPTCHA_PHRASES):
        return "response looks like a bot-check / block page (captcha)"
    if "checking your browser" in lowered or "access denied" in lowered:
        return "response looks like a bot-check / block page"
    return None


def _is_current_month(year: int, month: int, today: dt.date) -> bool:
    return year == today.year and month == today.month


def fetch_forex_factory_month(
    config: AppConfig,
    manifest: Manifest,
    year: int,
    month: int,
    force: bool = False,
    session: Optional[requests.Session] = None,
    today: Optional[dt.date] = None,
) -> MonthFetchResult:
    # Local import: fetch.py depends on the parsing function for
    # checkpoint validation, but NOT on the full normalize pipeline
    # (currency filtering / event mapping / MacroEvent construction) --
    # parser/downloader separation is preserved at the module-responsibility
    # level, not by literally never importing the parser.
    from ..normalize.forex_factory import parse_forex_factory_page

    today = today or dt.date.today()
    provider_cfg = config.provider("forex_factory")
    raw_dir = config.provider_raw_dir("forex_factory")
    _month_dir(raw_dir, year, month).mkdir(parents=True, exist_ok=True)

    start = dt.date(year, month, 1).isoformat()
    end = dt.date(year, month, monthrange(year, month)[1]).isoformat()
    is_current = _is_current_month(year, month, today)

    if not force and not is_current and manifest.is_complete("forex_factory", MANIFEST_KEY, start, end):
        cached_path = latest_verified_forex_factory_path(raw_dir, year, month)
        if cached_path is not None:
            logger.info("[ForexFactory] %s already downloaded, skipping (force=False)", start)
            return MonthFetchResult(
                year, month, cached_path, "skipped_cached", cached_path.stat().st_size,
                retrieved_at=latest_verified_retrieved_at(raw_dir, year, month),
            )
        logger.warning("[ForexFactory] %s manifest says complete but the verified artifact is missing/corrupt -- refetching", start)

    url = f"{provider_cfg['base_url']}?month={month_query_param(year, month)}"
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

    retrieved_at = dt.datetime.now(dt.timezone.utc)

    try:
        response = request_with_retry(
            "GET",
            url,
            session=sess,
            max_retries=provider_cfg.get("max_retries", 5),
            request_delay_seconds=provider_cfg.get("request_delay_seconds", 1.5),
        )
    except Exception as exc:  # noqa: BLE001 - recorded in manifest, not swallowed
        logger.error("[ForexFactory] %s FAILED: %s", start, exc)
        manifest.record(
            ManifestEntry(provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status="failed", error=str(exc))
        )
        return MonthFetchResult(year, month, _month_dir(raw_dir, year, month), "failed", 0, error=str(exc), retrieved_at=retrieved_at)

    html = response.text
    attempt_path = _attempt_path(raw_dir, year, month, retrieved_at)

    # Preserve the raw response BEFORE further validation, as its OWN
    # immutable attempt file -- including ones that will end up marked
    # failed, needed to diagnose why parsing broke, and never overwriting
    # a prior (possibly still-good) attempt for this same month.
    attempt_path.write_text(html, encoding="utf-8")
    checksum = checksum_bytes(html.encode("utf-8"))

    sanity_error = _cheap_sanity_check(html)
    if sanity_error:
        logger.error("[ForexFactory] %s FAILED sanity check: %s", start, sanity_error)
        manifest.record(
            ManifestEntry(
                provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status="failed",
                error=sanity_error, checksum=checksum, path=str(attempt_path),
            )
        )
        return MonthFetchResult(year, month, attempt_path, "failed", len(html), error=sanity_error, retrieved_at=retrieved_at)

    try:
        parsed_rows = parse_forex_factory_page(html, year, month)
    except Exception as exc:  # noqa: BLE001 - parser broke on real markup change
        logger.error("[ForexFactory] %s FAILED to parse: %s", start, exc)
        manifest.record(
            ManifestEntry(
                provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status="failed",
                error=f"parser exception: {exc}", checksum=checksum, path=str(attempt_path),
            )
        )
        return MonthFetchResult(year, month, attempt_path, "failed", len(html), error=f"parser exception: {exc}", retrieved_at=retrieved_at)

    if len(parsed_rows) == 0:
        # A calendar month with literally zero events (any currency, any
        # impact) is not plausible for FF's covered date range -- treat
        # as suspected parser breakage or bot-block, NOT a verified-empty
        # checkpoint. This is the exact bug being fixed: previously this
        # was recorded "complete" with rows=0.
        error = "zero events parsed from page -- parser breakage or bot-block suspected"
        logger.error("[ForexFactory] %s FAILED: %s", start, error)
        manifest.record(
            ManifestEntry(
                provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status="failed",
                error=error, checksum=checksum, path=str(attempt_path),
            )
        )
        return MonthFetchResult(year, month, attempt_path, "failed", len(html), parsed_rows=0, error=error, retrieved_at=retrieved_at)

    in_requested_month = sum(1 for r in parsed_rows if r.date.year == year and r.date.month == month)
    if in_requested_month == 0:
        error = f"parsed {len(parsed_rows)} rows but none fall within requested month {start[:7]} -- requested-month identity mismatch"
        logger.error("[ForexFactory] %s FAILED: %s", start, error)
        manifest.record(
            ManifestEntry(
                provider="forex_factory", key=MANIFEST_KEY, start=start, end=end, status="failed",
                error=error, checksum=checksum, path=str(attempt_path),
            )
        )
        return MonthFetchResult(year, month, attempt_path, "failed", len(html), parsed_rows=len(parsed_rows), error=error, retrieved_at=retrieved_at)

    status = "provisional" if is_current else "complete"
    manifest.record(
        ManifestEntry(
            provider="forex_factory",
            key=MANIFEST_KEY,
            start=start,
            end=end,
            status=status,
            rows=len(parsed_rows),  # real parsed count, never a 0 placeholder
            checksum=checksum,
            path=str(attempt_path),
        )
    )
    # Only a successful (complete/provisional) attempt becomes "the"
    # currently-trusted artifact for this month -- a failure never moves
    # the pointer, so normalize keeps reading the last good page.
    _write_pointer(raw_dir, year, month, attempt_path, retrieved_at, checksum, len(parsed_rows))
    logger.info("[ForexFactory] %s %s (%d bytes, %d rows parsed)", start, status, len(html), len(parsed_rows))
    return MonthFetchResult(year, month, attempt_path, status, len(html), parsed_rows=len(parsed_rows), retrieved_at=retrieved_at)


def fetch_forex_factory(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
    force: bool = False,
    today: Optional[dt.date] = None,
) -> List[MonthFetchResult]:
    """Single entry point: user gives a wide date range, this determines
    every calendar month in between and fetches each (skipping cached
    months unless force=True; the current month is always re-fetched
    since it's provisional; future months are skipped entirely -- there
    is nothing real to fetch yet)."""
    today = today or dt.date.today()
    results: List[MonthFetchResult] = []
    session = requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)

    for chunk in iter_date_chunks(start_date, end_date, frequency="month"):
        if chunk.start > today:
            logger.info("[ForexFactory] %s is in the future, skipping (nothing to fetch yet)", chunk.label)
            results.append(
                MonthFetchResult(
                    chunk.start.year, chunk.start.month,
                    _month_dir(config.provider_raw_dir("forex_factory"), chunk.start.year, chunk.start.month),
                    "skipped_future", 0,
                )
            )
            continue
        result = fetch_forex_factory_month(
            config, manifest, chunk.start.year, chunk.start.month, force=force, session=session, today=today
        )
        results.append(result)

    failed = [r for r in results if r.status == "failed"]
    if failed:
        logger.warning(
            "[ForexFactory] %d/%d month(s) failed: %s",
            len(failed),
            len(results),
            ", ".join(f"{r.year:04d}-{r.month:02d}" for r in failed),
        )
    return results
