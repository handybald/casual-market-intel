"""Regression tests for bounded retry/backoff behavior (required test #2:
429, timeout, server error, and exhausted-retries handling)."""
import requests

import pytest

from src.data.http_utils import request_with_retry, FetchError


class _FakeResponse:
    def __init__(self, status_code, text="ok"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class _FakeSession:
    def __init__(self, behaviors):
        """`behaviors` is a list of either an int status code, an
        Exception instance to raise, or "ok" for a 200 response,
        consumed one per call to `.request()`."""
        self.behaviors = list(behaviors)
        self.calls = 0

    def request(self, method, url, timeout=None, **kwargs):
        self.calls += 1
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "ok":
            return _FakeResponse(200)
        return _FakeResponse(behavior)


def test_retries_on_429_then_succeeds():
    session = _FakeSession([429, 429, "ok"])
    response = request_with_retry("GET", "http://x", session=session, max_retries=5, base_delay=0.001)
    assert response.status_code == 200
    assert session.calls == 3


def test_retries_on_server_error_then_succeeds():
    session = _FakeSession([500, 503, "ok"])
    response = request_with_retry("GET", "http://x", session=session, max_retries=5, base_delay=0.001)
    assert response.status_code == 200
    assert session.calls == 3


def test_retries_on_timeout_then_succeeds():
    session = _FakeSession([requests.Timeout("timed out"), "ok"])
    response = request_with_retry("GET", "http://x", session=session, max_retries=5, base_delay=0.001)
    assert response.status_code == 200
    assert session.calls == 2


def test_retries_on_connection_error_then_succeeds():
    session = _FakeSession([requests.ConnectionError("refused"), "ok"])
    response = request_with_retry("GET", "http://x", session=session, max_retries=5, base_delay=0.001)
    assert response.status_code == 200


def test_exhausts_retries_and_raises_fetch_error():
    session = _FakeSession([503, 503, 503])
    with pytest.raises(FetchError):
        request_with_retry("GET", "http://x", session=session, max_retries=3, base_delay=0.001)
    assert session.calls == 3


def test_non_retryable_4xx_raises_immediately_without_retry():
    session = _FakeSession([404])
    with pytest.raises(requests.HTTPError):
        request_with_retry("GET", "http://x", session=session, max_retries=5, base_delay=0.001)
    assert session.calls == 1  # no retry burned on a permanent client error
