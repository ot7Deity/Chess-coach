"""The position cache against a real Postgres.

The properties that matter are the ones a mock cannot show: that the composite
key separates searches, and that a lost race leaves exactly the winner's row.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator

import chess
import pytest
from sqlalchemy import Engine, event, select, text

from chess_coach.cache import PositionCache
from chess_coach.models import Position
from chess_coach.positions import EvalCache, Evaluation, fen_hash, normalize_fen

pytestmark = [pytest.mark.postgres]

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


@pytest.fixture
def make_cache(clean_engine: Engine):
    def make(**kwargs) -> PositionCache:
        return PositionCache(clean_engine, **kwargs)

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


def test_put_writes_the_row_and_the_next_get_is_a_hit(cache, clean_engine):
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)

    row = pg_row(clean_engine, key)
    assert (row.fen, row.eval_cp, row.mate_in, row.compute_ms) == (fen, 31, None, 250)

    assert cache.get(key, ENGINE, DEPTH) == EVAL
    stats = cache.stats
    assert (stats.postgres_hits, stats.misses) == (1, 0)
    assert stats.engine_ms_saved == 250
    assert stats.writes_inserted == 1


@pytest.mark.parametrize(("engine", "depth"), [("sf_18", 12), ("sf_18", 20), ("sf_17", 16)])
def test_another_engine_or_depth_is_a_miss(cache, engine, depth):
    """The bug the composite key exists to prevent: a depth-16 result must never
    answer a depth-12 question."""
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


def test_a_mate_round_trips(cache):
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    key, fen = fen_hash(board), normalize_fen(board)
    mate = Evaluation(eval_cp=None, mate_in=1, best_move="a1a8", compute_ms=3)
    cache.put(key, fen, ENGINE, DEPTH, mate)
    assert cache.get(key, ENGINE, DEPTH) == mate


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


def test_the_first_write_wins_and_the_second_is_a_no_op(cache, clean_engine):
    key, fen = position("e4", "e5")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    cache.put(key, fen, ENGINE, DEPTH, OTHER)

    assert pg_row(clean_engine, key).eval_cp == EVAL.eval_cp
    assert cache.get(key, ENGINE, DEPTH) == EVAL
    assert (cache.stats.writes_inserted, cache.stats.writes_existing) == (1, 1)


def test_concurrent_writers_leave_exactly_one_row_each(make_cache, clean_engine):
    """Eight workers race to write the same 25 positions, each with its own
    (different) evaluation. Exactly one write per position wins, and every
    writer agrees on which: inserted + existing adds up with no errors."""
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
    inserted = sum(c.stats.writes_inserted for c in caches)
    existing = sum(c.stats.writes_existing for c in caches)
    assert (inserted, existing) == (len(positions), len(positions) * (writers - 1))


# --- batches ---------------------------------------------------------------------


def test_get_many_is_one_query_for_a_whole_game(cache, pg_selects):
    positions = opening_positions(60)
    for key, fen in positions[:40]:
        cache.put(key, fen, ENGINE, DEPTH, EVAL)

    pg_selects.clear()
    keys = [k for k, _ in positions]
    found = cache.get_many(keys + keys[:5], ENGINE, DEPTH)  # with repeats

    assert set(found) == set(keys[:40])
    assert len(pg_selects) == 1
    stats = cache.stats
    assert (stats.postgres_hits, stats.misses) == (40, 20)
    assert stats.lookups == 60  # repeats inside a batch count once


def test_a_huge_batch_is_chunked_rather_than_one_giant_in_list(cache, pg_selects):
    key, fen = position("e4")
    cache.put(key, fen, ENGINE, DEPTH, EVAL)
    keys = [random.Random(i).randbytes(32) for i in range(2499)] + [key]
    pg_selects.clear()
    assert cache.get_many(keys, ENGINE, DEPTH) == {key: EVAL}
    assert len(pg_selects) == 3


def test_get_many_of_nothing_does_no_io(cache, pg_selects):
    assert cache.get_many([], ENGINE, DEPTH) == {}
    assert pg_selects == []
