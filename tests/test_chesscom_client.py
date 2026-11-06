"""The client's HTTP behaviour: identification, revalidation, retries."""

from __future__ import annotations

import httpx
import pytest

from chess_coach.chesscom import (
    ChessComClient,
    ChessComError,
    ChessComUnavailable,
    PlayerNotFound,
    normalize_username,
)
from tests.chesscom_fake import FakeChessCom, NoopLimiter, make_game


@pytest.fixture
def fake() -> FakeChessCom:
    f = FakeChessCom()
    f.add_player("deitythemenace")
    f.add_game("deitythemenace", "2026-10", make_game("g1", white="deitythemenace", black="x"))
    return f


def test_every_request_carries_the_contact_user_agent():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={"archives": []})

    client = ChessComClient(
        user_agent="ChessCoach/0.1 (me@example.com)",
        limiter=NoopLimiter(),
        transport=httpx.MockTransport(handler),
    )
    client.archive_months("someone")
    assert seen == ["ChessCoach/0.1 (me@example.com)"]


def test_archive_urls_become_sorted_months(fake: FakeChessCom):
    fake.add_game("deitythemenace", "2025-01", make_game("g0", white="deitythemenace", black="x"))
    assert fake.client().archive_months("DeityTheMenace") == ["2025-01", "2026-10"]


def test_an_unrecognised_archive_url_fails_loudly():
    """Skipping it would silently drop a month of history."""
    client = ChessComClient(
        user_agent="t",
        limiter=NoopLimiter(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"archives": ["https://x/games/latest"]})
        ),
    )
    with pytest.raises(ChessComError):
        client.archive_months("someone")


def test_replaying_the_etag_gets_a_304_with_no_games(fake: FakeChessCom):
    client = fake.client()
    first = client.month("deitythemenace", "2026-10")
    assert not first.not_modified and len(first.games) == 1 and first.etag

    second = client.month("deitythemenace", "2026-10", etag=first.etag)
    assert second.not_modified
    assert second.games == []
    assert second.etag == first.etag


def test_unknown_player_is_player_not_found(fake: FakeChessCom):
    with pytest.raises(PlayerNotFound):
        fake.client().profile("nobody-here")


@pytest.mark.parametrize("bad", ["../admin", "a?b=c", "user/games", "", "x" * 51])
def test_usernames_that_could_change_the_url_are_rejected_before_any_request(bad):
    fake = FakeChessCom()
    with pytest.raises(ValueError):
        fake.client().profile(bad)
    assert fake.log == []


def test_normalize_username_lowercases_and_strips():
    assert normalize_username("  DeityTheMenace ") == "deitythemenace"


def test_429_backs_off_for_retry_after_and_penalises_the_shared_bucket(fake: FakeChessCom):
    """A 429 means the budget all workers share is spent, so the penalty goes to
    the shared limiter, not only this process's sleep."""
    path = "player/deitythemenace/games/archives"
    fake.fail(path, 429)
    limiter, sleeps = NoopLimiter(), []

    # Retry-After is set by the fake via a custom handler wrapper.
    def handler(request: httpx.Request) -> httpx.Response:
        resp = fake.handle(request)
        if resp.status_code == 429:
            return httpx.Response(429, headers={"retry-after": "7"})
        return resp

    client = ChessComClient(
        user_agent="t",
        limiter=limiter,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    assert client.archive_months("deitythemenace") == ["2026-10"]
    assert sleeps == [7.0]
    assert limiter.penalties == [7.0]
    # The retry spent a token too: a retry is a request to Chess.com.
    assert limiter.acquired == 2
    assert client.requests_sent == 2


def test_5xx_is_retried_with_growing_jittered_backoff(fake: FakeChessCom):
    path = "player/deitythemenace/games/archives"
    fake.fail(path, 503, 502, 500)
    sleeps: list[float] = []
    client = fake.client(sleep=sleeps.append, rng=lambda: 1.0, backoff_base_sec=1.0)
    assert client.archive_months("deitythemenace") == ["2026-10"]
    # Equal jitter with rng=1.0 is the full window: 1, 2, 4.
    assert sleeps == [1.0, 2.0, 4.0]


def test_backoff_never_drops_below_half_the_window(fake: FakeChessCom):
    """Full jitter could retry instantly after being told to slow down."""
    fake.fail("player/deitythemenace/games/archives", 503, 503)
    sleeps: list[float] = []
    fake.client(sleep=sleeps.append, rng=lambda: 0.0).archive_months("deitythemenace")
    assert sleeps == [0.5, 1.0]


def test_backoff_is_capped(fake: FakeChessCom):
    fake.fail("player/deitythemenace/games/archives", *[500] * 5)
    sleeps: list[float] = []
    client = fake.client(sleep=sleeps.append, rng=lambda: 1.0, backoff_cap_sec=3.0)
    client.archive_months("deitythemenace")
    assert max(sleeps) == 3.0


def test_retries_are_bounded(fake: FakeChessCom):
    fake.fail("player/deitythemenace/games/archives", *[500] * 10)
    client = fake.client(max_retries=2)
    with pytest.raises(ChessComUnavailable):
        client.archive_months("deitythemenace")
    assert client.requests_sent == 3


def test_network_errors_are_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"archives": []})

    client = ChessComClient(
        user_agent="t",
        limiter=NoopLimiter(),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert client.archive_months("someone") == []
    assert len(calls) == 2


def test_other_4xx_is_not_retried():
    """A 400 is our bug; retrying it only spends rate budget."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    client = ChessComClient(
        user_agent="t", limiter=NoopLimiter(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ChessComError):
        client.archive_months("someone")
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{}, {"games": None}, [], {"games": "nope"}])
def test_a_month_without_a_games_list_is_an_error_not_an_empty_month(body):
    """Read as 'no games', a malformed 200 would freeze a finished month empty."""
    client = ChessComClient(
        user_agent="t",
        limiter=NoopLimiter(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )
    with pytest.raises(ChessComError):
        client.month("someone", "2026-08")


@pytest.mark.parametrize("status", [429, 503])
def test_an_absurd_retry_after_fails_fast_instead_of_sleeping(status):
    """Found by the reviewer: the cap used to clamp to 300 s and retry, so a
    server asking for an hour kept a sync asleep for 25 minutes."""
    limiter, sleeps = NoopLimiter(), []
    client = ChessComClient(
        user_agent="t",
        limiter=limiter,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, headers={"retry-after": "3600"})
        ),
        sleep=sleeps.append,
    )
    with pytest.raises(ChessComUnavailable, match="3600"):
        client.archive_months("someone")
    assert sleeps == []
    assert client.requests_sent == 1
    # Other workers still back off, by the cap rather than the full hour.
    assert limiter.penalties == ([300.0] if status == 429 else [])


def test_a_large_configured_backoff_cap_does_not_trip_the_retry_after_limit():
    """The fail-fast applies to waits the *server* asks for, not our own backoff."""
    sleeps: list[float] = []
    calls = []

    def handler(request):
        calls.append(1)
        return (
            httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"archives": []})
        )

    client = ChessComClient(
        user_agent="t",
        limiter=NoopLimiter(),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        rng=lambda: 1.0,
        backoff_base_sec=500.0,
        backoff_cap_sec=1000.0,
    )
    assert client.archive_months("someone") == []
    assert sleeps == [500.0]
