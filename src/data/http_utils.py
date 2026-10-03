"""Small HTTP helper: bounded exponential backoff + polite rate limiting.

Deliberately not using a heavyweight retry framework -- the policy is
simple enough (retry on timeout/connection error/429/5xx, bounded
attempts, exponential backoff with jitter) to keep inline and easy to
reason about.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests

from .redaction import redact_secrets  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# `requests` embeds the full request URL -- query string included -- in
# HTTPError and ConnectionError messages, and Massive authenticates via
# `apiKey=` in the query string; every message built here goes through the
# shared last-line sanitizer (see src/data/redaction.py).
_ERROR_BODY_MAX_CHARS = 300


class FetchError(RuntimeError):
    """Raised when a request exhausts all retries."""


def request_with_retry(
    method: str,
    url: str,
    *,
    session: Optional[requests.Session] = None,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    timeout: float = 30.0,
    request_delay_seconds: float = 0.0,
    **kwargs,
) -> requests.Response:
    """Perform an HTTP request with bounded exponential backoff + jitter.

    Retries on: connection errors, timeouts, and RETRYABLE_STATUS_CODES.
    Does NOT retry on other 4xx (those are treated as permanent failures,
    e.g. 404/401) -- those raise immediately via raise_for_status().
    """
    sess = session or requests.Session()
    attempt = 0
    last_exc: Optional[BaseException] = None

    while attempt < max_retries:
        attempt += 1
        if request_delay_seconds:
            time.sleep(request_delay_seconds)
        try:
            response = sess.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            logger.warning(
                "request error (attempt %d/%d) %s %s: %s",
                attempt,
                max_retries,
                method,
                redact_secrets(url),
                redact_secrets(str(exc)),
            )
        else:
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_exc = FetchError(
                    f"retryable status {response.status_code} from {url}"
                )
                logger.warning(
                    "retryable HTTP %d (attempt %d/%d) %s",
                    response.status_code,
                    attempt,
                    max_retries,
                    redact_secrets(url),
                )
            else:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    pass
                else:
                    return response
                # Raised OUTSIDE the except block so the original
                # exception (whose message carries the full URL,
                # credentials included) is neither chained nor kept
                # as __context__. The provider's own error body is
                # kept -- usually the only statement of WHY (e.g. an
                # entitlement limit).
                # Redact the FULL body first, THEN truncate: truncating
                # first could cut a credential so the remaining fragment
                # no longer matches the known secret and leaks.
                body = redact_secrets(str(getattr(response, "text", "") or ""))[:_ERROR_BODY_MAX_CHARS]
                raise requests.HTTPError(
                    f"HTTP {response.status_code} for {method} {redact_secrets(url)}: {body}",
                    response=response,
                )

        if attempt >= max_retries:
            break
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)
        time.sleep(delay)

    # Not chained to `last_exc`: its message may carry the full URL with
    # credentials; the redacted text is included instead.
    raise FetchError(
        f"exhausted {max_retries} retries for {method} {redact_secrets(url)}"
        + (f": {redact_secrets(str(last_exc))}" if last_exc is not None else "")
    )
