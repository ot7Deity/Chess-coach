"""Try to break the M3 position cache (Redis in front of Postgres).

Skips on branches without chess_coach.cache.position_cache. M3's own suite
already covers wrong engine/depth, corrupt values and concurrent puts well;
these go after what it does not: Redis being *down*, Postgres and Redis
disagreeing, and the value-must-match-the-key rule from positions.py.
"""

from __future__ import annotations

import threading

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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: position_cache.py:320-326 and 350-357 call pipe.execute() with "
        "raise_on_error=False, which only swallows per-command errors; a "
        "connection failure still raises, so a Redis outage fails every lookup "
        "instead of falling back to Postgres. _redis_usable() only checks for None."
    ),
)
def test_a_redis_outage_falls_back_to_postgres_on_get(clean_engine, redis_client, dead_redis):
    key, fen = _position("e4")
    _cache(clean_engine, None, redis_client.key_prefix).put(
        key, fen, ENGINE, DEPTH, Evaluation(eval_cp=30, mate_in=None, best_move="e7e5")
    )
    hit = _cache(clean_engine, dead_redis, redis_client.key_prefix).get(key, ENGINE, DEPTH)
    assert hit is not None and hit.eval_cp == 30


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: put() commits to Postgres, then _redis_set raises ConnectionError "
        "when Redis is down - the caller sees a failed put for a value that was "
        "in fact stored, and the analysis job fails."
    ),
)
def test_a_redis_outage_does_not_fail_a_put(clean_engine, redis_client, dead_redis):
    key, fen = _position("d4")
    cache = _cache(clean_engine, dead_redis, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=20, mate_in=None, best_move="d7d5"))
    with clean_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM positions")) == 1


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DISAGREEMENT: the m1/6 contract (positions.py EvalCache) says a put that "
        "disagrees with the stored value means the reproducibility rule was broken "
        "and 'should be reported, not silently resolved'. put() only counts it as "
        "writes_existing; nothing distinguishes a duplicate from a contradiction."
    ),
)
def test_a_put_that_contradicts_the_stored_value_is_reported(clean_engine, redis_client):
    key, fen = _position("Nf3")
    cache = _cache(clean_engine, redis_client, redis_client.key_prefix)
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=25, mate_in=None, best_move="d7d5"))
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=-400, mate_in=None, best_move="a7a6"))
    assert any("conflict" in k or "contradict" in k for k in cache.stats.as_dict())


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
