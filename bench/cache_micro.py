"""Position-cache microbenchmarks: what one lookup costs in each layer.

    uv run python -m bench.cache_micro

Uses DATABASE_URL / REDIS_URL (the database name must contain "bench" or
"test"). Writes rows under a throwaway engine version and keys under a
throwaway prefix, and deletes both afterwards. Prints a table and writes JSON
to bench/results/.

Measured, as medians over repeats:

* one `get` served by Redis, by Postgres (Redis-less cache), and a miss;
* one game's worth of positions (80) via `get_many` versus 80 single `get`s,
  from Redis and from Postgres;
* one `put`;
* Redis memory per entry (`MEMORY USAGE`);
* a lookup while Redis is unreachable, with the breaker versus without it.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import socket
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import chess
import redis
from sqlalchemy import create_engine, text

from bench.harness import check_database_is_disposable
from bench.throughput import RESULTS_DIR
from chess_coach.cache import PositionCache, cache_redis_client
from chess_coach.config import Settings, get_settings
from chess_coach.positions import Evaluation, fen_hash, normalize_fen

GAME = 80
EVAL = Evaluation(eval_cp=17, mate_in=None, best_move="e2e4", nodes=1_000_000, compute_ms=400)


def positions(count: int, seed: int) -> list[tuple[bytes, str]]:
    rng = random.Random(seed)
    board = chess.Board()
    out: dict[bytes, str] = {}
    while len(out) < count:
        if board.is_game_over():
            board = chess.Board()
        board.push(rng.choice(sorted(board.legal_moves, key=lambda m: m.uci())))
        out[fen_hash(board)] = normalize_fen(board)
    return list(out.items())


def median_ms(fn: Callable[[], object], repeats: int) -> float:
    fn()  # warm the connection pools
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main() -> int:
    settings = get_settings()
    check_database_is_disposable(settings.database_url)
    engine = create_engine(settings.database_url)
    client = redis.Redis.from_url(settings.redis_url)
    tag = uuid.uuid4().hex[:8]
    version, depth = f"micro-{tag}", 16
    prefix = f"bench:micro:{tag}:"
    full = PositionCache(engine, client, key_prefix=prefix)
    pg_only = PositionCache(engine, None, key_prefix=prefix)
    game = positions(GAME, seed=1)
    keys = [k for k, _ in game]
    absent = positions(GAME, seed=2)
    results: dict[str, float] = {}
    try:
        for key, fen in game:
            full.put(key, fen, version, depth, EVAL)
        one, _ = game[0]
        results["get_redis_hit_ms"] = median_ms(lambda: full.get(one, version, depth), 200)
        results["get_postgres_hit_ms"] = median_ms(lambda: pg_only.get(one, version, depth), 200)
        results["get_miss_ms"] = median_ms(lambda: full.get(absent[0][0], version, depth), 200)
        results["game_80_singles_redis_ms"] = median_ms(
            lambda: [full.get(k, version, depth) for k in keys], 20
        )
        results["game_80_batch_redis_ms"] = median_ms(
            lambda: full.get_many(keys, version, depth), 50
        )
        results["game_80_singles_postgres_ms"] = median_ms(
            lambda: [pg_only.get(k, version, depth) for k in keys], 20
        )
        results["game_80_batch_postgres_ms"] = median_ms(
            lambda: pg_only.get_many(keys, version, depth), 50
        )

        fresh = iter(positions(400, seed=3))

        def put_one() -> None:
            key, fen = next(fresh)
            full.put(key, fen, version, depth, EVAL)

        results["put_ms"] = median_ms(put_one, 200)
        name = full.redis_name(one, version, depth)
        results["redis_bytes_per_entry"] = float(client.memory_usage(name, samples=0) or 0)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        dead_settings = Settings(redis_url=f"redis://127.0.0.1:{port}/0")
        dead = cache_redis_client(dead_settings)
        breaker = PositionCache(engine, dead, key_prefix=prefix, redis_retry_sec=30)
        no_breaker = PositionCache(engine, dead, key_prefix=prefix, redis_retry_sec=0)
        results["get_redis_down_with_breaker_ms"] = median_ms(
            lambda: breaker.get(one, version, depth), 20
        )
        results["get_redis_down_without_breaker_ms"] = median_ms(
            lambda: no_breaker.get(one, version, depth), 5
        )
        dead.close()
    finally:
        full.purge_redis()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM positions WHERE engine_version = :v"), {"v": version})
        engine.dispose()
        client.close()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(RESULTS_DIR) / f"cache-micro-{stamp}.json"
    out.write_text(json.dumps({"created_at": stamp, "results": results}, indent=2))
    print("| Measurement | Value |\n| --- | ---: |")
    for name, value in results.items():
        print(f"| {name} | {value:.3f} |")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
