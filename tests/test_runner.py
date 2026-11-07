"""The analysis worker against real Postgres and a real (shallow) Stockfish."""

from __future__ import annotations

from pathlib import Path

import chess
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.analysis.jobs import JOB_TYPE, PRIORITY, dedupe_key, enqueue_pending_games
from chess_coach.analysis.runner import CachedEvaluator, Worker
from chess_coach.config import Settings
from chess_coach.engine import EngineCrashed, StockfishEngine
from chess_coach.positions import Evaluation
from tests.games import HANGS_A_BISHOP, LONG_GAME, SCHOLARS_MATE, insert_game

pytestmark = [pytest.mark.postgres, pytest.mark.engine]

DEPTH = 6


@pytest.fixture(scope="module")
def stockfish(stockfish_path: Path):
    with StockfishEngine(stockfish_path, depth=DEPTH, hash_mb=16) as engine:
        yield engine


def _settings(**kw) -> Settings:
    defaults = dict(
        _env_file=None,
        job_lock_timeout_sec=60,
        engine_timeout_sec=10,
        job_retry_base_sec=30,
        worker_poll_interval_sec=0.05,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _worker(engine: Engine, stockfish, name: str = "w1", **kw) -> Worker:
    return Worker(engine, stockfish, worker_id=name, settings=_settings(**kw))


def _enqueue(engine: Engine) -> int:
    with Session(engine) as s, s.begin():
        return enqueue_pending_games(s, max_attempts=5)


def _one(engine: Engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).one()


# --- enqueue ----------------------------------------------------------------


@pytest.mark.parametrize("time_class", sorted(PRIORITY))
def test_each_pending_game_gets_one_job_with_its_time_class_priority(
    clean_engine: Engine, time_class: str
):
    game_id = insert_game(clean_engine, ["e4"], time_class=time_class)
    assert _enqueue(clean_engine) == 1
    assert _enqueue(clean_engine) == 0, "enqueueing twice must not add a second job"
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.type, job.payload, job.dedupe_key) == (
        JOB_TYPE,
        {"game_id": game_id},
        dedupe_key(game_id),
    )
    assert job.priority == PRIORITY[time_class]


def test_rapid_and_blitz_are_claimed_before_bullet(clean_engine: Engine):
    bullet = insert_game(clean_engine, ["e4"], uuid="b", time_class="bullet")
    rapid = insert_game(clean_engine, ["e4"], uuid="r", time_class="rapid")
    blitz = insert_game(clean_engine, ["e4"], uuid="z", time_class="blitz")
    _enqueue(clean_engine)
    order = []
    while True:
        with Session(clean_engine) as s, s.begin():
            lease = queue.claim(s, "w", [JOB_TYPE])
        if lease is None:
            break
        order.append(lease.payload["game_id"])
    assert set(order[:2]) == {rapid, blitz}
    assert order[2] == bullet


def test_only_pending_games_are_enqueued(clean_engine: Engine):
    insert_game(clean_engine, ["e4"], uuid="a", analysis_status="skipped")
    insert_game(clean_engine, ["e4"], uuid="b", analysis_status="complete")
    pending = insert_game(clean_engine, ["e4"], uuid="c")
    assert _enqueue(clean_engine) == 1
    assert _one(clean_engine, "SELECT payload FROM jobs").payload == {"game_id": pending}


def test_one_game_between_two_tracked_players_is_two_jobs(clean_engine: Engine):
    """Two `games` rows (one per point of view) need two sets of moves, so the
    job is keyed on games.id, not the Chess.com uuid."""
    insert_game(clean_engine, ["e4"], username="alice", uuid="shared", color="white")
    insert_game(clean_engine, ["e4"], username="bob", uuid="shared", color="black")
    assert _enqueue(clean_engine) == 2


# --- one game, end to end ---------------------------------------------------


