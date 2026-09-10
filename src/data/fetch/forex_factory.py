"""Forex Factory historical calendar downloader.

Canonical pre-release forecast/expectation source. Plain HTTP + parser
(no browser automation): one GET per calendar month, raw HTML cached to
disk, parsed separately by normalize/forex_factory.py.

IMPORTANT: `forecast` values scraped here are stored as `provider_forecast`
with `forecast_source="FOREX_FACTORY"`, never as `economist_consensus` --
Forex Factory does not publish enough survey methodology to justify that
label. See config/event_mapping.yaml and src/data/schemas.py.

The month-page markup (calendar__row / calendar__time / calendar__currency
/ calendar__impact / calendar__event / calendar__actual / calendar__forecast
/ calendar__previous) reflects Forex Factory's long-standing calendar table
structure. It has not been re-verified against a live fetch in this
environment -- if normalize/forex_factory.py starts producing zero rows,
the site markup likely changed and the parser needs updating (see
`detect_parser_failure` below, which raises instead of silently returning
an empty-but-"successful" result).
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import List, NamedTuple, Optional

import requests

from calendar import monthrange

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


def month_query_param(year: int, month: int) -> str:
    return f"{_MONTH_ABBR[month - 1]}.{year}"


def raw_html_path(raw_dir: Path, year: int, month: int) -> Path:
    return raw_dir / f"{year:04d}-{month:02d}.html"


class MonthFetchResult(NamedTuple):
    year: int
    month: int
    path: Path
    status: str  # "complete" | "empty" | "failed" | "skipped_cached"
    bytes_len: int
    error: Optional[str] = None


class ParserFailureError(RuntimeError):
    """Raised when a page downloads fine but looks structurally unparsable
    (e.g. site markup changed) -- distinct from an empty-but-valid page."""


def detect_parser_failure(html: str) -> Optional[str]:
    """Cheap structural sanity check on a downloaded page, run before
    caching it as a valid raw file. Returns an error string, or None if
    the page looks like a normal calendar page (populated or genuinely
    empty for that month)."""
    if len(html) < 500:
        return "response body suspiciously short"
    lowered = html.lower()
    if "calendar" not in lowered:
        return "response does not look like a Forex Factory calendar page"
    if "captcha" in lowered or "access denied" in lowered or "cloudflare" in lowered and "checking your browser" in lowered:
        return "response looks like a bot-check / block page"
    return None


def fetch_forex_factory_month(
    config: AppConfig,
    manifest: Manifest,
    year: int,
    month: int,
    force: bool = False,
    session: Optional[requests.Session] = None,
) -> MonthFetchResult:
    provider_cfg = config.provider("forex_factory")
    raw_dir = config.provider_raw_dir("forex_factory")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_html_path(raw_dir, year, month)

    key = "US"  # calendar month page is country/currency-agnostic; filtering happens at normalize time
    start = dt.date(year, month, 1).isoformat()
    end = dt.date(year, month, monthrange(year, month)[1]).isoformat()

    if not force and manifest.is_complete("forex_factory", key, start, end) and path.exists():
        logger.info("[ForexFactory] %s already downloaded, skipping (force=False)", start)
        return MonthFetchResult(year, month, path, "skipped_cached", path.stat().st_size)

    url = f"{provider_cfg['base_url']}?month={month_query_param(year, month)}"
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

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
            ManifestEntry(
                provider="forex_factory",
                key=key,
                start=start,
                end=end,
                status="failed",
                error=str(exc),
            )
        )
        return MonthFetchResult(year, month, path, "failed", 0, error=str(exc))

    html = response.text
    failure_reason = detect_parser_failure(html)
    if failure_reason:
        logger.error("[ForexFactory] %s FAILED parse sanity check: %s", start, failure_reason)
        manifest.record(
            ManifestEntry(
                provider="forex_factory",
                key=key,
                start=start,
                end=end,
                status="failed",
                error=failure_reason,
            )
        )
        return MonthFetchResult(year, month, path, "failed", len(html), error=failure_reason)

    path.write_text(html, encoding="utf-8")
    checksum = checksum_bytes(html.encode("utf-8"))

    manifest.record(
        ManifestEntry(
            provider="forex_factory",
            key=key,
            start=start,
            end=end,
            status="complete",
            rows=0,  # row count filled in by normalize step
            checksum=checksum,
            path=str(path),
        )
    )
    logger.info("[ForexFactory] %s downloaded (%d bytes)", start, len(html))
    return MonthFetchResult(year, month, path, "complete", len(html))


def fetch_forex_factory(
    config: AppConfig,
    manifest: Manifest,
    start_date: dt.date,
    end_date: dt.date,
    force: bool = False,
) -> List[MonthFetchResult]:
    """Single entry point: user gives a wide date range, this determines
    every calendar month in between and fetches each (skipping cached
    months unless force=True)."""
    results: List[MonthFetchResult] = []
    session = requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)

    for chunk in iter_date_chunks(start_date, end_date, frequency="month"):
        result = fetch_forex_factory_month(
            config, manifest, chunk.start.year, chunk.start.month, force=force, session=session
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
