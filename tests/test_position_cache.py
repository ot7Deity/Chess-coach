"""The position cache against a real Postgres and a real Redis.

The properties that matter are the ones a mock cannot show: which layer answers,
that the composite key separates searches in both layers, and that a lost race
leaves both layers agreeing with the winner.
"""

from __future__ import annotations

import random
import re
import socket
import threading
from collections.abc import Iterator

import chess
import pytest
import redis
from sqlalchemy import Engine, event, select, text

from chess_coach.cache import PositionCache, build_eval_cache, cache_redis_client, redis_key
from chess_coach.cache.codec import decode, encode
from chess_coach.config import Settings
from chess_coach.models import Position
from chess_coach.positions import EvalCache, Evaluation, NullCache, fen_hash, normalize_fen

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

ENGINE = "sf_18"
DEPTH = 16
EVAL = Evaluation(eval_cp=31, mate_in=None, best_move="e7e5", nodes=1000, compute_ms=250)
OTHER = Evaluation(eval_cp=-12, mate_in=None, best_move="c7c5", nodes=900, compute_ms=240)


def position(*sans: str) -> tuple[bytes, str]:
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return fen_hash(board), normalize_fen(board)


def opening_positions(count: int) -> list[tuple[bytes, str]]:
    """`count` distinct positions from a fixed random walk."""
    rng = random.Random(7)
    board = chess.Board()
    out: dict[bytes, str] = {}
    while len(out) < count:
        if board.is_game_over():
            board = chess.Board()
        board.push(rng.choice(list(board.legal_moves)))
        out[fen_hash(board)] = normalize_fen(board)
    return list(out.items())


class CountingRedis:
    """A real client that counts pipelines (Redis round trips) and can be told
    to fail the way an unreachable Redis does."""

    def __init__(self, real: redis.Redis) -> None:
        self.real = real
        self.pipelines = 0
        self.down = False

    def pipeline(self, transaction: bool = True):
        self.pipelines += 1
        pipe = self.real.pipeline(transaction=transaction)
        if self.down:

            def fail(*args, **kwargs):
                raise redis.ConnectionError("simulated outage")

            pipe.execute = fail
        return pipe

    def __getattr__(self, name):
        return getattr(self.real, name)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def glob_safe_prefix(redis_client) -> Iterator[None]:
    """Give `redis_client` a key prefix with no glob metacharacters.

    conftest's cleanup deletes `SCAN MATCH <prefix>*` with the prefix
    unescaped, and the prefix contains the test name - which for a
    parametrized test has `[...]` in it. Redis reads that as a character
    class, the pattern matches nothing, and every key leaks. This swaps in a
    sanitised prefix and cleans it up here.
    """
    safe = re.sub(r"[\[\]*?\\]", "_", redis_client.key_prefix)
    redis_client.key_prefix = safe
    yield
    keys = list(redis_client.scan_iter(match=safe + "*"))
    if keys:
        redis_client.delete(*keys)


@pytest.fixture
def make_cache(clean_engine: Engine, redis_client):
    def make(client=redis_client, **kwargs) -> PositionCache:
        kwargs.setdefault("key_prefix", redis_client.key_prefix)
        kwargs.setdefault("ttl_sec", 3600)
        return PositionCache(clean_engine, client, **kwargs)

    return make


@pytest.fixture
def cache(make_cache) -> PositionCache:
    return make_cache()


@pytest.fixture
def pg_selects(clean_engine: Engine) -> Iterator[list[str]]:
    """Every SELECT against `positions`, to count round trips."""
    seen: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "positions" in statement:
            seen.append(statement)

    event.listen(clean_engine, "before_cursor_execute", listener)
    yield seen
    event.remove(clean_engine, "before_cursor_execute", listener)


def rkey(redis_client, key: bytes, engine: str = ENGINE, depth: int = DEPTH) -> str:
    return redis_key(redis_client.key_prefix, key, engine, depth)


def pg_row(engine: Engine, key: bytes, depth: int = DEPTH):
    with engine.connect() as conn:
        return conn.execute(
            select(Position).where(
                Position.fen_hash == key,
                Position.engine_version == ENGINE,
                Position.depth == depth,
            )
        ).one_or_none()


