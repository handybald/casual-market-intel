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

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


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
                url,
                exc,
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
                    url,
                )
            else:
                response.raise_for_status()
                return response

        if attempt >= max_retries:
            break
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)
        time.sleep(delay)

    raise FetchError(
        f"exhausted {max_retries} retries for {method} {url}"
    ) from last_exc
