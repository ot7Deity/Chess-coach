"""The benchmark's moving parts: reset a workload, enqueue it, run N workers, measure.

`bench.throughput` is the command-line driver around these. Everything is
measured from the database afterwards - `games.analyzed_at`, `moves` rows and
`positions` rows - never from what the workers report about themselves. See
`bench.throughput` for the definitions of each number and the known bias.

Destructive by design: `reset_workload` resets games and can empty
`positions`, so callers check `check_database_is_disposable` first.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import redis
from sqlalchemy import Engine, text

from chess_coach.cache import PositionCache

REPO_ROOT = Path(__file__).resolve().parent.parent

# M2's job format, as M2 confirmed it: type 'analyze-game' (hyphen), payload
# {"game_id": <games.id>}, dedupe key 'analyze-game:<games.id>'. M2 owns it;
# every one of these is overridable from the command line.
DEFAULT_JOB_TYPE = "analyze-game"
DEFAULT_JOB_PAYLOAD = '{{"game_id": {game_id}}}'
DEFAULT_DEDUPE_KEY = "analyze-game:{game_id}"


class BenchError(RuntimeError):
    pass


@dataclass
class RoundResult:
    workers: int
    games_target: int
    games_completed: int
    wall_seconds: float | None
    games_per_minute: float | None
    plies: int
    plies_per_second: float | None
    lookups: int
    engine_evaluations: int
    cache_hit_rate: float | None
    engine_seconds_spent: float
    engine_seconds_without_cache: float
    engine_seconds_saved: float
    search: list[str] | None
    worker_exit_codes: list[int | None] = field(default_factory=list)
    timed_out: bool = False


# --- safety and setup ---------------------------------------------------------


def check_database_is_disposable(url: str) -> None:
    name = (urlsplit(url).path or "").lstrip("/")
    if "bench" not in name and "test" not in name:
        raise BenchError(
            f"refusing to benchmark against {name!r}: the run resets games and empties "
            "positions, so the database name must contain 'bench' or 'test'"
        )


def select_workload(engine: Engine, games: int, player: str | None) -> list[int]:
    query = (
        "SELECT g.id FROM games g JOIN players p ON p.id = g.player_id "
        "WHERE g.rules = 'chess' AND (CAST(:player AS text) IS NULL OR p.username = :player) "
        "ORDER BY g.played_at, g.id LIMIT :games"
    )
    with engine.connect() as conn:
        ids = list(
            conn.execute(
                text(query), {"player": player.lower() if player else None, "games": games}
            ).scalars()
        )
    if not ids:
        raise BenchError("no standard-chess games to benchmark with")
    return ids


def reset_workload(
    engine: Engine,
    game_ids: Sequence[int],
    *,
    cold_cache: bool,
    redis_client: redis.Redis | None,
    key_prefix: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM moves WHERE game_id = ANY(:ids)"), {"ids": list(game_ids)})
        conn.execute(
            text(
                "UPDATE games SET analysis_status = 'pending', analyzed_at = NULL, "
                "analysis_skip_reason = NULL WHERE id = ANY(:ids)"
            ),
            {"ids": list(game_ids)},
        )
        if cold_cache:
            conn.execute(text("DELETE FROM positions"))
    if cold_cache and redis_client is not None:
        PositionCache(engine, redis_client, key_prefix=key_prefix).purge_redis()


def enqueue(
    engine: Engine, game_ids: Sequence[int], job_type: str, payload_tpl: str, dedupe_tpl: str
) -> None:
    rows = []
    for game_id in game_ids:
        payload = json.loads(payload_tpl.format(game_id=game_id))
        rows.append(
            {
                "type": job_type,
                "payload": json.dumps(payload),
                "dedupe": dedupe_tpl.format(game_id=game_id),
            }
        )
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE dedupe_key = ANY(:keys)"),
            {"keys": [r["dedupe"] for r in rows]},
        )
        # Every NOT NULL column spelled out: priority, attempts and max_attempts
        # have ORM-side defaults only (models.Job), no server default, so a raw
        # INSERT that leaves them out fails.
        conn.execute(
            text(
                "INSERT INTO jobs (type, payload, status, priority, attempts, max_attempts, "
                "dedupe_key) VALUES (:type, CAST(:payload AS jsonb), 'pending', 100, 0, 5, "
                ":dedupe)"
            ),
            rows,
        )


# --- running ------------------------------------------------------------------


def expand_command(template: Sequence[str], *, n: int, index: int, metrics_port: int) -> list[str]:
    values = {
        "worker_id": f"bench-{n}-{index}",
        "index": index,
        "n": n,
        "metrics_port": metrics_port + index if metrics_port else 0,
    }
    cmd = [part.format(**values) for part in template]
    resolved = shutil.which(cmd[0])
    if resolved is None and not Path(cmd[0]).exists():
        raise BenchError(f"worker command not found: {cmd[0]!r}")
    return [resolved or cmd[0], *cmd[1:]]


def _db_now(engine: Engine) -> dt.datetime:
    with engine.connect() as conn:
        return conn.execute(text("SELECT clock_timestamp()")).scalar_one()


def _done_count(engine: Engine, game_ids: Sequence[int], since: dt.datetime) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM games WHERE id = ANY(:ids) AND analyzed_at >= :since"),
            {"ids": list(game_ids), "since": since},
        ).scalar_one()


def run_round(
    engine: Engine,
    game_ids: Sequence[int],
    n: int,
    worker_cmd: Sequence[str],
    *,
    env: dict[str, str],
    log_dir: Path,
    timeout_sec: float,
    metrics_port: int = 0,
    poll_sec: float = 0.5,
    cwd: Path = REPO_ROOT,
) -> tuple[dt.datetime, list[int | None], bool]:
    """Start n workers, wait for the workload, stop them. Returns (start, exit codes, timed out)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    started = _db_now(engine)
    procs: list[subprocess.Popen] = []
    logs = []
    try:
        for index in range(n):
            cmd = expand_command(worker_cmd, n=n, index=index, metrics_port=metrics_port)
            log = (log_dir / f"workers-{n}-{index}.log").open("wb")
            logs.append(log)
            procs.append(
                subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
            )

        deadline = time.monotonic() + timeout_sec
        timed_out = False
        while True:
            if _done_count(engine, game_ids, started) >= len(game_ids):
                break
            if all(p.poll() is not None for p in procs):
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(poll_sec)
    finally:
        for p in procs:
            if p.poll() is None:
                # Workers that exit on an empty queue get a moment to do so;
                # the workload is finished, so stragglers are just idle pollers.
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.terminate()
                    p.wait(timeout=10)
        for log in logs:
            log.close()
    return started, [p.returncode for p in procs], timed_out