# --- the basic path ---------------------------------------------------------


def test_it_satisfies_the_contract(cache):
    typed: EvalCache = cache  # the worker only ever sees this type
    key, fen = position("e4")
    assert typed.get(key, ENGINE, DEPTH) is None
    typed.put(key, fen, ENGINE, DEPTH, EVAL)
    assert typed.get(key, ENGINE, DEPTH) == EVAL


def test_a_miss_is_counted_and_returns_none(cache):
    key, _ = position("e4")
    assert cache.get(key, ENGINE, DEPTH) is None
    assert cache.stats.misses == 1
    assert cache.stats.hit_rate == 0.0


def test_put_writes_both_layers_and_the_next_get_is_a_redis_hit(cache, redis_client, clean_engine):
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)

    row = pg_row(clean_engine, key)
    assert (row.fen, row.eval_cp, row.mate_in, row.compute_ms) == (fen, 31, None, 250)
    assert decode(redis_client.get(rkey(redis_client, key)), ENGINE, DEPTH) == EVAL

    assert cache.get(key, ENGINE, DEPTH) == EVAL
    stats = cache.stats
    assert (stats.redis_hits, stats.postgres_hits, stats.misses) == (1, 0, 0)
    assert stats.engine_ms_saved == 250
    assert stats.writes_inserted == 1


def test_a_redis_miss_falls_through_to_postgres_and_backfills(cache, redis_client):
    key, fen = position("d4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    redis_client.delete(rkey(redis_client, key))  # expired or evicted

    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert redis_client.exists(rkey(redis_client, key))
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    stats = cache.stats
    assert (stats.postgres_hits, stats.redis_hits) == (1, 1)
    assert stats.engine_ms_saved == 500


@pytest.mark.parametrize(("engine", "depth"), [("sf_18", 12), ("sf_18", 20), ("sf_17", 16)])
def test_another_engine_or_depth_is_a_miss_in_both_layers(cache, engine, depth):
    """The bug the composite key exists to prevent: a depth-16 result must never
    answer a depth-12 question, from Redis or from Postgres."""
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    assert cache.get(key, engine, depth) is None

    cache.put(key, fen, engine, depth, OTHER)
    assert cache.get(key, engine, depth) == OTHER
    assert cache.get(key, ENGINE, DEPTH) == EVAL


def test_hits_without_compute_ms_are_counted_separately(cache):
    key, fen = position("c4")
    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=5, mate_in=None, best_move="e7e5"))
    cache.get(key, ENGINE, DEPTH)
    assert cache.stats.engine_ms_saved == 0
    assert cache.stats.hits_without_compute_ms == 1


def test_a_mate_round_trips_through_both_layers(cache, redis_client):
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    key, fen = fen_hash(board), normalize_fen(board)
    mate = Evaluation(eval_cp=None, mate_in=1, best_move="a1a8", compute_ms=3)
    cache.put(key, fen, ENGINE, DEPTH, mate)
    assert cache.get(key, ENGINE, DEPTH) == mate
    redis_client.delete(rkey(redis_client, key))
    assert cache.get(key, ENGINE, DEPTH) == mate


def test_postgres_only_mode(make_cache):
    cache = make_cache(None)
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert cache.stats.postgres_hits == 1
    assert not cache.redis_active


# --- input checking -----------------------------------------------------------


def test_put_refuses_a_fen_that_is_not_the_keys_normalised_fen(cache):
    board = chess.Board()
    board.push_san("e4")
    key = fen_hash(board)
    with pytest.raises(ValueError, match="normalize_fen"):
        cache.put(key, board.fen(), ENGINE, DEPTH, EVAL)  # move counters included
    _, other_fen = position("d4")
    with pytest.raises(ValueError, match="normalize_fen"):
        cache.put(key, other_fen, ENGINE, DEPTH, EVAL)
    with pytest.raises(ValueError, match="not a FEN"):
        cache.put(key, "garbage", ENGINE, DEPTH, EVAL)


