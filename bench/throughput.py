"""Worker-scaling benchmark: the same workload at 1, 2, 4 and 8 workers.

    uv run python -m bench.throughput --games 200 --workers 1 2 4 8 \\
        -- chess-coach-worker --worker-id {worker_id} --until-empty

Everything after `--` is the worker command, one process per worker, with
these placeholders filled in per process: {worker_id} (bench-<n>-<i>), {index}
(0-based), {n} (workers in this round) and {metrics_port} (`--metrics-port`
plus index, or 0). It defaults to M2's `chess-coach-worker --worker-id
{worker_id} --until-empty`. Do not add `--enqueue-pending`: the harness
enqueues exactly the workload, and a worker enqueueing every pending game
would grow the workload mid-round.

Each round:

1. resets the workload - the same `--games` games every round - to unanalysed,
   deletes their moves, and (with `--cache cold`, the default) empties the
   position cache in Postgres and Redis, so every round starts equally cold;
2. enqueues one job per game (`--job-type`, `--job-payload`, `--dedupe-key`
   templates, or `--no-enqueue` if something else does it);
3. starts N workers and waits until every workload game has `analyzed_at`, or
   every worker has exited, or `--timeout`;
4. measures **from the database**, not from what the workers report:

   * games/minute: workload games finished / (last `analyzed_at` - start);
   * plies/second: `moves` rows written / the same window;
   * engine evaluations: `positions` rows computed since the round started;
   * cache hit rate: 1 - engine evaluations / lookups, where lookups = plies +
     one final position per game. Reported only when the round stored its
     evaluations under one (engine version, depth); with POSITION_CACHE=off
     nothing is stored and the hit rate is unknown ("-"), not 100%;
   * engine seconds spent: sum of those new rows' `compute_ms`;
   * engine seconds saved: sum of `compute_ms` over every ply's position (what
     the plies would have cost with no cache) minus what this round spent on
     those same positions. Final positions (no `moves` row) are left out of
     both sides, so a hit on one is not counted - a slight undercount.

   Known bias: when two workers evaluate one position at the same moment, only
   the winner's row exists, so the loser's engine time is invisible here and
   the hit rate reads slightly high at large N. The workers' own Prometheus
   counters (`chess_coach_positions_evaluated_total`) count both.

Destructive by design (it resets games and empties `positions`), so it refuses
any database whose name does not contain "bench" or "test". Make one from real
data with, for example: `CREATE DATABASE chess_coach_bench TEMPLATE chess_coach`
(with nothing else connected to chess_coach). Results go to bench/results/ as
JSON (gitignored), and a markdown table is printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import redis
from sqlalchemy import create_engine

from bench.harness import (
    DEFAULT_DEDUPE_KEY,
    DEFAULT_JOB_PAYLOAD,
    DEFAULT_JOB_TYPE,
    REPO_ROOT,
    BenchError,
    RoundResult,
    check_database_is_disposable,
    enqueue,
    measure,
    reset_workload,
    run_round,
    select_workload,
)

RESULTS_DIR = REPO_ROOT / "bench" / "results"
DEFAULT_WORKER_CMD = ["chess-coach-worker", "--worker-id", "{worker_id}", "--until-empty"]

__all__ = [
    "RESULTS_DIR",
    "BenchConfig",
    "BenchError",
    "RoundResult",
    "check_database_is_disposable",
    "main",
    "markdown_table",
    "run_benchmark",
]


def markdown_table(rows: Sequence[RoundResult]) -> str:
    def fmt(value, spec):
        return "-" if value is None else format(value, spec)

    base = rows[0].games_per_minute if rows and rows[0].games_per_minute else None
    lines = [
        "| Workers | Games | Wall s | Games/min | Speedup | Plies/s | Engine evals "
        "| Hit rate | Engine s spent | Engine s saved |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        speedup = r.games_per_minute / base if base and r.games_per_minute else None
        hit_pct = None if r.cache_hit_rate is None else r.cache_hit_rate * 100
        lines.append(
            f"| {r.workers} | {r.games_completed}/{r.games_target} | {fmt(r.wall_seconds, '.1f')} "
            f"| {fmt(r.games_per_minute, '.1f')} | {fmt(speedup, '.2f')}x "
            f"| {fmt(r.plies_per_second, '.1f')} | {r.engine_evaluations} "
            f"| {fmt(hit_pct, '.1f')}% "
            f"| {r.engine_seconds_spent:.1f} | {r.engine_seconds_saved:.1f} |"
        )
    return "\n".join(lines)


# --- driver -------------------------------------------------------------------


@dataclass
class BenchConfig:
    database_url: str
    redis_url: str | None
    worker_cmd: list[str]
    workers: list[int]
    games: int
    player: str | None = None
    cold_cache: bool = True
    key_prefix: str = "chess_coach:pos:v1:"
    enqueue_jobs: bool = True
    job_type: str = DEFAULT_JOB_TYPE
    job_payload: str = DEFAULT_JOB_PAYLOAD
    dedupe_key: str = DEFAULT_DEDUPE_KEY
    timeout_sec: float = 3600.0
    metrics_port: int = 0
    out_dir: Path = RESULTS_DIR
    extra_env: dict[str, str] = field(default_factory=dict)


def run_benchmark(config: BenchConfig) -> tuple[list[RoundResult], Path]:
    check_database_is_disposable(config.database_url)
    engine = create_engine(config.database_url)
    redis_client = redis.Redis.from_url(config.redis_url) if config.redis_url else None
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    log_dir = config.out_dir / f"logs-{stamp}"
    env = {
        **os.environ,
        "DATABASE_URL": config.database_url,
        "POSITION_CACHE_KEY_PREFIX": config.key_prefix,
        **({"REDIS_URL": config.redis_url} if config.redis_url else {}),
        **config.extra_env,
    }
    try:
        game_ids = select_workload(engine, config.games, config.player)
        results = []
        for n in config.workers:
            reset_workload(
                engine,
                game_ids,
                cold_cache=config.cold_cache,
                redis_client=redis_client,
                key_prefix=config.key_prefix,
            )
            if config.enqueue_jobs:
                enqueue(engine, game_ids, config.job_type, config.job_payload, config.dedupe_key)
            started, codes, timed_out = run_round(
                engine,
                game_ids,
                n,
                config.worker_cmd,
                env=env,
                log_dir=log_dir,
                timeout_sec=config.timeout_sec,
                metrics_port=config.metrics_port,
            )
            result = measure(engine, game_ids, n, started)
            result.worker_exit_codes = codes
            result.timed_out = timed_out
            results.append(result)
    finally:
        engine.dispose()
        if redis_client is not None:
            redis_client.close()

    config.out_dir.mkdir(parents=True, exist_ok=True)
    out = config.out_dir / f"throughput-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "created_at": stamp,
                "git_commit": _git_commit(),
                "host": {
                    "platform": platform.platform(),
                    "processor": platform.processor(),
                    "cpu_count": os.cpu_count(),
                    "python": sys.version.split()[0],
                },
                "config": {
                    "games": len(game_ids),
                    "player": config.player,
                    "cold_cache": config.cold_cache,
                    "worker_cmd": config.worker_cmd,
                    "workers": config.workers,
                },
                "rounds": [asdict(r) for r in results],
            },
            indent=2,
            default=str,
        )
    )
    return results, out


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    worker_cmd = DEFAULT_WORKER_CMD
    if "--" in argv:
        split = argv.index("--")
        argv, worker_cmd = argv[:split], argv[split + 1 :]

    parser = argparse.ArgumentParser(prog="bench.throughput", description=__doc__.split("\n")[0])
    parser.add_argument("--games", type=int, default=200)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--player", help="only this player's games")
    parser.add_argument("--cache", choices=["cold", "keep"], default="cold")
    parser.add_argument("--no-enqueue", action="store_true")
    parser.add_argument("--job-type", default=DEFAULT_JOB_TYPE)
    parser.add_argument("--job-payload", default=DEFAULT_JOB_PAYLOAD)
    parser.add_argument("--dedupe-key", default=DEFAULT_DEDUPE_KEY)
    parser.add_argument("--timeout", type=float, default=3600.0, help="seconds per round")
    parser.add_argument("--metrics-port", type=int, default=0)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    from chess_coach.config import get_settings

    settings = get_settings()
    config = BenchConfig(
        database_url=settings.database_url,
        redis_url=settings.redis_url if settings.position_cache == "full" else None,
        worker_cmd=worker_cmd,
        workers=args.workers,
        games=args.games,
        player=args.player,
        cold_cache=args.cache == "cold",
        key_prefix=settings.position_cache_key_prefix,
        enqueue_jobs=not args.no_enqueue,
        job_type=args.job_type,
        job_payload=args.job_payload,
        dedupe_key=args.dedupe_key,
        timeout_sec=args.timeout,
        metrics_port=args.metrics_port,
        out_dir=args.out,
    )
    try:
        results, out = run_benchmark(config)
    except BenchError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2
    print(markdown_table(results))
    print(f"\nwrote {out}")
    return 0 if all(r.games_completed == r.games_target for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
