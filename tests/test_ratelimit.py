"""The shared token bucket, against a real Redis.

A mocked Redis would test the mock: the property that matters is that several
processes drawing from one key are limited together.
"""

from __future__ import annotations

import time

import pytest
import redis

from chess_coach.ratelimit import RedisTokenBucket

pytestmark = pytest.mark.redis


def _bucket(client, **kwargs) -> RedisTokenBucket:
    kwargs.setdefault("key", client.key_prefix + "bucket")
    return RedisTokenBucket(client, **kwargs)


def test_burst_is_free_then_requests_are_spaced_at_the_rate(redis_client):
    bucket = _bucket(redis_client, rate_per_sec=1.0, burst=3)
    waits = [bucket.reserve() for _ in range(5)]
    assert waits[:3] == [0.0, 0.0, 0.0]
    assert waits[3] == pytest.approx(1.0, abs=0.1)
    assert waits[4] == pytest.approx(2.0, abs=0.1)


def test_two_workers_share_one_budget(redis_client, redis_url):
    """The point of putting the bucket in Redis: a second process with its own
    connection does not get a second budget."""
    other = redis.Redis.from_url(redis_url)
    try:
        a = _bucket(redis_client, rate_per_sec=1.0, burst=2)
        b = RedisTokenBucket(
            other, key=redis_client.key_prefix + "bucket", rate_per_sec=1.0, burst=2
        )
        assert a.reserve() == 0.0
        assert b.reserve() == 0.0
        assert b.reserve() == pytest.approx(1.0, abs=0.1)
        assert a.reserve() == pytest.approx(2.0, abs=0.1)
    finally:
        other.close()


def test_tokens_refill_over_time(redis_client):
    bucket = _bucket(redis_client, rate_per_sec=20.0, burst=1)
    assert bucket.reserve() == 0.0
    assert bucket.reserve() > 0
    time.sleep(0.25)  # repays the debt (0.05 s) and refills the one token
    assert bucket.reserve() == 0.0


def test_penalty_applies_to_every_worker(redis_client):
    bucket = _bucket(redis_client, rate_per_sec=1.0, burst=3)
    bucket.penalize(5)
    assert bucket.reserve() == pytest.approx(6.0, abs=0.1)


def test_sub_second_waits_survive_the_trip_out_of_lua(redis_client):
    """Redis truncates Lua numbers to integers; the script returns strings so a
    0.2 s wait is not rounded down to 'go now'."""
    bucket = _bucket(redis_client, rate_per_sec=5.0, burst=1)
    bucket.reserve()
    assert bucket.reserve() == pytest.approx(0.2, abs=0.05)


def test_the_key_expires_once_idle(redis_client):
    bucket = _bucket(redis_client, rate_per_sec=1.0, burst=3)
    bucket.reserve()
    ttl = redis_client.pttl(redis_client.key_prefix + "bucket")
    assert 0 < ttl <= 62_000


def test_acquire_sleeps_for_the_reservation(redis_client):
    slept: list[float] = []
    bucket = _bucket(redis_client, rate_per_sec=1.0, burst=1, sleep=slept.append)
    bucket.acquire()
    bucket.acquire()
    assert slept == [pytest.approx(1.0, abs=0.1)]


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (-1, 1), (1, 0)])
def test_nonsense_configuration_is_rejected(redis_client, rate, burst):
    with pytest.raises(ValueError):
        _bucket(redis_client, rate_per_sec=rate, burst=burst)


@pytest.mark.parametrize("label", ["with[brackets]", "star*", "q?"])
def test_the_redis_fixture_cleans_up_even_for_parametrized_names(redis_client, label):
    """The fixture's cleanup is a SCAN MATCH glob on the key prefix. A raw
    parametrized test name ('...[with[brackets]]') is itself a glob, matched
    nothing, and leaked every key (M3 found 24 in its Redis DB)."""
    assert all(c.isalnum() or c in "_-:" for c in redis_client.key_prefix)
    redis_client.set(redis_client.key_prefix + "probe", 1)
    assert list(redis_client.scan_iter(match=redis_client.key_prefix + "*"))
