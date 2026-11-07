"""The Stockfish wrapper: reproducible answers, the right point of view, and
recovery from an engine that hangs or dies.

The hang and crash tests drive a fake UCI engine (a tiny Python script), so
they run without a Stockfish binary. The rest are `engine` tests.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import chess
import pytest

from chess_coach.engine import (
    EngineCrashed,
    EngineError,
    EngineTimeout,
    StockfishEngine,
    engine_version,
    engine_version_from_name,
)
from chess_coach.positions import fen_hash

DEPTH = 10

# --- version string (pure) --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("Stockfish 18", "sf_18"),
        ("Stockfish 17.1", "sf_17.1"),
        ("Stockfish dev-20250101-abcdef12", "sf_dev-20250101-abcdef12"),
    ],
)
def test_engine_version_comes_from_the_uci_name(name, version):
    assert engine_version_from_name(name) == version


@pytest.mark.parametrize(("hash_mb", "version"), [(16, "sf_18-h16"), (128, "sf_18-h128")])
def test_the_cache_version_names_the_hash_size(hash_mb, version):
    """Regression for the sixth "the value depends on something the key does
    not name": 16 MB and 128 MB gave different scores for one position in six
    (by 2 cp at depth 16), so evaluations at different hash sizes must never be
    filed under the same version."""
    assert engine_version("Stockfish 18", hash_mb=hash_mb) == version


def test_more_than_one_search_thread_is_refused(tmp_path: Path):
    """Not merely discouraged: no cache key can name thread timing."""
    with pytest.raises(ValueError, match="threads must be 1"):
        StockfishEngine(_fake(tmp_path, "ok"), depth=DEPTH, threads=2)


def test_an_unknown_engine_is_refused_rather_than_given_a_made_up_version():
    """The version is part of the cache key. Guessing one for an engine we do
    not recognise would file its evaluations under someone else's name."""
    with pytest.raises(EngineError):
        engine_version_from_name("Komodo 14")


# --- hung and crashed engines (fake engine, no binary needed) ---------------

_FAKE_ENGINE = """
import pathlib, sys, time
mode, counter = sys.argv[1], pathlib.Path(sys.argv[2])
# Each process takes a number, so "only the first process misbehaves" works
# across restarts.
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
for line in sys.stdin:
    cmd = line.strip()
    if cmd == "uci":
        print("id name Stockfish 18")
        print("option name Threads type spin default 1 min 1 max 512")
        print("option name Hash type spin default 16 min 1 max 33554432")
        print("uciok", flush=True)
    elif cmd == "isready":
        print("readyok", flush=True)
    elif cmd.startswith("go"):
        if n == 0 and mode == "hang":
            time.sleep(3600)
        if n == 0 and mode == "crash":
            sys.exit(3)
        print("info depth 10 score cp 12 nodes 34 pv e2e4", flush=True)
        print("bestmove e2e4", flush=True)
    elif cmd == "quit":
        break
"""


def _fake(tmp_path: Path, mode: str) -> list[str]:
    script = tmp_path / "fake_engine.py"
    script.write_text(_FAKE_ENGINE, encoding="utf-8")
    return [sys.executable, str(script), mode, str(tmp_path / "starts")]


def test_a_hung_search_is_killed_restarted_and_reported(tmp_path: Path):
    """SimpleEngine.analyse() has no timeout for a depth-limited search - only
    for time-limited ones - so without our own bound, a hung engine would hang
    the worker, and its heartbeat with it, until the lease expired."""
    threads_before = threading.active_count()
    with StockfishEngine(_fake(tmp_path, "hang"), depth=DEPTH, timeout_sec=1.0) as engine:
        started = time.monotonic()
        with pytest.raises(EngineTimeout):
            engine.evaluate(chess.Board())
        assert time.monotonic() - started < 10
        assert engine.restarts == 1

        # The replacement process answers normally.
        assert engine.evaluate(chess.Board()).eval_cp == 12
    assert (tmp_path / "starts").read_text() == "2"

    # Regression: the first version cancelled the in-flight search before
    # killing the process. python-chess then never saw the process exit, and
    # each timeout leaked a non-daemon event-loop thread that hung the
    # interpreter at shutdown - this test file hung pytest about one run in
    # four. Every engine thread must be gone once the engine is closed.
    deadline = time.monotonic() + 5
    while threading.active_count() > threads_before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() == threads_before


def test_a_crashed_engine_is_restarted_and_reported(tmp_path: Path):
    with StockfishEngine(_fake(tmp_path, "crash"), depth=DEPTH, timeout_sec=5.0) as engine:
        with pytest.raises(EngineCrashed):
            engine.evaluate(chess.Board())
        assert engine.restarts == 1
        assert engine.evaluate(chess.Board()).best_move == "e2e4"


