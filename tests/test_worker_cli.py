"""`chess-coach-worker` as a real process: the interface the M3 benchmark
harness launches N copies of and then measures from the database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from tests.games import HANGS_A_BISHOP, SCHOLARS_MATE, insert_game

pytestmark = [pytest.mark.postgres, pytest.mark.engine]


def _run(database_url: str, stockfish_path: Path, *args: str, **env_overrides: str):
    env = dict(os.environ)
    env.update(
        DATABASE_URL=database_url,
        STOCKFISH_PATH=str(stockfish_path),
        ENGINE_DEPTH="6",
        ENGINE_HASH_MB="16",
        WORKER_POLL_INTERVAL_SEC="0.1",
        ENV="test",
    )
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "chess_coach.worker", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_until_empty_on_an_empty_queue_exits_at_once(
    clean_engine: Engine, database_url: str, stockfish_path: Path
):
    proc = _run(database_url, stockfish_path, "--until-empty")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"processed": 0' in proc.stdout


def test_enqueue_pending_then_drain(clean_engine: Engine, database_url: str, stockfish_path: Path):
    insert_game(clean_engine, HANGS_A_BISHOP, uuid="a")
    insert_game(clean_engine, SCHOLARS_MATE, uuid="b")
    proc = _run(
        database_url, stockfish_path, "--worker-id", "bench-1", "--enqueue-pending", "--until-empty"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with clean_engine.connect() as c:
        jobs = c.execute(text("SELECT status, locked_by FROM jobs ORDER BY id")).all()
        complete = c.scalar(text("SELECT count(*) FROM games WHERE analysis_status = 'complete'"))
    assert [tuple(j) for j in jobs] == [("done", "bench-1"), ("done", "bench-1")]
    assert complete == 2


def test_max_jobs_stops_after_that_many(
    clean_engine: Engine, database_url: str, stockfish_path: Path
):
    for i in range(3):
        insert_game(clean_engine, HANGS_A_BISHOP, uuid=f"g{i}")
    proc = _run(database_url, stockfish_path, "--enqueue-pending", "--max-jobs", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with clean_engine.connect() as c:
        statuses = c.execute(text("SELECT status, count(*) FROM jobs GROUP BY status")).all()
    assert dict(statuses) == {"done": 2, "pending": 1}


def test_a_missing_engine_binary_is_a_clear_error_not_a_traceback(
    clean_engine: Engine, database_url: str, tmp_path: Path
):
    proc = _run(database_url, tmp_path / "no-such-stockfish", "--until-empty")
    assert proc.returncode == 2
    assert "worker.no_engine" in proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr
