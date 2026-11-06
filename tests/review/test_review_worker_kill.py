"""M2's 'done when', tested the way it is stated: kill a real worker process
mid-job, with no chance to clean up, and the game is still analysed exactly
once. Skips on branches without chess_coach.worker.

M2's own suite simulates the lost lease in-process; nothing there kills a
`chess-coach-worker`. These tests do (Popen.kill is TerminateProcess on
Windows, SIGKILL on Linux).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

pytest.importorskip("chess_coach.worker")

from chess_coach.analysis.jobs import enqueue_pending_games

from tests.games import LONG_GAME, insert_game

pytestmark = [pytest.mark.postgres, pytest.mark.engine]

# A lease lapses 6 s after its last heartbeat; heartbeats every 2 s. The engine
# timeout must stay under two thirds of the lock timeout (Worker refuses
# otherwise).
_ENV = {
    "ENGINE_DEPTH": "8",
    "ENGINE_HASH_MB": "16",
    "ENGINE_TIMEOUT_SEC": "3",
    "JOB_LOCK_TIMEOUT_SEC": "6",
    "JOB_RETRY_BASE_SEC": "1",
    "WORKER_POLL_INTERVAL_SEC": "0.1",
    "POSITION_CACHE": "off",
    "ENV": "test",
}


def _env(database_url: str, stockfish_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_ENV, DATABASE_URL=database_url, STOCKFISH_PATH=str(stockfish_path))
    return env


def _worker_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "chess_coach.worker", *args]


def _wait_for(engine: Engine, sql: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as c:
            if c.scalar(text(sql)):
                return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {sql}")


def _assert_each_game_analysed_once(engine: Engine, game_ids: list[int]) -> None:
    plies = len(LONG_GAME)
    with engine.connect() as c:
        for gid in game_ids:
            status, ply_count = c.execute(
                text("SELECT analysis_status, ply_count FROM games WHERE id = :g"), {"g": gid}
            ).one()
            moves = c.scalars(
                text("SELECT ply FROM moves WHERE game_id = :g ORDER BY ply"), {"g": gid}
            ).all()
            assert (status, ply_count) == ("complete", plies), gid
            assert moves == list(range(1, plies + 1)), gid
        jobs = c.execute(text("SELECT status, count(*) FROM jobs GROUP BY status")).all()
    assert dict(jobs) == {"done": len(game_ids)}


def test_a_hard_killed_worker_process_still_ends_in_exactly_one_analysis(
    clean_engine: Engine, database_url: str, stockfish_path: Path
):
    game_ids = [insert_game(clean_engine, LONG_GAME, uuid=f"kill-{i}") for i in range(2)]
    with Session(clean_engine) as s, s.begin():
        assert enqueue_pending_games(s, max_attempts=5) == 2

    env = _env(database_url, stockfish_path)
    victim = subprocess.Popen(
        _worker_cmd("--worker-id", "victim"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Mid-game: the job is running and its game is marked running.
        _wait_for(clean_engine, "SELECT count(*) FROM games WHERE analysis_status = 'running'")
        time.sleep(0.3)
        victim.kill()
        victim.wait(10)
    finally:
        if victim.poll() is None:
            victim.kill()

    with clean_engine.connect() as c:
        killed = c.execute(
            text("SELECT id, attempts FROM jobs WHERE status = 'running' AND locked_by = 'victim'")
        ).all()
        partial = c.scalar(text("SELECT count(*) FROM moves"))
    assert len(killed) == 1, "the victim was not mid-job when killed"
    assert partial in (0, len(LONG_GAME)), "a half-written game"

    survivor = subprocess.run(
        _worker_cmd("--worker-id", "survivor", "--until-empty"),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stdout[-2000:] + survivor.stderr[-2000:]

    _assert_each_game_analysed_once(clean_engine, game_ids)
    with clean_engine.connect() as c:
        attempts, locked_by = c.execute(
            text("SELECT attempts, locked_by FROM jobs WHERE id = :j"), {"j": killed[0].id}
        ).one()
    assert (attempts, locked_by) == (killed[0].attempts + 1, "survivor")


def test_three_workers_one_killed_mid_run_finish_every_game_once(
    clean_engine: Engine, database_url: str, stockfish_path: Path
):
    game_ids = [insert_game(clean_engine, LONG_GAME, uuid=f"pool-{i}") for i in range(6)]
    with Session(clean_engine) as s, s.begin():
        enqueue_pending_games(s, max_attempts=5)

    env = _env(database_url, stockfish_path)
    workers = [
        subprocess.Popen(
            _worker_cmd("--worker-id", f"pool{i}", "--until-empty"),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(3)
    ]
    try:
        _wait_for(
            clean_engine,
            "SELECT count(*) FROM jobs WHERE status = 'running' AND locked_by = 'pool0'",
        )
        workers[0].kill()
        for w in workers[1:]:
            assert w.wait(240) == 0
    finally:
        for w in workers:
            if w.poll() is None:
                w.kill()

    _assert_each_game_analysed_once(clean_engine, game_ids)
