"""Entry points for tests that need real separate processes.

They live in an importable module, not a test file, because Windows starts
child processes with `spawn`, which re-imports the target by module path.
"""

from __future__ import annotations

import time

RESULTS_DDL = (
    "CREATE TABLE IF NOT EXISTS review_results (job_id int NOT NULL, worker text NOT NULL)"
)


def drain_queue(database_url: str, worker_id: str, types: list[str]) -> list[int]:
    """Claim and complete jobs until none are left, writing one result row per
    job in the same transaction as complete(), as queue.py prescribes."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from chess_coach import queue

    engine = create_engine(database_url)
    done: list[int] = []
    try:
        while True:
            with Session(engine) as s, s.begin():
                lease = queue.claim(s, worker_id, types)
            if lease is None:
                return done
            with Session(engine) as s, s.begin():
                queue.complete(s, lease)
                s.execute(
                    text("INSERT INTO review_results VALUES (:j, :w)"),
                    {"j": lease.job_id, "w": worker_id},
                )
            done.append(lease.job_id)
    finally:
        engine.dispose()


def claim_then_hang(
    database_url: str, worker_id: str, types: list[str], inside, phase: str
) -> None:
    """Claim one job, then hang forever at `phase` until the parent kills us:

    * 'computing': lease committed, nothing written yet (killed mid-analysis).
    * 'completing': inside the complete() transaction with a result row
      written but not committed (killed mid-commit).
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from chess_coach import queue

    engine = create_engine(database_url)
    with Session(engine) as s, s.begin():
        lease = queue.claim(s, worker_id, types)
    assert lease is not None
    if phase == "computing":
        inside.set()
        time.sleep(600)
    session = Session(engine)
    session.begin()
    queue.complete(session, lease)
    session.execute(
        text("INSERT INTO review_results VALUES (:j, :w)"), {"j": lease.job_id, "w": worker_id}
    )
    session.flush()
    inside.set()
    time.sleep(600)


def acquire_tokens(redis_url: str, key: str, rate: float, burst: int, n: int) -> list[float]:
    """Acquire `n` tokens from a shared bucket; return wall-clock send times."""
    import redis

    from chess_coach.ratelimit import RedisTokenBucket

    client = redis.Redis.from_url(redis_url)
    try:
        bucket = RedisTokenBucket(client, key=key, rate_per_sec=rate, burst=burst)
        stamps = []
        for _ in range(n):
            bucket.acquire()
            stamps.append(time.time())
        return stamps
    finally:
        client.close()