def test_positions_without_a_legal_move_never_reach_the_engine(tmp_path: Path):
    """Checkmate and stalemate are known without searching. The fake engine
    would answer 'cp 12' for anything it was asked, so a wrong answer here
    means the position was sent to it."""
    with StockfishEngine(_fake(tmp_path, "ok"), depth=DEPTH) as engine:
        mated = engine.evaluate(chess.Board("R5k1/5ppp/8/8/8/8/8/7K b - - 1 1"))
        stalemated = engine.evaluate(chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"))
    assert (mated.eval_cp, mated.mate_in, mated.best_move) == (None, 0, None)
    assert (stalemated.eval_cp, stalemated.mate_in) == (0, None)


# --- the real engine --------------------------------------------------------


@pytest.fixture(scope="module")
def stockfish(stockfish_path: Path):
    with StockfishEngine(stockfish_path, depth=DEPTH, hash_mb=16) as engine:
        yield engine


@pytest.mark.engine
def test_the_pinned_engine_reports_sf_18_and_its_hash_size(stockfish: StockfishEngine):
    """The release must match tools/stockfish.lock.json's tag and must not
    mention the SIMD build; the cache key carries the hash size as well."""
    assert stockfish.release == "sf_18"
    assert stockfish.version == "sf_18-h16"


@pytest.mark.engine
def test_scores_are_from_the_side_to_moves_point_of_view(stockfish: StockfishEngine):
    """White is a queen up. Asked with black to move, the score is negative -
    the `Evaluation` contract. Converting to the mover's view is the analysis
    code's job, not the engine's."""
    fen = "4k3/8/8/8/8/8/3Q4/4K3 {} - - 0 1"
    white_to_move = stockfish.evaluate(chess.Board(fen.format("w")))
    black_to_move = stockfish.evaluate(chess.Board(fen.format("b")))
    assert white_to_move.mate_in is not None or white_to_move.eval_cp > 500
    assert black_to_move.mate_in is not None or black_to_move.eval_cp < -500
    if black_to_move.mate_in is not None:
        assert black_to_move.mate_in < 0


@pytest.mark.engine
def test_a_mate_is_a_mate_distance_not_a_big_number(stockfish: StockfishEngine):
    ev = stockfish.evaluate(chess.Board("6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"))
    assert (ev.eval_cp, ev.mate_in, ev.best_move) == (None, 1, "a1a8")


@pytest.mark.engine
def test_the_same_position_gets_the_same_answer_whatever_came_before(
    stockfish: StockfishEngine,
):
    """The regression test for the reason the hash is cleared per position.
    Without it, the transposition table carries earlier searches into later
    ones: on sf_18 the same position measured +264, +266 and +269 at depth 16
    depending on what had been searched before it. A cache keyed on (position,
    engine, depth) would then be storing one sample of a distribution."""
    board = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4")
    first = stockfish.evaluate(board)
    for other in (
        chess.Board(),
        chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"),
    ):
        stockfish.evaluate(other)
    again = stockfish.evaluate(board)
    assert (again.eval_cp, again.mate_in, again.best_move, again.nodes) == (
        first.eval_cp,
        first.mate_in,
        first.best_move,
        first.nodes,
    )


@pytest.mark.engine
def test_the_engine_sees_only_what_the_cache_key_contains(stockfish: StockfishEngine):
    """The cache key ignores move history and the move counters, so the
    evaluation must too: the same position reached by moves, or set up from a
    FEN with a fifty-move counter of 90, gets the identical answer."""
    by_moves = chess.Board()
    for san in ("e4", "e5", "Nf3", "Nc6", "Ng1", "Nb8"):
        by_moves.push_san(san)
    from_fen = chess.Board(by_moves.fen().replace(" 4 4", " 90 60"))
    a, b = stockfish.evaluate(by_moves), stockfish.evaluate(from_fen)
    assert (a.eval_cp, a.mate_in, a.best_move, a.nodes) == (
        b.eval_cp,
        b.mate_in,
        b.best_move,
        b.nodes,
    )


def _same(a, b) -> bool:
    """Equal in everything the cache stores except wall-clock time."""
    return (a.eval_cp, a.mate_in, a.best_move, a.nodes) == (
        b.eval_cp,
        b.mate_in,
        b.best_move,
        b.nodes,
    )


@pytest.mark.engine
def test_one_position_reached_by_two_games_gets_one_answer(stockfish: StockfishEngine):
    """A transposition: the same position via different move orders, so with
    different move histories, different halfmove clocks (0 and 2) and, in one
    of them, a stale en-passant square. All three are outside fen_hash, so
    none of them may change the evaluation filed under it."""
    a, b = chess.Board(), chess.Board()
    for san in ("Nf3", "Nf6", "d4", "d5"):
        a.push_san(san)
    for san in ("d4", "d5", "Nf3", "Nf6"):
        b.push_san(san)
    assert a.halfmove_clock != b.halfmove_clock
    assert fen_hash(a) == fen_hash(b)

    first = stockfish.evaluate(a)
    stockfish.evaluate(chess.Board("4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1"))  # disturb any state
    assert _same(first, stockfish.evaluate(b))
