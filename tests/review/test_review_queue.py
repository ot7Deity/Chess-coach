"""Try to break the M2 job queue with real processes and real kills.

Skips on branches without chess_coach.queue (M2's m2/* branches add it).

"Exactly one complete analysis" is measured with a stand-in results table,
review_results, written in the same transaction as complete() - the pattern
queue.py prescribes for the analysis worker. If the queue's fencing is right,
every job ends with exactly one result row, however workers die.
"""

from __future__ import annotations

import multiprocessing
import time
from collections import Counter
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.review import _procs

queue = pytest.importorskip("chess_coach.queue")

pytestmark = pytest.mark.postgres

T = ["analyze-game"]


@pytest.fixture
def results(clean_engine: Engine) -> Iterator[Engine]:
    with clean_engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS review_results"))
        c.execute(text(_procs.RESULTS_DDL))
    yield clean_engine
    with clean_engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS review_results"))


def _enqueue(engine: Engine, n: int) -> list[int]:
    with Session(engine) as s, s.begin():
        return [
            queue.enqueue(s, "analyze-game", {"game_id": i}, dedupe_key=f"r:{i}") for i in range(n)
        ]


def _result_counts(engine: Engine) -> Counter[int]:
    with engine.connect() as c:
        return Counter(c.scalars(text("SELECT job_id FROM review_results")))


def _job(engine: Engine, job_id: int):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


