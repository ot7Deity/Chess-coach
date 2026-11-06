"""M3's benchmark harness against M2's real worker. Needs both stacks, so it
skips unless bench.harness and chess_coach.worker are both importable (an
integration of m2/* and m3/*).

The harness claims everything is "measured from the database ... never from
what the workers report about themselves". These check that the numbers it
derives from the database are the right ones.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

harness = pytest.importorskip("bench.harness")
pytest.importorskip("chess_coach.worker")

from chess_coach.analysis.jobs import JOB_TYPE, dedupe_key  # noqa: E402

from tests.games import HANGS_A_BISHOP, insert_game  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.engine]


def test_the_harness_default_job_is_one_the_worker_claims():
    """Was a BUG up to m3/11 ('analyze_game' vs M2's 'analyze-game'); fixed in m3/12."""
    assert harness.DEFAULT_JOB_TYPE == JOB_TYPE
    assert harness.DEFAULT_DEDUPE_KEY.format(game_id=7) == dedupe_key(7)


def _no_cache_round(clean_engine, database_url, stockfish_path, tmp_path):
    """One game, one real M2 worker with POSITION_CACHE=off, measured by the harness."""
    game_id = insert_game(clean_engine, HANGS_A_BISHOP, uuid="bench-1")
    harness.enqueue(
        clean_engine,
        [game_id],
        JOB_TYPE,
        harness.DEFAULT_JOB_PAYLOAD,
        "analyze-game:{game_id}",
    )
    env = dict(os.environ)
    env.update(
        DATABASE_URL=database_url,
        STOCKFISH_PATH=str(stockfish_path),
        ENGINE_DEPTH="6",
        ENGINE_HASH_MB="16",
        POSITION_CACHE="off",
        WORKER_POLL_INTERVAL_SEC="0.1",
        ENV="test",
    )
    cmd = [sys.executable, "-m", "chess_coach.worker", "--worker-id", "{worker_id}"]
    started, codes, timed_out = harness.run_round(
        clean_engine,
        [game_id],
        1,
        [*cmd, "--until-empty"],
        env=env,
        log_dir=tmp_path,
        timeout_sec=120,
    )
    assert not timed_out and codes == [0]
    result = harness.measure(clean_engine, [game_id], 1, started)
    assert result.games_completed == 1
    return result


def test_a_run_without_a_cache_is_not_reported_as_all_cache_hits(
    clean_engine: Engine, database_url: str, stockfish_path: Path, tmp_path: Path
):
    """Fixed in m3/9: no stored evaluations reads as 'unknown', not '100% hits'.
    (On m3/7 this reported cache_hit_rate == 1.0.)"""
    result = _no_cache_round(clean_engine, database_url, stockfish_path, tmp_path)
    assert result.cache_hit_rate != 1.0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: the 'without cache' baseline (POSITION_CACHE=off) stores no positions "
        "rows, and measure() reads engine time only from positions.compute_ms - so "
        "the baseline's engine seconds are always 0. DESIGN.md's 'engine time saved: "
        "Stockfish seconds with cache vs without, same games' cannot be computed "
        "from the database for the run that matters most."
    ),
)
def test_the_no_cache_baseline_records_the_engine_time_it_spent(
    clean_engine: Engine, database_url: str, stockfish_path: Path, tmp_path: Path
):
    result = _no_cache_round(clean_engine, database_url, stockfish_path, tmp_path)
    assert result.engine_seconds_spent > 0
