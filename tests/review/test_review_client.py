"""Try to break the Chess.com client's HTTP behaviour."""

from __future__ import annotations

import httpx
import pytest

from chess_coach.chesscom import ChessComClient, ChessComError, ChessComUnavailable
from tests.chesscom_fake import NoopLimiter

ME = "deitythemenace"


def _client(handler, **kwargs) -> ChessComClient:
    kwargs.setdefault("limiter", NoopLimiter())
    kwargs.setdefault("sleep", lambda _s: None)
    return ChessComClient(user_agent="t", transport=httpx.MockTransport(handler), **kwargs)


def test_a_304_without_an_etag_header_keeps_the_etag_we_sent():
    """If the 304 dropped the ETag, the stored one would be overwritten with
    NULL and every later sync would re-download the month in full."""
    client = _client(lambda r: httpx.Response(304))
    resp = client.month(ME, "2026-11", etag='W/"abc"')
    assert resp.not_modified
    assert resp.etag == 'W/"abc"'


def test_every_attempt_spends_a_token_including_network_failures():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"archives": []})

    limiter = NoopLimiter()
    client = _client(handler, limiter=limiter)
    client.archive_months(ME)
    assert limiter.acquired == len(calls) == client.requests_sent == 3


def test_a_429_without_retry_after_penalises_by_the_backoff_it_sleeps():
    statuses = [429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        return httpx.Response(status, json={"archives": []})

    limiter, sleeps = NoopLimiter(), []
    client = _client(handler, limiter=limiter, sleep=sleeps.append, rng=lambda: 0.5)
    client.archive_months(ME)
    assert sleeps == [0.75]
    assert limiter.penalties == sleeps


def test_a_limiter_failure_sends_nothing():
    """If Redis is down the client must fail closed, not fall back to sending
    unthrottled requests."""
    calls = []

    class Broken:
        def acquire(self) -> None:
            raise ConnectionError("redis down")

        def penalize(self, seconds: float) -> None:
            raise AssertionError("not reached")

    client = _client(lambda r: calls.append(1) or httpx.Response(200), limiter=Broken())
    with pytest.raises(ConnectionError):
        client.archive_months(ME)
    assert calls == []


def test_a_non_json_200_is_an_error_not_an_empty_month():
    """An HTML error page served with 200 (a CDN in front of the API) must not
    parse as 'this month has no games', which would then be frozen as final."""
    client = _client(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ValueError):
        client.month(ME, "2026-10")


def test_a_month_body_without_a_games_key_is_an_error():
    client = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ChessComError):
        client.month(ME, "2026-10")


def test_an_absurd_retry_after_fails_fast_instead_of_sleeping():
    sleeps: list[float] = []
    client = _client(
        lambda r: httpx.Response(429, headers={"retry-after": "3600"}), sleep=sleeps.append
    )
    with pytest.raises(ChessComUnavailable):
        client.archive_months(ME)
    assert sleeps == []


def test_retries_exhausted_on_mixed_failures_report_the_last_problem():
    statuses = iter([503, 429, 502])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses))

    client = _client(handler, max_retries=2)
    with pytest.raises(ChessComUnavailable, match="502"):
        client.archive_months(ME)


def test_410_gone_for_a_closed_account_is_not_retried():
    calls = []
    client = _client(lambda r: calls.append(1) or httpx.Response(410, text="gone"))
    with pytest.raises(ChessComError):
        client.profile(ME)
    assert calls == [1]