def _expire_when_unlocked(engine: Engine, job_id: int, timeout: float = 20) -> None:
    """Backdate the lease once the dead worker's row lock has been released.

    A killed process's connection is closed by the OS; Postgres notices and
    aborts its transaction, but not instantly. lock_timeout turns "still
    locked" into an error we can retry instead of a hang."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.begin() as c:
                c.execute(text("SET LOCAL lock_timeout = '200ms'"))
                c.execute(
                    text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
                    {"id": job_id},
                )
            return
        except OperationalError:
            if time.monotonic() > deadline:
                raise


# --- real processes -----------------------------------------------------------


def test_four_worker_processes_never_share_a_job(results: Engine, database_url: str):
    """M2's own race test uses threads in one process. These are separate
    processes with separate connection pools, as production runs them."""
    jobs = _enqueue(results, 120)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        per_worker = pool.starmap(
            _procs.drain_queue, [(database_url, f"proc{i}", T) for i in range(4)]
        )
    claimed = Counter(j for worker in per_worker for j in worker)
    assert set(claimed) == set(jobs)
    assert max(claimed.values()) == 1
    assert _result_counts(results) == Counter({j: 1 for j in jobs})
    assert sum(1 for w in per_worker if w) >= 2, "only one process did any work"


@pytest.mark.parametrize("phase", ["computing", "completing"])
def test_a_hard_killed_worker_still_ends_in_exactly_one_result(
    results: Engine, database_url: str, phase: str
):
    """The M2 'done when': kill a worker mid-job (TerminateProcess, no cleanup,
    no finally blocks) and the job still ends with exactly one analysis."""
    (job_id,) = _enqueue(results, 1)
    ctx = multiprocessing.get_context("spawn")
    inside = ctx.Event()
    victim = ctx.Process(
        target=_procs.claim_then_hang, args=(database_url, "victim", T, inside, phase)
    )
    victim.start()
    try:
        assert inside.wait(60), "victim never reached the kill point"
        victim.kill()
        victim.join(10)
    finally:
        if victim.is_alive():
            victim.kill()

    _expire_when_unlocked(results, job_id)
    with Session(results) as s, s.begin():
        reaped = queue.reap_expired(s, 60)
    assert [r.job_id for r in reaped] == [job_id]

    assert _procs.drain_queue(database_url, "survivor", T) == [job_id]
    assert _result_counts(results) == Counter({job_id: 1})
    row = _job(results, job_id)
    assert (row.status, row.attempts) == ("done", 2)


# --- fencing under real lock contention ---------------------------------------


def test_a_zombie_blocked_behind_the_reaper_writes_nothing(results: Engine):
    """The tight ordering: the reaper has locked the row but not committed when
    the zombie calls complete(). The zombie's UPDATE waits on the row lock,
    then re-checks its WHERE against the reaper's committed version - and must
    match zero rows. (A test that reaps and commits first never exercises the
    re-check.)"""
    import threading

    (job_id,) = _enqueue(results, 1)
    with Session(results) as s, s.begin():
        lease = queue.claim(s, "zombie", T)
    with results.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))

    outcome: list[object] = []
    started = threading.Event()

    def zombie_completes() -> None:
        try:
            with Session(results) as s, s.begin():
                started.set()
                queue.complete(s, lease)  # blocks on the reaper's row lock
                s.execute(text("INSERT INTO review_results VALUES (:j, 'zombie')"), {"j": job_id})
            outcome.append("completed")
        except BaseException as exc:
            outcome.append(exc)

    with Session(results) as reaper:
        reaper.begin()
        assert [r.job_id for r in queue.reap_expired(reaper, 60)] == [job_id]
        t = threading.Thread(target=zombie_completes)
        t.start()
        assert started.wait(5)
        time.sleep(0.5)  # let the zombie's UPDATE reach the lock wait
        assert t.is_alive(), "complete() did not wait for the reaper's lock"
        reaper.commit()
    t.join(10)

    assert len(outcome) == 1 and isinstance(outcome[0], queue.LeaseLost), outcome
    assert _result_counts(results) == Counter()
    assert _job(results, job_id).status == "pending"


def test_two_reapers_at_once_reap_each_job_once(results: Engine):
    ids = _enqueue(results, 5)
    for _ in ids:
        with Session(results) as s, s.begin():
            queue.claim(s, "w", T)
    with results.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))

    with Session(results) as a, Session(results) as b:
        a.begin()
        b.begin()
        first = queue.reap_expired(a, 60)
        second = queue.reap_expired(b, 60)  # SKIP LOCKED: must not block or double-reap
        a.commit()
        b.commit()
    assert sorted(r.job_id for r in first) == sorted(ids)
    assert second == []


def test_a_lease_that_expires_mid_job_and_is_heartbeated_before_the_reaper_survives(
    results: Engine,
):
    """Expiry is a reaper decision, not a clock: a slow worker that heartbeats
    late but before any reaper ran keeps its job."""
    (job_id,) = _enqueue(results, 1)
    with Session(results) as s, s.begin():
        lease = queue.claim(s, "slow", T)
    with results.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    with Session(results) as s, s.begin():
        queue.heartbeat(s, lease)
    with Session(results) as s, s.begin():
        assert queue.reap_expired(s, 60) == []
    with Session(results) as s, s.begin():
        queue.complete(s, lease)
    assert _job(results, job_id).status == "done"


# --- retries ------------------------------------------------------------------


def test_max_attempts_means_that_many_runs_in_total(results: Engine):
    """'dead-letter after 5 attempts': five claims happen, the sixth does not."""
    (job_id,) = _enqueue(results, 1)
    claims = 0
    while True:
        with Session(results) as s, s.begin():
            lease = queue.claim(s, "w", T)
        if lease is None:
            break
        claims += 1
        with Session(results) as s, s.begin():
            queue.fail(s, lease, "boom", retry_in_sec=0)
    assert claims == 5
    row = _job(results, job_id)
    assert (row.status, row.attempts) == ("dead", 5)


def test_a_fractional_retry_delay_is_honoured(results: Engine):
    (job_id,) = _enqueue(results, 1)
    with Session(results) as s, s.begin():
        lease = queue.claim(s, "w", T)
    with Session(results) as s, s.begin():
        queue.fail(s, lease, "boom", retry_in_sec=queue.retry_delay(1, base_sec=1.5, cap_sec=10))
    with results.connect() as c:
        delay = c.scalar(
            text("SELECT extract(epoch FROM run_after - updated_at) FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
    assert 0.75 <= float(delay) <= 1.5


def test_a_huge_error_message_is_truncated_not_fatal(results: Engine):
    (job_id,) = _enqueue(results, 1)
    with Session(results) as s, s.begin():
        lease = queue.claim(s, "w", T)
    with Session(results) as s, s.begin():
        queue.fail(s, lease, "x" * 1_000_000, retry_in_sec=1)
    assert len(_job(results, job_id).last_error) <= 2000