@pytest.mark.parametrize(
    ("key", "engine", "depth"),
    [
        (b"short", ENGINE, DEPTH),
        (b"x" * 32, "", DEPTH),
        (b"x" * 32, "v" * 33, DEPTH),
        (b"x" * 32, ENGINE, 0),
        (b"x" * 32, ENGINE, True),
    ],
)
def test_get_refuses_malformed_keys(cache, key, engine, depth):
    with pytest.raises(ValueError):
        cache.get(key, engine, depth)


# --- first write wins -------------------------------------------------------


def test_the_first_write_wins_and_the_second_is_a_no_op(cache, redis_client, clean_engine):
    key, fen = position("e4", "e5")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    cache.put(key, fen, ENGINE, DEPTH, OTHER)

    assert pg_row(clean_engine, key).eval_cp == EVAL.eval_cp
    assert decode(redis_client.get(rkey(redis_client, key)), ENGINE, DEPTH) == EVAL
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert (cache.stats.writes_inserted, cache.stats.writes_existing) == (1, 1)


def test_a_losing_write_never_reaches_redis(cache, redis_client):
    """If the loser wrote Redis, Redis would disagree with Postgres. Here the
    winner's Redis copy is gone (expired, say); the loser must not fill the gap
    with its own value."""
    key, fen = position("e4", "c5")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    redis_client.delete(rkey(redis_client, key))

    cache.put(key, fen, ENGINE, DEPTH, OTHER)
    assert not redis_client.exists(rkey(redis_client, key))
    assert cache.get(key, ENGINE, DEPTH) == EVAL  # backfilled from the winner


def test_concurrent_writers_leave_one_row_and_redis_agrees_with_it(
    make_cache, redis_client, clean_engine
):
    """Eight workers race to write the same 25 positions, each with its own
    (different) evaluation. Exactly one write per position wins, and Redis holds
    the winner's value - not whichever writer happened to touch Redis last."""
    positions = opening_positions(25)
    writers = 8
    caches = [make_cache() for _ in range(writers)]
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        mine = Evaluation(eval_cp=i, mate_in=None, best_move="e2e4", compute_ms=10 + i)
        order = positions[:]
        random.Random(i).shuffle(order)
        try:
            barrier.wait()
            for key, fen in order:
                caches[i].put(key, fen, ENGINE, DEPTH, mine)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with clean_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM positions")).scalar_one()
    assert count == len(positions)
    for key, _ in positions:
        winner = pg_row(clean_engine, key)
        cached = decode(redis_client.get(rkey(redis_client, key)), ENGINE, DEPTH)
        assert (cached.eval_cp, cached.compute_ms) == (winner.eval_cp, winner.compute_ms)

    inserted = sum(c.stats.writes_inserted for c in caches)
    existing = sum(c.stats.writes_existing for c in caches)
    assert (inserted, existing) == (len(positions), len(positions) * (writers - 1))


# --- bad data in Redis ------------------------------------------------------------


def test_a_corrupt_redis_value_is_a_miss_then_repaired_from_postgres(cache, redis_client):
    key, fen = position("Nf3")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    redis_client.set(rkey(redis_client, key), b"\x00garbage")

    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert cache.stats.corrupt_values == 1
    assert cache.stats.postgres_hits == 1
    assert decode(redis_client.get(rkey(redis_client, key)), ENGINE, DEPTH) == EVAL


def test_a_corrupt_value_with_nothing_in_postgres_is_just_a_miss(cache, redis_client):
    key, fen = position("g3")
    redis_client.set(rkey(redis_client, key), b"[1,")
    assert cache.get(key, ENGINE, DEPTH) is None
    cache.put(key, fen, ENGINE, DEPTH, EVAL)  # the worker computes and writes
    assert decode(redis_client.get(rkey(redis_client, key)), ENGINE, DEPTH) == EVAL


def test_a_value_for_another_depth_under_this_key_is_not_served(cache, redis_client):
    """Simulates a key-building bug: a depth-12 value sitting under the depth-16
    key. The value's own (engine, depth) disagrees, so it is refused."""
    key, _ = position("b3")
    redis_client.set(rkey(redis_client, key), encode(ENGINE, 12, EVAL))
    assert cache.get(key, ENGINE, DEPTH) is None
    assert cache.stats.corrupt_values == 1


