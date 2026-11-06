"""Entry points for tests that need real separate processes.

They live in an importable module, not a test file, because Windows starts
child processes with `spawn`, which re-imports the target by module path.
"""

from __future__ import annotations

import time


def acquire_tokens(redis_url: str, key: str, rate: float, burst: int, n: int) -> list[float]:
    """Acquire `n` tokens from a shared bucket; return wall-clock send times."""
    import redis

    from chess_coach.ratelimit import RedisTokenBucket

    client = redis.Redis.from_url(redis_url)
    try:
        bucket = RedisTokenBucket(client, key=key, rate_per_sec=rate, burst=burst)
        stamps = []
        for _ in range(n):
            bucket.acquire()
            stamps.append(time.time())
        return stamps
    finally:
        client.close()
