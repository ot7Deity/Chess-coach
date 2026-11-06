"""The throughput benchmark's driver: rounds, JSON output, the table, the CLI."""

from __future__ import annotations

import json
import sys

import pytest
from bench.throughput import BenchConfig, RoundResult, main, markdown_table, run_benchmark
from sqlalchemy import Engine

from chess_coach.config import get_settings
from tests.bench_support import create_workload


def row(n: int, gpm: float | None, hit: float | None) -> RoundResult:
    return RoundResult(
        workers=n,
        games_target=10,
        games_completed=10,
        wall_seconds=60 / gpm * 10 if gpm else None,
        games_per_minute=gpm,
        plies=800,
        plies_per_second=1.0,
        lookups=810,
        engine_evaluations=500,
        cache_hit_rate=hit,
        engine_seconds_spent=50.0,
        engine_seconds_without_cache=80.0,
        engine_seconds_saved=30.0,
        search=["sf_18", "16"],
    )


def test_markdown_table_reports_speedup_against_one_worker():
    lines = markdown_table([row(1, 10.0, 0.0), row(2, 18.0, None)]).splitlines()
    assert lines[0].startswith("| Workers |")
    assert "| 1.00x |" in lines[2] and "| 0.0% |" in lines[2]
    assert "| 1.80x |" in lines[3] and "| -% |" in lines[3]


def test_markdown_table_survives_a_round_that_finished_nothing():
    lines = markdown_table([row(1, None, None)]).splitlines()
    assert "| - | -x |" in lines[2]


def test_the_cli_refuses_a_real_database_before_touching_it(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/chess_coach")
    get_settings.cache_clear()
    try:
        assert main(["--games", "1", "--workers", "1", "--", sys.executable]) == 2
    finally:
        get_settings.cache_clear()
    assert "refusing" in capsys.readouterr().err


@pytest.mark.postgres
def test_each_round_is_recorded_and_written_to_json(clean_engine: Engine, database_url, tmp_path):
    create_workload(clean_engine)
    config = BenchConfig(
        database_url=database_url,
        redis_url=None,
        worker_cmd=[sys.executable, "-c", "raise SystemExit(0)"],
        workers=[1, 2],
        games=6,
        timeout_sec=60,
        out_dir=tmp_path,
    )
    results, out = run_benchmark(config)  # round 2 re-enqueues the same dedupe keys
    assert [(r.workers, r.games_target, r.worker_exit_codes) for r in results] == [
        (1, 6, [0]),
        (2, 6, [0, 0]),
    ]
    saved = json.loads(out.read_text())
    assert saved["config"]["workers"] == [1, 2]
    assert saved["config"]["games"] == 6
    assert saved["host"]["cpu_count"]
    assert [r["workers"] for r in saved["rounds"]] == [1, 2]