def test_a_key_of_the_wrong_redis_type_is_a_miss(cache, redis_client):
    key, fen = position("f4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    name = rkey(redis_client, key)
    redis_client.delete(name)
    redis_client.hset(name, "x", "1")  # WRONGTYPE for GETEX

    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert cache.stats.corrupt_values == 1
    assert decode(redis_client.get(name), ENGINE, DEPTH) == EVAL  # SET replaced it


# --- Redis unavailable -----------------------------------------------------------


def test_redis_down_degrades_to_postgres_and_the_breaker_stops_retrying(make_cache, redis_client):
    switch = CountingRedis(redis_client)
    clock = FakeClock()
    cache = make_cache(switch, redis_retry_sec=30, clock=clock)
    key, fen = position("e4")
    other_key, other_fen = position("d4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)

    switch.down = True
    assert cache.get(key, ENGINE, DEPTH) == EVAL  # from Postgres
    assert not cache.redis_active
    assert cache.stats.redis_errors == 1

    calls = switch.pipelines
    cache.put(other_key, other_fen, ENGINE, DEPTH, OTHER)  # Postgres only
    assert cache.get(other_key, ENGINE, DEPTH) == OTHER
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert switch.pipelines == calls, "breaker open: Redis must not be touched"
    assert cache.stats.redis_errors == 1

    switch.down = False
    clock.now += 31
    assert cache.get(other_key, ENGINE, DEPTH) == OTHER  # Redis miss, PG hit, backfill
    assert cache.redis_active
    assert redis_client.exists(rkey(redis_client, other_key))
    assert cache.get(other_key, ENGINE, DEPTH) == OTHER
    assert cache.stats.redis_hits == 1


def test_a_put_while_redis_is_down_still_commits_to_postgres(make_cache, redis_client):
    switch = CountingRedis(redis_client)
    cache = make_cache(switch, clock=FakeClock())
    key, fen = position("e4")
    switch.down = True
    cache.put(key, fen, ENGINE, DEPTH, EVAL)  # must not raise
    assert cache.stats.writes_inserted == 1
    assert not redis_client.exists(rkey(redis_client, key))
    assert make_cache(None).get(key, ENGINE, DEPTH) == EVAL


def test_a_failed_backfill_trips_the_breaker_but_still_returns(make_cache, redis_client):
    switch = CountingRedis(redis_client)
    cache = make_cache(switch, clock=FakeClock())
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    redis_client.delete(rkey(redis_client, key))

    real_pipeline = switch.pipeline

    def fail_on_writes(transaction=True):
        pipe = real_pipeline(transaction)
        original = pipe.execute

        def execute(*args, **kwargs):
            if any(cmd[0][0] == "SET" for cmd in pipe.command_stack):
                raise redis.TimeoutError("simulated slow Redis")
            return original(*args, **kwargs)

        pipe.execute = execute
        return pipe

    switch.pipeline = fail_on_writes
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert not cache.redis_active


def test_an_unreachable_redis_server_is_survivable(make_cache):
    """A real client against a port nothing listens on, so the exception types
    are whatever redis-py actually raises, not what a fake chose to raise."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    dead = cache_redis_client(
        Settings(redis_url=f"redis://127.0.0.1:{port}/0", position_cache_redis_timeout_sec=0.2)
    )
    cache = make_cache(dead, key_prefix="unused:")
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert cache.stats.postgres_hits == 1
    assert cache.stats.redis_errors == 1  # the put's write tripped it; the get skipped Redis
    dead.close()


# --- batches ---------------------------------------------------------------------


def test_get_many_is_three_round_trips_for_a_whole_game(
    make_cache, redis_client, clean_engine, pg_selects
):
    positions = opening_positions(60)
    counting = CountingRedis(redis_client)
    cache = make_cache(counting)
    in_redis = positions[:20]
    in_postgres_only = positions[20:40]
    for key, fen in in_redis + in_postgres_only:
        cache.put(key, fen, ENGINE, DEPTH, EVAL)
    for key, _ in in_postgres_only:
        redis_client.delete(rkey(redis_client, key))

    counting.pipelines = 0
    pg_selects.clear()
    keys = [k for k, _ in positions]
    found = cache.get_many(keys + keys[:5], ENGINE, DEPTH)  # with repeats

    assert set(found) == set(keys[:40])
    assert counting.pipelines == 2  # one read, one backfill
    assert len(pg_selects) == 1
    stats = cache.stats
    assert (stats.redis_hits, stats.postgres_hits, stats.misses) == (20, 20, 20)
    assert stats.lookups == 60  # repeats inside a batch count once


def test_get_many_with_everything_in_redis_never_touches_postgres(cache, pg_selects):
    positions = opening_positions(10)
    for key, fen in positions:
        cache.put(key, fen, ENGINE, DEPTH, EVAL)
    pg_selects.clear()
    assert len(cache.get_many([k for k, _ in positions], ENGINE, DEPTH)) == 10
    assert pg_selects == []


def test_a_huge_batch_is_chunked_rather_than_one_giant_in_list(make_cache, pg_selects):
    cache = make_cache(None)
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    keys = [random.Random(i).randbytes(32) for i in range(2499)] + [key]
    pg_selects.clear()
    assert cache.get_many(keys, ENGINE, DEPTH) == {key: EVAL}
    assert len(pg_selects) == 3


def test_get_many_of_nothing_does_no_io(make_cache, redis_client, pg_selects):
    counting = CountingRedis(redis_client)
    cache = make_cache(counting)
    assert cache.get_many([], ENGINE, DEPTH) == {}
    assert counting.pipelines == 0
    assert pg_selects == []


# --- expiry ------------------------------------------------------------------------


def test_every_hit_refreshes_the_ttl(make_cache, redis_client):
    cache = make_cache(ttl_sec=1000)
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    name = rkey(redis_client, key)
    assert 990 <= redis_client.ttl(name) <= 1000

    redis_client.expire(name, 5)
    cache.get(key, ENGINE, DEPTH)
    assert redis_client.ttl(name) > 990


def test_backfilled_entries_get_a_ttl(make_cache, redis_client):
    cache = make_cache(ttl_sec=1000)
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    redis_client.delete(rkey(redis_client, key))
    cache.get(key, ENGINE, DEPTH)
    assert redis_client.ttl(rkey(redis_client, key)) > 990


def test_ttl_zero_means_no_expiry(make_cache, redis_client):
    cache = make_cache(ttl_sec=0)
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert redis_client.ttl(rkey(redis_client, key)) == -1


# --- maintenance and wiring --------------------------------------------------------


def test_purge_redis_is_scoped_to_one_search(cache, redis_client):
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    cache.put(key, fen, ENGINE, 12, EVAL)
    cache.put(key, fen, "sf_17", DEPTH, EVAL)
    sibling = redis_client.key_prefix.rstrip(":") + "-sibling:x"
    redis_client.set(sibling, b"keep")

    assert cache.purge_redis(ENGINE, 12) == 1
    assert cache.purge_redis(ENGINE) == 1
    assert redis_client.exists(rkey(redis_client, key, "sf_17", DEPTH))
    assert cache.purge_redis() == 1
    assert redis_client.exists(sibling)
    redis_client.delete(sibling)

    # Postgres still has all three, and Redis refills from it.
    assert cache.get(key, ENGINE, 12) == EVAL
    assert cache.stats.postgres_hits == 1


def test_build_eval_cache_follows_the_setting(clean_engine, redis_url):
    assert isinstance(build_eval_cache(Settings(position_cache="off"), clean_engine), NullCache)

    pg_only = build_eval_cache(Settings(position_cache="postgres"), clean_engine)
    assert isinstance(pg_only, PositionCache)
    assert not pg_only.redis_active

    full = build_eval_cache(Settings(position_cache="full", redis_url=redis_url), clean_engine)
    assert isinstance(full, PositionCache)
    assert full.redis_active

    with pytest.raises(ValueError):
        Settings(position_cache="redis")