def test_a_game_is_analysed_end_to_end(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, LONG_GAME)
    _enqueue(clean_engine)
    assert _worker(clean_engine, stockfish).run(until_empty=True) == 1

    game = _one(clean_engine, "SELECT * FROM games WHERE id = :id", id=game_id)
    assert (game.analysis_status, game.ply_count) == ("complete", len(LONG_GAME))
    assert game.analyzed_at is not None
    plies = _one(
        clean_engine,
        "SELECT count(*) AS n, min(ply) AS lo, max(ply) AS hi, "
        "count(*) FILTER (WHERE is_player_move) AS mine, "
        "count(*) FILTER (WHERE classification IS NULL) AS unclassified "
        "FROM moves WHERE game_id = :id",
        id=game_id,
    )
    assert (plies.n, plies.lo, plies.hi) == (len(LONG_GAME), 1, len(LONG_GAME))
    assert plies.mine == len(LONG_GAME) // 2
    assert plies.unclassified == 0
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.status, job.attempts, job.locked_by) == ("done", 1, "w1")


def test_the_stored_moves_carry_the_movers_point_of_view(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, HANGS_A_BISHOP)
    _enqueue(clean_engine)
    _worker(clean_engine, stockfish).run(until_empty=True)
    ba6 = _one(clean_engine, "SELECT * FROM moves WHERE game_id = :id AND ply = 3", id=game_id)
    bxa6 = _one(clean_engine, "SELECT * FROM moves WHERE game_id = :id AND ply = 4", id=game_id)
    assert (ba6.san, ba6.classification, ba6.is_player_move) == ("Ba6", "blunder", True)
    assert ba6.eval_cp_after < -150 < 150 < bxa6.eval_cp_before
    assert bxa6.eval_cp_before == -ba6.eval_cp_after


def test_a_mating_move_is_stored_as_mate_given(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, SCHOLARS_MATE)
    _enqueue(clean_engine)
    _worker(clean_engine, stockfish).run(until_empty=True)
    last = _one(
        clean_engine,
        "SELECT * FROM moves WHERE game_id = :id ORDER BY ply DESC LIMIT 1",
        id=game_id,
    )
    assert (last.san, last.mate_in_after, last.eval_cp_after) == ("Qxf7#", 0, None)
    assert last.classification == "best"


def test_re_analysing_a_game_rewrites_its_moves_rather_than_duplicating_them(
    clean_engine: Engine, stockfish
):
    """The safety net under the fencing: if a game ever is run twice, moves are
    upserted on (game_id, ply), so the second run overwrites the first."""
    insert_game(clean_engine, HANGS_A_BISHOP)
    _enqueue(clean_engine)
    _worker(clean_engine, stockfish).run(until_empty=True)
    with clean_engine.begin() as c:
        c.execute(text("UPDATE games SET analysis_status = 'pending'"))
        c.execute(text("UPDATE jobs SET status = 'pending'"))
    _worker(clean_engine, stockfish).run(until_empty=True)
    assert _one(clean_engine, "SELECT count(*) AS n FROM moves").n == len(HANGS_A_BISHOP)


def test_an_already_complete_game_is_not_analysed_again(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, HANGS_A_BISHOP)
    _enqueue(clean_engine)
    with clean_engine.begin() as c:
        c.execute(text("UPDATE games SET analysis_status = 'complete'"))
    _worker(clean_engine, stockfish).run(until_empty=True)
    assert _one(clean_engine, "SELECT status FROM jobs").status == "done"
    assert (
        _one(clean_engine, "SELECT count(*) AS n FROM moves WHERE game_id = :id", id=game_id).n == 0
    )


# --- failures ---------------------------------------------------------------


def test_a_game_that_cannot_be_replayed_fails_permanently(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, [], pgn="1. e4 e5 2. Ke3 *")
    _enqueue(clean_engine)
    _worker(clean_engine, stockfish).run(until_empty=True)
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.status, job.attempts) == ("failed", 1)
    assert "BadGame" in job.last_error
    game = _one(clean_engine, "SELECT * FROM games WHERE id = :id", id=game_id)
    assert game.analysis_status == "error"


