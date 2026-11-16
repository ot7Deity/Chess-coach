"""The benchmark end to end with `bench.fake_worker`.

The real worker does not exist yet (M2). Every number the harness reports is
checked against what the workload must produce, computed independently from
the PGNs - so when the real worker arrives, the only unknown is the system.
"""

from __future__ import annotations

import io
import json
import sys
import uuid

import chess.pgn
import pytest
from bench.throughput import BenchConfig, run_benchmark
from sqlalchemy import Engine

from chess_coach.positions import fen_hash
from tests.bench_support import create_workload

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

MS = 2
FAKE_WORKER = [
    sys.executable,
    "-m",
    "bench.fake_worker",
    "--worker-id",
    "{worker_id}",
    "--exit-when-empty",
    "--ms-per-eval",
    str(MS),
]


def positions_of(pgn: str) -> tuple[list[bytes], bytes]:
    """(the position before each ply, the final position), as a worker sees them."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    board = game.board()
    before = []
    for move in game.mainline_moves():
        before.append(fen_hash(board))
        board.push(move)
    return before, fen_hash(board)


def run(database_url, redis_url, redis_client, tmp_path, workers, **extra):
    prefix = f"benchtest:{uuid.uuid4().hex[:8]}:"
    config = BenchConfig(
        database_url=database_url,
        redis_url=redis_url,
        worker_cmd=FAKE_WORKER,
        workers=workers,
        games=6,
        key_prefix=prefix,
        timeout_sec=120,
        out_dir=tmp_path,
        **extra,
    )
    try:
        return run_benchmark(config)
    finally:
        keys = list(redis_client.scan_iter(match=prefix + "*"))
        if keys:
            redis_client.delete(*keys)


def test_a_two_round_benchmark_measures_the_workload_exactly(
    clean_engine: Engine, database_url, redis_url, redis_client, tmp_path
):
    _, pgns = create_workload(clean_engine)
    results, out = run(database_url, redis_url, redis_client, tmp_path, [1, 2])

    plies = 0
    all_positions: set[bytes] = set()
    ply_positions: set[bytes] = set()
    for pgn in pgns:
        before, final = positions_of(pgn)
        plies += len(before)
        ply_positions.update(before)
        all_positions.update(before)
        all_positions.add(final)
    lookups = plies + len(pgns)

    assert [r.workers for r in results] == [1, 2]
    for r in results:  # every round starts cold, so every round is identical work
        assert r.games_completed == 6 and not r.timed_out
        assert r.worker_exit_codes == [0] * r.workers
        assert r.plies == plies
        assert r.lookups == lookups
        assert r.engine_evaluations == len(all_positions)
        assert r.cache_hit_rate == pytest.approx(1 - len(all_positions) / lookups)
        assert r.engine_seconds_spent == pytest.approx(len(all_positions) * MS / 1000)
        assert r.engine_seconds_saved == pytest.approx((plies - len(ply_positions)) * MS / 1000)
        assert r.search == ["fake-1", "1"]
        assert r.games_per_minute and r.games_per_minute > 0
    assert results[0].cache_hit_rate > 0.1  # the shared openings really were reused

    saved = json.loads(out.read_text())
    assert [r["games_completed"] for r in saved["rounds"]] == [6, 6]
    assert list(tmp_path.glob("logs-*/workers-2-1.log"))


def test_the_no_cache_baseline_runs_too(
    clean_engine: Engine, database_url, redis_url, redis_client, tmp_path
):
    """POSITION_CACHE=off is the 'without cache' side of engine-seconds-saved;
    the worker must run with a cache that has only the EvalCache protocol."""
    create_workload(clean_engine)
    (result,), _ = run(
        database_url,
        redis_url,
        redis_client,
        tmp_path,
        [1],
        extra_env={"POSITION_CACHE": "off"},
    )
    assert result.games_completed == 6
    assert result.engine_evaluations == 0  # NullCache stores nothing...
    # ...so SQL cannot see the engine work. "0 evaluations" must not be read as
    # "100% hit rate" - the exact opposite of a no-cache run.
    assert result.cache_hit_rate is None
    assert result.search is None
    assert result.engine_seconds_saved == 0