# --- measuring ----------------------------------------------------------------


def measure(
    engine: Engine,
    game_ids: Sequence[int],
    n: int,
    started: dt.datetime,
    search: tuple[str, int] | None = None,
) -> RoundResult:
    ids = {"ids": list(game_ids), "since": started}
    with engine.connect() as conn:
        games_done, last_done = conn.execute(
            text(
                "SELECT count(*), max(analyzed_at) FROM games "
                "WHERE id = ANY(:ids) AND analyzed_at >= :since"
            ),
            ids,
        ).one()
        plies = conn.execute(
            text("SELECT count(*) FROM moves WHERE game_id = ANY(:ids)"), ids
        ).scalar_one()

        if search is None:
            groups = conn.execute(
                text(
                    "SELECT engine_version, depth FROM positions WHERE computed_at >= :since "
                    "GROUP BY 1, 2"
                ),
                ids,
            ).all()
            search = (groups[0][0], groups[0][1]) if len(groups) == 1 else None

        evals, spent_ms = conn.execute(
            text(
                "SELECT count(*), coalesce(sum(compute_ms), 0) FROM positions "
                "WHERE computed_at >= :since"
            ),
            ids,
        ).one()

        # Saved is computed over ply positions only, on both sides: what every
        # ply's position costs (as if nothing were cached) minus what was
        # actually spent this round on positions some ply refers to. A game's
        # final position has no moves row, so it is left out of both.
        without_ms = spent_on_plies_ms = 0
        if search is not None:
            params = {**ids, "engine": search[0], "depth": search[1]}
            without_ms = conn.execute(
                text(
                    "SELECT coalesce(sum(p.compute_ms), 0) FROM moves m "
                    "JOIN positions p ON p.fen_hash = m.fen_hash "
                    "AND p.engine_version = :engine AND p.depth = :depth "
                    "WHERE m.game_id = ANY(:ids)"
                ),
                params,
            ).scalar_one()
            spent_on_plies_ms = conn.execute(
                text(
                    "SELECT coalesce(sum(p.compute_ms), 0) FROM positions p "
                    "WHERE p.computed_at >= :since "
                    "AND p.engine_version = :engine AND p.depth = :depth "
                    "AND EXISTS (SELECT 1 FROM moves m "
                    "            WHERE m.fen_hash = p.fen_hash AND m.game_id = ANY(:ids))"
                ),
                params,
            ).scalar_one()

    wall = (last_done - started).total_seconds() if last_done is not None else None
    lookups = plies + games_done
    # The hit rate is "1 - evaluations / lookups" with evaluations counted as
    # new positions rows - which only works if the round's evaluations were
    # stored. With no cache (POSITION_CACHE=off) nothing is stored, and 0 rows
    # would read as a 100% hit rate. So no identifiable search this round (no
    # new rows, or a mix of engines/depths) means no hit rate: unknown, not 1.
    hit_rate = 1 - evals / lookups if lookups and search is not None else None
    return RoundResult(
        workers=n,
        games_target=len(game_ids),
        games_completed=games_done,
        wall_seconds=wall,
        games_per_minute=games_done / wall * 60 if wall else None,
        plies=plies,
        plies_per_second=plies / wall if wall else None,
        lookups=lookups,
        engine_evaluations=evals,
        cache_hit_rate=hit_rate,
        engine_seconds_spent=spent_ms / 1000,
        engine_seconds_without_cache=without_ms / 1000,
        engine_seconds_saved=max(0, without_ms - spent_on_plies_ms) / 1000,
        search=[search[0], str(search[1])] if search else None,
    )
