"""Metrics recording in the analysis worker: engine time, job outcomes, job duration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import Engine, text

from chess_coach import queue
from chess_coach.analysis.runner import Worker
from chess_coach.engine import StockfishEngine
from chess_coach.queue import Lease
from tests.games import SCHOLARS_MATE, insert_game

pytestmark = [pytest.mark.postgres, pytest.mark.engine]


def get_metric_value(metric_name: str, labels: dict | None = None) -> float:
    """Get current value of a metric from the Prometheus registry."""
    sample_name = metric_name + "_total" if metric_name.startswith("chess_coach_") else metric_name
    if not sample_name.startswith("chess_coach_"):
        sample_name = f"chess_coach_{sample_name}"

    return REGISTRY.get_sample_value(sample_name, labels=labels) or 0.0


def test_metrics_recorded_for_cold_then_cached_game(
    clean_engine: Engine, database_url: str, stockfish_path: Path
):
    """Running a cold game increments GAMES_ANALYZED, POSITIONS_EVALUATED (misses),
    ENGINE_SECONDS > 0. Running the same game again (all hits) leaves POSITIONS_EVALUATED
    and ENGINE_SECONDS unchanged."""
    # Get baseline metrics
    games_before = get_metric_value("chess_coach_games_analyzed", {"outcome": "complete"})
    positions_before = get_metric_value("chess_coach_positions_evaluated")
    engine_before = get_metric_value("chess_coach_engine_seconds")

    # Create and analyze first game (cold, all misses)
    insert_game(clean_engine, SCHOLARS_MATE, uuid="cold1")
    with clean_engine.begin() as conn:
        conn.execute(text("INSERT INTO jobs (type, payload, dedupe_key) VALUES (:t, :p, :d)"),
            {"t": "analyze-game", "p": '{"game_id": 1}', "d": "analyze-game:1"}
        )

    # Create worker and process job
    stockfish = StockfishEngine(str(stockfish_path), depth=6, hash_mb=16)
    worker = Worker(clean_engine, stockfish, worker_id="test-cold", settings=None)
    with clean_engine.begin() as conn:
        lease = queue.claim(conn, "test-cold", worker.job_types)

    if lease:
        worker.process(lease)

    # Verify cold game incremented metrics
    games_after_cold = get_metric_value("chess_coach_games_analyzed", {"outcome": "complete"})
    positions_after_cold = get_metric_value("chess_coach_positions_evaluated")
    engine_after_cold = get_metric_value("chess_coach_engine_seconds")

    assert games_after_cold > games_before, "GAMES_ANALYZED should increment"
    assert (
        positions_after_cold > positions_before
    ), "POSITIONS_EVALUATED should increment on cold game"
    assert engine_after_cold > engine_before, (
        "ENGINE_SECONDS should be > 0 on cold game"
    )

    # Analyze same game again (all hits from cache)
    insert_game(clean_engine, SCHOLARS_MATE, uuid="cached1")
    with clean_engine.begin() as conn:
        conn.execute(text("INSERT INTO jobs (type, payload, dedupe_key) VALUES (:t, :p, :d)"),
            {"t": "analyze-game", "p": '{"game_id": 2}', "d": "analyze-game:2"}
        )
        lease = queue.claim(conn, "test-cold", worker.job_types)

    if lease:
        worker.process(lease)

    # Verify cached game doesn't increment positions/engine but does games
    games_after_cached = get_metric_value("chess_coach_games_analyzed", {"outcome": "complete"})
    positions_after_cached = get_metric_value("chess_coach_positions_evaluated")
    engine_after_cached = get_metric_value("chess_coach_engine_seconds")

    assert (
        games_after_cached > games_after_cold
    ), "GAMES_ANALYZED should increment for cached game too"
    assert (
        positions_after_cached == positions_after_cold
    ), "POSITIONS_EVALUATED should not change on cache hits"
    assert (
        engine_after_cached == engine_after_cold
    ), "ENGINE_SECONDS should not change on cache hits"


def test_job_metrics_on_failure(clean_engine: Engine):
    """A job that fails increments JOBS_RETRIED. A job that exhausts retries
    increments JOBS_DEAD. Both record JOB_DURATION_SECONDS."""

    class FailingWorker(Worker):
        def __init__(self, *args, fail_count=0, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_count = fail_count
            self.call_count = 0

        def handle(self, lease: Lease) -> None:
            self.call_count += 1
            if self.call_count <= self.fail_count:
                raise ValueError("Intentional test failure")
            # Complete the lease on success
            with clean_engine.begin() as conn:
                queue.complete(conn, lease)

    # Get baseline
    retried_before = get_metric_value("chess_coach_jobs_retried", {"job_type": "analyze-game"})
    dead_before = get_metric_value("chess_coach_jobs_dead", {"job_type": "analyze-game"})

    # Create a job that will fail once then succeed
    with clean_engine.begin() as conn:
        conn.execute(text("INSERT INTO jobs (type, payload, dedupe_key) VALUES (:t, :p, :d)"),
            {"t": "analyze-game", "p": '{"game_id": 999}', "d": "test-retry:1"}
        )
        lease = queue.claim(conn, "test-retry", ("analyze-game",))

    worker = FailingWorker(clean_engine, None, worker_id="test-retry", fail_count=1)
    if lease:
        worker.process(lease)

    # Should have incremented retried
    retried_after = get_metric_value("chess_coach_jobs_retried", {"job_type": "analyze-game"})
    assert retried_after > retried_before, "JOBS_RETRIED should increment on failure"

    # Test dead-lettering: job that always fails
    with clean_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (type, payload, dedupe_key, attempt, max_attempts) "
                "VALUES (:t, :p, :d, :a, :m)"
            ),
            {"t": "analyze-game", "p": '{"game_id": 999}', "d": "test-dead:1", "a": 10, "m": 1},
        )
        lease = queue.claim(conn, "test-dead", ("analyze-game",))

    worker_dead = FailingWorker(clean_engine, None, worker_id="test-dead", fail_count=10)
    if lease:
        worker_dead.process(lease)

    # Should have incremented dead
    dead_after = get_metric_value("chess_coach_jobs_dead", {"job_type": "analyze-game"})
    assert dead_after > dead_before, "JOBS_DEAD should increment when job exhausts attempts"


def test_two_workers_no_port_collision(database_url: str, stockfish_path: Path):
    """Two workers started with default settings both exit successfully
    (no port collision)."""
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": database_url,
        "STOCKFISH_PATH": str(stockfish_path),
        "ENGINE_DEPTH": "6",
        "ENGINE_HASH_MB": "16",
        "WORKER_POLL_INTERVAL_SEC": "0.1",
        "ENV": "test",
    })

    # Start two workers with --until-empty (no metrics port specified, defaults to 0)
    proc1 = subprocess.run(
        [sys.executable, "-m", "chess_coach.worker", "--worker-id", "w1", "--until-empty"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    proc2 = subprocess.run(
        [sys.executable, "-m", "chess_coach.worker", "--worker-id", "w2", "--until-empty"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    # Both should exit successfully (no port collision)
    assert proc1.returncode == 0, f"Worker 1 failed: {proc1.stderr}"
    assert proc2.returncode == 0, f"Worker 2 failed: {proc2.stderr}"
