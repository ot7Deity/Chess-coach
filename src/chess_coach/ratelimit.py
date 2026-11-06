"""A token bucket shared by every ingest worker, held in Redis.

Chess.com's rate limit applies to the client, not to one process of it, so a
per-process limiter would let N workers send N times the budget. The bucket
lives in Redis and every worker draws from it.

Three choices worth knowing about:

* **Atomic Lua, Redis's clock.** Read-refill-take happens in one script, so two
  workers cannot both see "one token left" and both take it. The script reads
  time from Redis (`TIME`) rather than accepting a timestamp from the caller:
  worker clocks drift, and a worker whose clock runs ahead would otherwise
  refill the bucket early for everyone.

* **Reservation, not polling.** Taking a token may drive the balance negative;
  the script returns how long the caller must wait before its token is "real".
  Each request is one round trip and one sleep, and waiting callers are served
  roughly in the order they asked instead of racing in a retry loop.

* **A 429 is shared information.** When one worker is told to slow down, the
  budget everyone shares is the thing that is exhausted. `penalize` pushes the
  shared balance into debt, so every worker backs off, not just the one that
  happened to get the 429.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

import redis

# Shared by both scripts: refill the bucket for the time elapsed since it was
# last touched, using Redis's clock.
_REFILL = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
"""

# Keep the key until any debt is repaid plus a minute; after that a missing key
# and a full bucket are the same thing.
_STORE = """
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', key, math.ceil(((burst - tokens) / rate + 60) * 1000))
"""

# Returns a string: Redis truncates Lua numbers to integers on the way out,
# which would round every sub-second wait down to zero.
_RESERVE = (
    _REFILL
    + """
tokens = tokens - tonumber(ARGV[3])
"""
    + _STORE
    + """
if tokens >= 0 then
  return '0'
end
return tostring(-tokens / rate)
"""
)

_PENALIZE = (
    _REFILL
    + """
local floor = -tonumber(ARGV[3]) * rate
if tokens > floor then
  tokens = floor
end
"""
    + _STORE
)


class RateLimiter(Protocol):
    """What the Chess.com client needs from a limiter."""

    def acquire(self) -> None:
        """Block until one request may be sent."""

    def penalize(self, seconds: float) -> None:
        """Stop everyone sharing this limiter from sending for `seconds`."""


class RedisTokenBucket:
    def __init__(
        self,
        client: redis.Redis,
        *,
        key: str = "ratelimit:chesscom",
        rate_per_sec: float = 1.0,
        burst: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._key = key
        self._rate = rate_per_sec
        self._burst = burst
        self._sleep = sleep
        self._reserve = client.register_script(_RESERVE)
        self._penalize = client.register_script(_PENALIZE)

    def reserve(self, cost: int = 1) -> float:
        """Take `cost` tokens now; return the seconds to wait before using them."""
        wait = self._reserve(keys=[self._key], args=[self._rate, self._burst, cost])
        return float(wait)

    def acquire(self) -> None:
        wait = self.reserve()
        if wait > 0:
            self._sleep(wait)

    def penalize(self, seconds: float) -> None:
        if seconds > 0:
            self._penalize(keys=[self._key], args=[self._rate, self._burst, seconds])
