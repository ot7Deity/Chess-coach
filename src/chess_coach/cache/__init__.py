"""The position cache (M3). See `position_cache` for the design.

The analysis worker should depend only on `chess_coach.positions.EvalCache`
and get its instance from `build_eval_cache()`, so POSITION_CACHE decides
between Redis + Postgres, Postgres only, and no cache at all without the worker
knowing which.
"""

from __future__ import annotations

import redis
from sqlalchemy import Engine

from chess_coach.cache.codec import CorruptValue
from chess_coach.cache.position_cache import (
    DEFAULT_KEY_PREFIX,
    CacheStats,
    PositionCache,
    redis_key,
)
from chess_coach.config import Settings, get_settings
from chess_coach.positions import EvalCache, NullCache

__all__ = [
    "DEFAULT_KEY_PREFIX",
    "CacheStats",
    "CorruptValue",
    "PositionCache",
    "build_eval_cache",
    "cache_redis_client",
    "redis_key",
]


def cache_redis_client(settings: Settings) -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def build_eval_cache(settings: Settings | None = None, engine: Engine | None = None) -> EvalCache:
    settings = settings or get_settings()
    if settings.position_cache == "off":
        return NullCache()

    if engine is None:
        from chess_coach.db import get_engine

        engine = get_engine()
    client = cache_redis_client(settings) if settings.position_cache == "full" else None
    return PositionCache(
        engine,
        client,
        key_prefix=settings.position_cache_key_prefix,
        ttl_sec=settings.position_cache_ttl_sec,
    )
