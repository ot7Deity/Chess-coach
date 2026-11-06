"""M2's acceptance test: kill a worker mid-game, and the game is still
analysed exactly once.

Real processes, real Postgres, real Stockfish. Worker A is started as a
subprocess, allowed to get properly into a game (its lease has been
heartbeated at least once, so it is mid-analysis, not just past the claim),
then killed hard - TerminateProcess on Windows, SIGKILL elsewhere: no signal
handler, no cleanup, no chance to release anything. Worker B is started and
told to drain the queue. B has to wait out A's lease, take the job through the
reaper, analyse the game, and exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach.analysis.jobs import enqueue_pending_games
from tests.games import HANGS_A_BISHOP, LONG_GAME, SCHOLARS_MATE, insert_game

pytestmark = [pytest.mark.postgres, pytest.mark.engine]

LOCK_TIMEOUT = 3.0  # workers heartbeat every second


def _env(database_url: str, stockfish_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        DATABASE_URL=database_url,
        STOCKFISH_PATH=str(stockfish_path),
        # Deep enough that one game takes a few seconds, so there is a real
        # "middle" to kill A in.
        ENGINE_DEPTH="12",
        ENGINE_HASH_MB="16",
        ENGINE_TIMEOUT_SEC="1.5",
        JOB_LOCK_TIMEOUT_SEC=str(LOCK_TIMEOUT),
        WORKER_POLL_INTERVAL_SEC="0.2",
        ENV="test",  # JSON logs, parsed below
        LOG_LEVEL="INFO",
    )
    return env


def _start(name: str, env: dict[str, str], log: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "chess_coach.worker", "--worker-id", name, *extra],
        env=env,
        stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def _events(log: Path) -> Counter:
    events: Counter = Counter()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events[json.loads(line)["event"]] += 1
        except (ValueError, KeyError, TypeError):
            continue
    return events


def _job(engine: Engine):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM jobs")).one()


def _move_count(engine: Engine) -> int:
    with engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM moves"))


def _wait(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def test_a_worker_killed_mid_game_still_gets_the_game_analysed_exactly_once(
    clean_engine: Engine, database_url: str, stockfish_path: Path, tmp_path: Path
):
    game_id = insert_game(clean_engine, LONG_GAME)
    with Session(clean_engine) as s, s.begin():
        assert enqueue_pending_games(s, max_attempts=5) == 1
    env = _env(database_url, stockfish_path)

    # --- A claims the game and gets into it --------------------------------
    a = _start("A", env, tmp_path / "a.log")
    try:
        _wait(lambda: _job(clean_engine).locked_by == "A", 60, "A to claim the job")
        claimed_at = _job(clean_engine).locked_at
        _wait(
            lambda: _job(clean_engine).locked_at > claimed_at,
            30,
            "A's first heartbeat (proof it is mid-analysis)",
        )
        assert _move_count(clean_engine) == 0, "A finished before it could be killed"
    finally:
        a.kill()  # TerminateProcess / SIGKILL: no handler runs
        a.wait(timeout=30)

    # A is gone: its lease stops moving, and it wrote nothing.
    frozen = _job(clean_engine)
    time.sleep(LOCK_TIMEOUT / 3 * 2.5)
    after = _job(clean_engine)
    assert (after.status, after.locked_by, after.attempts) == ("running", "A", 1)
    assert after.locked_at == frozen.locked_at, "A (or its interpreter) is still heartbeating"
    assert _move_count(clean_engine) == 0

    # --- B drains the queue: reap, re-claim, analyse ------------------------
    b = _start("B", env, tmp_path / "b.log", "--until-empty")
    assert b.wait(timeout=180) == 0, (tmp_path / "b.log").read_text(errors="replace")

    job = _job(clean_engine)
    assert (job.status, job.locked_by, job.attempts) == ("done", "B", 2)
    assert "lease expired: A" in job.last_error

    with clean_engine.connect() as c:
        game = c.execute(text("SELECT * FROM games WHERE id = :id"), {"id": game_id}).one()
        plies = c.execute(
            text(
                "SELECT count(*) AS n, count(DISTINCT ply) AS distinct_plies, "
                "min(ply) AS lo, max(ply) AS hi, "
                "count(*) FILTER (WHERE classification IS NULL) AS ungraded "
                "FROM moves WHERE game_id = :id"
            ),
            {"id": game_id},
        ).one()
    assert (game.analysis_status, game.ply_count) == ("complete", len(LONG_GAME))
    assert (plies.n, plies.distinct_plies, plies.lo, plies.hi) == (
        len(LONG_GAME),
        len(LONG_GAME),
        1,
        len(LONG_GAME),
    )
    assert plies.ungraded == 0

    # Completed once, by B, after reaping A's lease; A never completed it.
    a_events, b_events = _events(tmp_path / "a.log"), _events(tmp_path / "b.log")
    assert a_events["job.done"] == 0
    assert b_events["job.reaped"] == 1
    assert b_events["job.done"] == 1

    # For the record (pytest -s): how long recovery took, from A's last sign of
    # life to the game being complete. Bounded below by the lock timeout.
    print(
        "\nlast heartbeat from A -> game complete: "
        f"{(job.finished_at - frozen.locked_at).total_seconds():.1f}s "
        f"(lock timeout {LOCK_TIMEOUT}s)"
    )


def test_concurrent_workers_analyse_each_game_once(
    clean_engine: Engine, database_url: str, stockfish_path: Path, tmp_path: Path
):
    """Three workers, eight games, no failures: every job done on its first
    attempt, every game complete, and work actually spread across workers."""
    games = [HANGS_A_BISHOP, SCHOLARS_MATE, LONG_GAME[:30], LONG_GAME[:40]] * 2
    ids = [insert_game(clean_engine, sans, uuid=f"g{i}") for i, sans in enumerate(games)]
    with Session(clean_engine) as s, s.begin():
        enqueue_pending_games(s, max_attempts=5)

    env = _env(database_url, stockfish_path)
    env["ENGINE_DEPTH"] = "8"
    workers = [_start(f"W{i}", env, tmp_path / f"w{i}.log", "--until-empty") for i in range(3)]
    for i, w in enumerate(workers):
        assert w.wait(timeout=180) == 0, (tmp_path / f"w{i}.log").read_text(errors="replace")

    with clean_engine.connect() as c:
        jobs = c.execute(text("SELECT status, attempts, locked_by FROM jobs")).all()
        complete = c.scalar(text("SELECT count(*) FROM games WHERE analysis_status = 'complete'"))
        rows = c.scalar(text("SELECT count(*) FROM moves"))
    assert all((j.status, j.attempts) == ("done", 1) for j in jobs)
    assert complete == len(ids)
    assert rows == sum(len(g) for g in games)
    done = sum(_events(tmp_path / f"w{i}.log")["job.done"] for i in range(3))
    assert done == len(ids)
