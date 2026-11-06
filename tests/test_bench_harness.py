"""The benchmark harness's parts: safety check, reset, enqueue, launch, measure."""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
from bench.harness import (
    DEFAULT_DEDUPE_KEY,
    DEFAULT_JOB_PAYLOAD,
    DEFAULT_JOB_TYPE,
    BenchError,
    check_database_is_disposable,
    enqueue,
    expand_command,
    measure,
    reset_workload,
    run_round,
    select_workload,
)
from sqlalchemy import Engine, text

from tests.bench_support import create_workload


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@localhost/chess_coach",
        "postgresql+psycopg://u:p@localhost/production",
    ],
)
def test_it_refuses_a_database_that_is_not_disposable(url):
    with pytest.raises(BenchError, match="bench"):
        check_database_is_disposable(url)


def test_it_accepts_bench_and_test_databases():
    check_database_is_disposable("postgresql+psycopg://u:p@h/chess_coach_bench")
    check_database_is_disposable("postgresql+psycopg://u:p@h/chess_coach_test_m3")


def test_placeholders_are_filled_per_worker():
    cmd = expand_command(
        [sys.executable, "--id", "{worker_id}", "--i={index}/{n}", "--port", "{metrics_port}"],
        n=4,
        index=2,
        metrics_port=9100,
    )
    assert cmd[1:] == ["--id", "bench-4-2", "--i=2/4", "--port", "9102"]
    assert expand_command([sys.executable, "{metrics_port}"], n=1, index=0, metrics_port=0)[1:] == [
        "0"
    ]


def test_job_defaults_match_the_format_m2_confirmed():
    """A wrong default doesn't fail - the workers just find nothing to claim, exit
    on an empty queue, and the round reports 0 games. So pin it to what M2 said."""
    assert DEFAULT_JOB_TYPE == "analyze-game"
    assert json.loads(DEFAULT_JOB_PAYLOAD.format(game_id=42)) == {"game_id": 42}
    assert DEFAULT_DEDUPE_KEY.format(game_id=42) == "analyze-game:42"


def test_the_default_worker_command_is_m2s_cli():
    from bench.throughput import DEFAULT_WORKER_CMD

    assert DEFAULT_WORKER_CMD == [
        "chess-coach-worker",
        "--worker-id",
        "{worker_id}",
        "--until-empty",
    ]


def test_a_missing_worker_command_is_a_clear_error():
    with pytest.raises(BenchError, match="not found"):
        expand_command(["definitely-not-a-real-command-xyz"], n=1, index=0, metrics_port=0)


# --- against Postgres -------------------------------------------------------------

pg = pytest.mark.postgres


@pg
def test_select_workload_is_the_same_games_in_play_order(clean_engine: Engine):
    ids, _ = create_workload(clean_engine)
    assert select_workload(clean_engine, 4, None) == ids[:4]
    assert select_workload(clean_engine, 100, "BenchPlayer") == ids
    with pytest.raises(BenchError):
        select_workload(clean_engine, 4, "nobody")


@pg
def test_enqueue_fills_every_not_null_column_and_replaces_its_own_jobs(clean_engine: Engine):
    """priority/attempts/max_attempts have ORM-side defaults only; a raw INSERT
    that omitted them would fail. And a second run must not collide on the
    unique dedupe_key."""
    ids, _ = create_workload(clean_engine)
    for _ in range(2):
        enqueue(clean_engine, ids, "analyze-game", '{{"game_id": {game_id}}}', "k:{game_id}")
    with clean_engine.connect() as conn:
        jobs = conn.execute(
            text("SELECT type, payload, status, priority, attempts, dedupe_key FROM jobs")
        ).all()
    assert len(jobs) == len(ids)
    assert {(j.type, j.status, j.priority, j.attempts) for j in jobs} == {
        ("analyze-game", "pending", 100, 0)
    }
    assert sorted(j.payload["game_id"] for j in jobs) == sorted(ids)
    assert {j.dedupe_key for j in jobs} == {f"k:{i}" for i in ids}


@pg
@pytest.mark.redis
def test_reset_makes_the_workload_cold_again(clean_engine: Engine, redis_client):
    ids, _ = create_workload(clean_engine)
    prefix = f"benchreset:{uuid.uuid4().hex[:8]}:"
    redis_client.set(prefix + "sf_18:16:" + "ab" * 32, b"x")
    redis_client.set(prefix.rstrip(":") + "-other", b"keep")
    with clean_engine.begin() as conn:
        conn.execute(text("UPDATE games SET analysis_status = 'complete', analyzed_at = now()"))
        conn.execute(
            text(
                "INSERT INTO positions (fen_hash, engine_version, depth, fen, eval_cp) "
                "VALUES (:h, 'sf_18', 16, 'x', 1)"
            ),
            {"h": b"\x01" * 32},
        )
    try:
        reset_workload(
            clean_engine, ids, cold_cache=True, redis_client=redis_client, key_prefix=prefix
        )
        with clean_engine.connect() as conn:
            statuses = conn.execute(text("SELECT analysis_status, analyzed_at FROM games")).all()
            positions = conn.execute(text("SELECT count(*) FROM positions")).scalar_one()
        assert set(statuses) == {("pending", None)}
        assert positions == 0
        assert list(redis_client.scan_iter(match=prefix + "*")) == []
        assert redis_client.exists(prefix.rstrip(":") + "-other")
    finally:
        redis_client.delete(prefix.rstrip(":") + "-other")


@pg
def test_a_round_ends_when_every_worker_has_exited(clean_engine: Engine, tmp_path):
    ids, _ = create_workload(clean_engine)
    started, codes, timed_out = run_round(
        clean_engine,
        ids,
        2,
        [sys.executable, "-c", "raise SystemExit(3)"],
        env=dict(os.environ),
        log_dir=tmp_path,
        timeout_sec=60,
    )
    assert codes == [3, 3] and not timed_out
    result = measure(clean_engine, ids, 2, started)
    assert result.games_completed == 0
    assert result.games_per_minute is None
    assert result.cache_hit_rate is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["workers-2-0.log", "workers-2-1.log"]


@pg
def test_a_round_that_overruns_is_stopped_and_reported(clean_engine: Engine, tmp_path):
    ids, _ = create_workload(clean_engine)
    _, codes, timed_out = run_round(
        clean_engine,
        ids,
        1,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ),
        log_dir=tmp_path,
        timeout_sec=1,
        poll_sec=0.2,
    )
    assert timed_out
    assert codes[0] not in (None, 0)  # it was terminated, not left running
