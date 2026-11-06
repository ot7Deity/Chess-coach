"""The analysis worker's loop: reap, claim, analyse, complete.

The window that matters is between claim and completion, which for one game
is seconds to minutes of engine time:

* Nothing is written while the engine works. All of a game's moves, its
  `complete` status and the job's `done` commit in one transaction, fenced by
  the lease (`queue.complete` goes first in it), so a game is either fully
  analysed or untouched - there is no half-analysed state to clean up.
* The lease is heartbeated between positions, so a long game is never reaped
  from a live worker, and a worker that has lost its lease finds out within a
  heartbeat and stops spending engine time on a result it may not write.
* A worker killed anywhere in that window leaves a `running` job with a lease
  that stops being renewed. The reaper hands it back and someone else re-runs
  it from scratch. Re-running is safe because moves are upserted on
  (game_id, ply).
"""

from __future__ import annotations

import time

import chess
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.analysis.game import BadGame, MoveRow, analyze_game
from chess_coach.analysis.jobs import JOB_TYPE
from chess_coach.config import Settings
from chess_coach.engine import StockfishEngine
from chess_coach.jobrunner import JobRunner
from chess_coach.logging import get_logger
from chess_coach.models import Game, Move
from chess_coach.positions import EvalCache, Evaluation, NullCache, fen_hash, normalize_fen
from chess_coach.queue import Lease

log = get_logger(__name__)


class CachedEvaluator:
    """Cache first, then the engine. The one place a position is evaluated.

    `cache` is any `positions.EvalCache`; `NullCache` (always miss) until M3's
    cache is wired in.
    """

    def __init__(self, engine: StockfishEngine, cache: EvalCache | None = None) -> None:
        self.engine = engine
        self.cache: EvalCache = cache if cache is not None else NullCache()

    def __call__(self, board: chess.Board) -> Evaluation:
        key = fen_hash(board)
        hit = self.cache.get(key, self.engine.version, self.engine.depth)
        if hit is not None:
            return hit
        evaluation = self.engine.evaluate(board)
        self.cache.put(
            key, normalize_fen(board), self.engine.version, self.engine.depth, evaluation
        )
        return evaluation


class Worker(JobRunner):
    """Analyses `analyze-game` jobs with one Stockfish."""

    job_types = (JOB_TYPE,)

    def __init__(
        self,
        db: Engine,
        engine: StockfishEngine,
        *,
        worker_id: str,
        settings: Settings,
        cache: EvalCache | None = None,
    ) -> None:
        # The lease is renewed between positions, so the longest gap between
        # heartbeats is a third of the lock timeout plus one search. A search
        # is bounded by the engine timeout; if that could outlast the rest of
        # the lock timeout, a live worker would be reaped mid-position.
        if settings.engine_timeout_sec >= settings.job_lock_timeout_sec * 2 / 3:
            raise ValueError(
                f"ENGINE_TIMEOUT_SEC ({settings.engine_timeout_sec}) must be under two thirds "
                f"of JOB_LOCK_TIMEOUT_SEC ({settings.job_lock_timeout_sec})"
            )
        super().__init__(db, worker_id=worker_id, settings=settings)
        self.engine = engine
        self.evaluate = CachedEvaluator(engine, cache)

    def describe(self) -> dict[str, object]:
        return {"engine": self.engine.version, "depth": self.engine.depth}

    # --- one job ------------------------------------------------------------

    def handle(self, lease: Lease) -> None:
        game_id = _game_id(lease.payload)
        if game_id is None:
            raise BadGame(f"payload has no game id: {lease.payload!r}")
        bound = log.bind(job_id=lease.job_id, game_id=game_id, attempt=lease.attempt)
        started = time.monotonic()
        game = self._start(lease, game_id)
        if game is None:
            bound.info("job.already_analysed")
            return
        rows = analyze_game(
            game.pgn,
            start_fen=game.start_fen,
            player_color=game.color,
            time_control=game.time_control,
            evaluate=self.evaluate,
            on_position=self.heartbeat(lease),
        )
        self._finish(lease, game_id, rows)
        bound.info("job.done", plies=len(rows), seconds=round(time.monotonic() - started, 2))

    def on_failed(self, session: Session, lease: Lease, status: str) -> None:
        game_id = _game_id(lease.payload)
        if game_id is not None:
            _set_game_status(session, game_id, "pending" if status == "pending" else "error")

    def on_reaped(self, session: Session, job: queue.ReapedJob) -> None:
        game_id = _game_id(job.payload)
        if game_id is not None:
            _set_game_status(session, game_id, "error" if job.status == "dead" else "pending")

    def _start(self, lease: Lease, game_id: int):
        """Load the game and mark it running. None if there is nothing to do."""
        with Session(self.db) as session, session.begin():
            queue.heartbeat(session, lease)  # still ours, and the clock restarts
            game = session.execute(
                select(
                    Game.pgn, Game.start_fen, Game.color, Game.time_control, Game.analysis_status
                ).where(Game.id == game_id)
            ).one_or_none()
            if game is None:
                raise BadGame(f"game {game_id} does not exist")
            if game.analysis_status in ("complete", "skipped"):
                queue.complete(session, lease)
                return None
            _set_game_status(session, game_id, "running")
            return game

    def _finish(self, lease: Lease, game_id: int, rows: list[MoveRow]) -> None:
        with Session(self.db) as session, session.begin():
            # First, so a lost lease writes nothing at all.
            queue.complete(session, lease)
            if rows:
                stmt = insert(Move).values([{"game_id": game_id, **r.as_dict()} for r in rows])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Move.game_id, Move.ply],
                    set_={k: stmt.excluded[k] for k in rows[0].as_dict() if k != "ply"},
                )
                session.execute(stmt)
            session.execute(
                update(Game)
                .where(Game.id == game_id)
                .values(analysis_status="complete", analyzed_at=func.now(), ply_count=len(rows))
            )


def _game_id(payload: dict) -> int | None:
    try:
        return int(payload["game_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _set_game_status(session: Session, game_id: int, status: str) -> None:
    """Never moves a finished game backwards: a stale job cannot un-complete it."""
    session.execute(
        update(Game)
        .where(Game.id == game_id, Game.analysis_status.not_in(("complete", "skipped")))
        .values(analysis_status=status)
    )