def test_an_engine_failure_is_retried_later(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, HANGS_A_BISHOP)
    _enqueue(clean_engine)
    worker = _worker(clean_engine, stockfish)

    def broken(board: chess.Board) -> Evaluation:
        raise EngineCrashed("simulated")

    worker.evaluate = broken
    assert worker.run(max_jobs=1) == 1
    job = _one(clean_engine, "SELECT *, run_after > now() AS later FROM jobs")
    assert (job.status, job.attempts, job.later) == ("pending", 1, True)
    game = _one(clean_engine, "SELECT * FROM games WHERE id = :id", id=game_id)
    assert game.analysis_status == "pending"
    assert _one(clean_engine, "SELECT count(*) AS n FROM moves").n == 0


def test_a_worker_that_loses_its_lease_mid_game_writes_nothing(clean_engine: Engine, stockfish):
    """Mid-analysis, A's lease is reaped and B claims the job (as if A had
    stalled past the lock timeout). A must notice at its next heartbeat or at
    completion, discard its work, and leave the job to B."""
    game_id = insert_game(clean_engine, LONG_GAME)
    _enqueue(clean_engine)
    a = _worker(clean_engine, stockfish, "A", job_lock_timeout_sec=60)
    a.heartbeat_interval = 0  # heartbeat after every position
    calls = 0

    def stalls_then_resumes(board: chess.Board) -> Evaluation:
        nonlocal calls
        calls += 1
        if calls == 10:
            with clean_engine.begin() as c:
                c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
            with Session(clean_engine) as s, s.begin():
                queue.reap_expired(s, 60)
                assert queue.claim(s, "B", [JOB_TYPE]) is not None
        return stockfish.evaluate(board)

    a.evaluate = stalls_then_resumes
    a.run(max_jobs=1)

    assert calls == 10, "A should stop at the first heartbeat after losing its lease"
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.status, job.locked_by, job.attempts) == ("running", "B", 2)
    assert (
        _one(clean_engine, "SELECT count(*) AS n FROM moves WHERE game_id = :id", id=game_id).n == 0
    )


def test_the_reaper_puts_the_games_status_back(clean_engine: Engine, stockfish):
    game_id = insert_game(clean_engine, LONG_GAME)
    _enqueue(clean_engine)
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET max_attempts = 1"))
    with Session(clean_engine) as s, s.begin():
        queue.claim(s, "dead-worker", [JOB_TYPE])
    with clean_engine.begin() as c:
        c.execute(text("UPDATE games SET analysis_status = 'running'"))
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    (reaped,) = _worker(clean_engine, stockfish).reap()
    assert reaped.status == "dead"
    game = _one(clean_engine, "SELECT * FROM games WHERE id = :id", id=game_id)
    assert game.analysis_status == "error"


def test_a_lock_timeout_too_short_for_the_engine_timeout_is_refused(stockfish):
    """Heartbeats happen between positions, so one search has to fit in the
    lock timeout with room to spare, or a live worker gets reaped mid-search."""
    with pytest.raises(ValueError, match="ENGINE_TIMEOUT_SEC"):
        Worker(
            None,  # type: ignore[arg-type]
            stockfish,
            worker_id="w",
            settings=_settings(job_lock_timeout_sec=30, engine_timeout_sec=30),
        )


# --- the cache seam ---------------------------------------------------------


class RecordingCache:
    def __init__(self) -> None:
        self.store: dict[tuple, Evaluation] = {}
        self.gets = 0

    def get(self, key, engine_version, depth):
        self.gets += 1
        return self.store.get((key, engine_version, depth))

    def put(self, key, fen, engine_version, depth, evaluation):
        self.store.setdefault((key, engine_version, depth), evaluation)


def test_the_evaluator_asks_the_cache_first_and_fills_it_on_a_miss(stockfish):
    cache = RecordingCache()
    evaluate = CachedEvaluator(stockfish, cache)
    board = chess.Board()
    first = evaluate(board)
    assert len(cache.store) == 1
    (key,) = cache.store
    assert key[1:] == ("sf_18", DEPTH)

    sentinel = Evaluation(eval_cp=12345, mate_in=None, best_move="a2a3")
    cache.store[key] = sentinel
    assert evaluate(board) is sentinel, "a hit must not reach the engine"
    assert first.eval_cp != 12345
