"""Try to break the M3 position cache (Redis in front of Postgres).

Skips on branches without chess_coach.cache.position_cache. M3's own suite
already covers wrong engine/depth, corrupt values and concurrent puts well;
these go after what it does not: Redis being *down*, Postgres and Redis
disagreeing, and the value-must-match-the-key rule from positions.py.
"""

from __future__ import annotations

import threading
import time

import chess
import pytest
import redis
from sqlalchemy import Engine, text

from chess_coach.positions import Evaluation, fen_hash, normalize_fen

pc = pytest.importorskip("chess_coach.cache.position_cache")

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

ENGINE, DEPTH = "sf_18", 16


def _position(*sans: str) -> tuple[bytes, str]:
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return fen_hash(board), normalize_fen(board)


def _cache(engine: Engine, client: redis.Redis | None, prefix: str):
    return pc.PositionCache(engine, client, key_prefix=prefix)


@pytest.fixture
def dead_redis():
    """A client for a port nothing listens on: Redis is down."""
    client = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    yield client
    client.close()


# --- Redis down ---------------------------------------------------------------


def test_a_redis_outage_falls_back_to_postgres_on_get(clean_engine, redis_client, dead_redis):
    key, fen = _position("e4")
    _cache(clean_engine, None, redis_client.key_prefix).put(
        key, fen, ENGINE, DEPTH, Evaluation(eval_cp=30, mate_in=None, best_move="e7e5")
    )
    hit = _cache(clean_engine, dead_redis, redis_client.key_prefix).get(key, ENGINE, DEPTH)
    assert hit is not None and hit.eval_cp == 30


def test_a_redis_outage_does_not_fail_a_put(clean_engine, redis_client, dead_redis):
    key, fen = _position("d4")
    cache = _cache(clean_engine, dead_redis, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=20, mate_in=None, best_move="d7d5"))
    with clean_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM positions")) == 1


def test_an_outage_costs_one_timeout_per_retry_window_not_one_per_lookup(
    clean_engine, redis_client, dead_redis
):
    keys = [_position(*m) for m in (["e4"], ["d4"], ["c4"], ["Nf3"], ["g3"], ["b3"])]
    pg = _cache(clean_engine, None, redis_client.key_prefix)
    for key, fen in keys:
        pg.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=1, mate_in=None, best_move="a7a6"))

    cache = pc.PositionCache(
        clean_engine, dead_redis, key_prefix=redis_client.key_prefix, redis_retry_sec=60
    )
    key0 = keys[0][0]
    assert cache.get(key0, ENGINE, DEPTH) is not None  # pays the one timeout, trips the breaker
    started = time.monotonic()
    for _ in range(5):
        for key, _fen in keys:
            assert cache.get(key, ENGINE, DEPTH) is not None
    elapsed = time.monotonic() - started
    # Inside the retry window Redis is not touched at all.
    assert elapsed < 1.0, elapsed
    assert cache.stats.redis_errors == 1
    assert cache.stats.postgres_hits == 31


def test_the_production_redis_client_fails_fast(dead_redis):
    """The breaker only bounds how *often* an outage is paid for; the client
    decides how much each payment costs. A default redis-py client retries a
    failed connect (measured here: 6.5 s per call against a dead port with a
    0.2 s timeout). cache_redis_client must not."""
    from chess_coach.cache import cache_redis_client

    from chess_coach.config import Settings

    settings = Settings(
        _env_file=None, redis_url="redis://127.0.0.1:1/0", position_cache_redis_timeout_sec=0.2
    )
    client = cache_redis_client(settings)
    started = time.monotonic()
    with pytest.raises(redis.RedisError):
        client.ping()
    assert time.monotonic() - started < 1.5


# --- Postgres and Redis disagreeing -----------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DISAGREEMENT: position_cache.py:5-9 says Redis 'can only disagree by "
        "lacking an entry - never by holding a different one'. Any reset of "
        "positions (TRUNCATE, a restored backup, a test DB sharing the default "
        "prefix on the same Redis db) leaves Redis serving hits Postgres does not "
        "have; the worker then never writes the row. The key prefix does not name "
        "the database."
    ),
)
def test_a_hit_is_always_backed_by_a_postgres_row(clean_engine, redis_client):
    key, fen = _position("c4")
    cache = _cache(clean_engine, redis_client, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=10, mate_in=None, best_move="e7e5"))
    with clean_engine.begin() as c:
        c.execute(text("TRUNCATE positions"))
    hit = cache.get(key, ENGINE, DEPTH)
    with clean_engine.connect() as c:
        rows = c.scalar(text("SELECT count(*) FROM positions"))
    assert hit is None or rows == 1


def test_a_put_that_contradicts_the_stored_value_is_reported(clean_engine, redis_client):
    key, fen = _position("Nf3")
    cache = _cache(clean_engine, redis_client, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=25, mate_in=None, best_move="d7d5"))
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=-400, mate_in=None, best_move="a7a6"))
    # A counter that exists is not enough: it has to have counted this one.
    assert getattr(cache.stats, "eval_conflicts", 0) == 1
    # And a put that agrees is not a conflict.
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=25, mate_in=None, best_move="d7d5"))
    assert cache.stats.eval_conflicts == 1


def test_concurrent_puts_of_one_key_agree_in_both_layers(clean_engine, redis_client):
    key, fen = _position("e4", "c5")
    caches = [_cache(clean_engine, redis_client, redis_client.key_prefix) for _ in range(8)]
    start = threading.Barrier(8)

    def put(i: int) -> None:
        start.wait()
        caches[i].put(
            key, fen, ENGINE, DEPTH, Evaluation(eval_cp=i, mate_in=None, best_move="g1f3")
        )

    threads = [threading.Thread(target=put, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    with clean_engine.connect() as c:
        stored = c.execute(text("SELECT eval_cp FROM positions")).scalars().all()
    assert len(stored) == 1
    fresh = _cache(clean_engine, redis_client, redis_client.key_prefix)
    assert fresh.get(key, ENGINE, DEPTH).eval_cp == stored[0]
    assert fresh.stats.redis_hits == 1  # served from Redis, and Redis agrees
    assert sum(c.stats.writes_inserted for c in caches) == 1


def test_mate_zero_survives_both_layers(clean_engine, redis_client):
    """0 is falsy: a `value or None` anywhere would turn checkmate into a miss."""
    board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    key, fen = fen_hash(board), normalize_fen(board)
    cache = _cache(clean_engine, redis_client, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=None, mate_in=0, best_move=None))
    from_redis = cache.get(key, ENGINE, DEPTH)
    pg_only = _cache(clean_engine, None, redis_client.key_prefix).get(key, ENGINE, DEPTH)
    for hit in (from_redis, pg_only):
        assert (hit.eval_cp, hit.mate_in, hit.best_move) == (None, 0, None)


def test_a_value_planted_under_the_right_key_for_another_engine_is_not_served(
    clean_engine, redis_client
):
    key, _ = _position("g3")
    cache = _cache(clean_engine, redis_client, redis_client.key_prefix)
    name = pc.redis_key(redis_client.key_prefix, key, ENGINE, DEPTH)
    redis_client.set(name, b'[1,"sf_17",16,999,null,"e7e5",null,null]')
    assert cache.get(key, ENGINE, DEPTH) is None
    assert cache.stats.corrupt_values == 1
