"""Try to break the shared token bucket with real concurrency.

The existing tests call `reserve()` from one thread and check the returned
numbers. The property that actually matters is about *send times*: across every
thread and process sharing a key, no window of length T may contain more than
`burst + rate * T` requests. These tests measure that directly.
"""

from __future__ import annotations

import multiprocessing
import threading
import time

import pytest
import redis

from chess_coach.ratelimit import RedisTokenBucket
from tests.review import _procs

pytestmark = pytest.mark.redis

# Send times are taken after the reply crosses the network and the sleep ends,
# so a send can be observed late but never early. Two sends observed with
# different latencies can look closer together than their slots were; this is
# the allowance for that, in seconds.
_JITTER_SEC = 0.05


def _worst_window_excess(stamps: list[float], rate: float, burst: int) -> float:
    """Max over all windows of (sends in window) - (allowed in window). <= 0 is good."""
    stamps = sorted(stamps)
    worst = float("-inf")
    for i in range(len(stamps)):
        for j in range(i, len(stamps)):
            sends = j - i + 1
            allowed = burst + rate * (stamps[j] - stamps[i] + _JITTER_SEC)
            worst = max(worst, sends - allowed)
    return worst


def test_threads_with_their_own_connections_never_exceed_the_rate(redis_client, redis_url):
    key = redis_client.key_prefix + "bucket"
    rate, burst, threads, per_thread = 10.0, 2, 6, 5
    stamps: list[float] = []
    guard = threading.Lock()
    start = threading.Barrier(threads)

    def worker() -> None:
        conn = redis.Redis.from_url(redis_url)
        try:
            bucket = RedisTokenBucket(conn, key=key, rate_per_sec=rate, burst=burst)
            start.wait()
            for _ in range(per_thread):
                bucket.acquire()
                now = time.monotonic()
                with guard:
                    stamps.append(now)
        finally:
            conn.close()

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=30)

    total = threads * per_thread
    assert len(stamps) == total
    assert _worst_window_excess(stamps, rate, burst) <= 0
    # And it is a limiter, not a stall: the whole run takes about as long as
    # the budget requires, not much longer.
    span = max(stamps) - min(stamps)
    assert (total - burst) / rate - _JITTER_SEC <= span <= (total - burst) / rate + 1.0


def test_separate_processes_never_exceed_the_rate(redis_client, redis_url):
    """The claim the bucket exists for: N worker *processes* get one budget."""
    key = redis_client.key_prefix + "bucket"
    rate, burst, procs, per_proc = 8.0, 2, 3, 6
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(procs) as pool:
        results = pool.starmap(
            _procs.acquire_tokens, [(redis_url, key, rate, burst, per_proc)] * procs
        )
    stamps = [s for proc in results for s in proc]
    assert len(stamps) == procs * per_proc
    assert _worst_window_excess(stamps, rate, burst) <= 0


def test_the_window_check_catches_a_per_process_limiter(redis_client, redis_url):
    """Control for the two tests above: give each process its own key (the
    'limiter per process' design INTERVIEW_PREP rejects) and the same check
    must fail. Otherwise the check proves nothing."""
    rate, burst, procs, per_proc = 8.0, 2, 3, 6
    ctx = multiprocessing.get_context("spawn")
    args = [
        (redis_url, redis_client.key_prefix + f"own{i}", rate, burst, per_proc)
        for i in range(procs)
    ]
    with ctx.Pool(procs) as pool:
        results = pool.starmap(_procs.acquire_tokens, args)
    stamps = [s for proc in results for s in proc]
    assert _worst_window_excess(stamps, rate, burst) > 1


def test_a_small_penalty_never_forgives_existing_debt(redis_client):
    """penalize() must only ever lengthen the wait. If it assigned the floor
    unconditionally, a 2 s Retry-After arriving while the bucket was 4 s in
    debt would cut everyone's wait short."""
    bucket = RedisTokenBucket(
        redis_client, key=redis_client.key_prefix + "bucket", rate_per_sec=1.0, burst=1
    )
    for _ in range(5):
        bucket.reserve()  # balance is now about -4
    bucket.penalize(2)
    assert bucket.reserve() == pytest.approx(5.0, abs=0.1)


def test_the_key_outlives_a_long_penalty(redis_client):
    """If the key expired before the debt was repaid, the next caller would find
    no key, read it as a full bucket, and the penalty would silently vanish."""
    key = redis_client.key_prefix + "bucket"
    bucket = RedisTokenBucket(redis_client, key=key, rate_per_sec=1.0, burst=3)
    bucket.penalize(120)
    assert redis_client.pttl(key) >= 120_000
    assert bucket.reserve() >= 119.0


@pytest.mark.parametrize("rate", [3.0, 7.0])
def test_fractional_waits_are_exact_not_truncated(redis_client, rate):
    """The string-return trick, for waits that are not round numbers."""
    bucket = RedisTokenBucket(
        redis_client, key=redis_client.key_prefix + f"bucket{rate}", rate_per_sec=rate, burst=1
    )
    bucket.reserve()
    wait = bucket.reserve()
    assert wait == pytest.approx(1 / rate, abs=0.02)
    assert wait > 0


def test_works_with_a_decode_responses_client(redis_url, redis_client):
    """The CLI builds a bytes client; any other caller may not."""
    conn = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        bucket = RedisTokenBucket(
            conn, key=redis_client.key_prefix + "bucket", rate_per_sec=5.0, burst=1
        )
        assert bucket.reserve() == 0.0
        assert bucket.reserve() == pytest.approx(0.2, abs=0.05)
    finally:
        conn.close()
