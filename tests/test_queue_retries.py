"""Retries, dead-lettering and the reaper - and the zombie worker they create.

The reaper is what makes a crashed worker survivable, and also what creates the
hardest case in the queue: a worker that was reaped but is not actually dead.
Those tests are at the bottom.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.queue import Lease, LeaseLost

T = ["analyze-game"]


def _enqueue(engine: Engine, *, max_attempts: int = 5) -> int:
    with Session(engine) as s, s.begin():
        return queue.enqueue(
            s, "analyze-game", {"game_id": 1}, dedupe_key="g:1", max_attempts=max_attempts
        )


def _claim(engine: Engine, worker: str = "w1") -> Lease:
    with Session(engine) as s, s.begin():
        lease = queue.claim(s, worker, T)
    assert lease is not None
    return lease


def _fail(engine: Engine, lease: Lease, **kw) -> str:
    with Session(engine) as s, s.begin():
        return queue.fail(s, lease, "boom", retry_in_sec=kw.pop("retry_in_sec", 0), **kw)


def _reap(engine: Engine, timeout: float = 60) -> list[queue.ReapedJob]:
    with Session(engine) as s, s.begin():
        return queue.reap_expired(s, timeout)


def _expire(engine: Engine) -> None:
    """Make every running lease look abandoned."""
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))


def _job(engine: Engine, job_id: int):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


# --- retry backoff (pure) ---------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "low", "high"),
    [(1, 15, 30), (2, 30, 60), (3, 60, 120), (10, 900, 1800), (0, 15, 30)],
)
def test_retry_delay_doubles_with_equal_jitter_and_a_cap(attempt, low, high):
    """Half the window is fixed and half random, so a retry is never immediate
    but jobs that failed together spread out."""
    kw = dict(base_sec=30, cap_sec=1800)
    assert queue.retry_delay(attempt, rng=lambda: 0.0, **kw) == low
    assert queue.retry_delay(attempt, rng=lambda: 1.0, **kw) == high


# --- fail -------------------------------------------------------------------


@pytest.mark.postgres
def test_a_failed_attempt_goes_back_to_the_queue_later(clean_engine: Engine):
    job_id = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    assert _fail(clean_engine, lease, retry_in_sec=120) == "pending"

    row = _job(clean_engine, job_id)
    assert (row.status, row.attempts, row.locked_by, row.locked_at) == ("pending", 1, None, None)
    assert row.last_error == "boom"
    with clean_engine.connect() as c:
        wait = c.scalar(
            text("SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
    assert 100 < wait <= 120

    with Session(clean_engine) as s, s.begin():
        assert queue.claim(s, "w2", T) is None, "claimed before its backoff ran out"
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now()"))
    assert _claim(clean_engine, "w2").attempt == 2


@pytest.mark.postgres
def test_the_last_allowed_attempt_dead_letters_the_job(clean_engine: Engine):
    job_id = _enqueue(clean_engine, max_attempts=2)
    assert _fail(clean_engine, _claim(clean_engine)) == "pending"
    assert _fail(clean_engine, _claim(clean_engine)) == "dead"

    row = _job(clean_engine, job_id)
    assert (row.status, row.attempts) == ("dead", 2)
    assert row.finished_at is not None
    assert row.locked_by == "w1", "the last worker is kept for the post-mortem"
    with Session(clean_engine) as s, s.begin():
        assert queue.claim(s, "w1", T) is None


@pytest.mark.postgres
def test_a_permanent_failure_is_not_retried(clean_engine: Engine):
    """A corrupt PGN is just as corrupt in 30 seconds. Retrying it four more
    times spends engine time and delays the dead-letter signal."""
    job_id = _enqueue(clean_engine)
    assert _fail(clean_engine, _claim(clean_engine), permanent=True) == "failed"
    row = _job(clean_engine, job_id)
    assert (row.status, row.attempts) == ("failed", 1)
    assert row.finished_at is not None


@pytest.mark.postgres
def test_fail_with_a_stale_lease_changes_nothing(clean_engine: Engine):
    job_id = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    stale = Lease(**{**lease.__dict__, "attempt": 0})
    with pytest.raises(LeaseLost):
        _fail(clean_engine, stale)
    assert _job(clean_engine, job_id).status == "running"


# --- reaper -----------------------------------------------------------------


@pytest.mark.postgres
def test_the_reaper_frees_a_job_whose_worker_stopped_heartbeating(clean_engine: Engine):
    job_id = _enqueue(clean_engine)
    _claim(clean_engine, "doomed")
    assert _reap(clean_engine) == [], "a fresh lease must not be reaped"

    _expire(clean_engine)
    (reaped,) = _reap(clean_engine)
    assert (reaped.job_id, reaped.status, reaped.payload) == (job_id, "pending", {"game_id": 1})

    row = _job(clean_engine, job_id)
    assert (row.status, row.locked_by, row.attempts) == ("pending", None, 1)
    assert "doomed" in row.last_error
    # Immediately claimable: the lock timeout it sat out already was a backoff.
    assert _claim(clean_engine, "rescuer").attempt == 2


@pytest.mark.postgres
def test_a_job_that_keeps_killing_workers_is_dead_lettered(clean_engine: Engine):
    """A reaped attempt still counts. Otherwise a job that crashes every worker
    that touches it would take the pool down one worker at a time, forever."""
    job_id = _enqueue(clean_engine, max_attempts=2)
    for expected in ("pending", "dead"):
        _claim(clean_engine)
        _expire(clean_engine)
        (reaped,) = _reap(clean_engine)
        assert reaped.status == expected
    assert _job(clean_engine, job_id).status == "dead"


@pytest.mark.postgres
def test_the_reaper_leaves_finished_and_pending_jobs_alone(clean_engine: Engine):
    _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, lease)
    _expire(clean_engine)
    assert _reap(clean_engine) == []


@pytest.mark.postgres
def test_the_reaper_skips_a_job_that_is_completing_right_now(clean_engine: Engine):
    """The completion transaction holds the job's row lock until it commits.
    The reaper must walk past it (SKIP LOCKED) rather than wait and then reap a
    job that has just finished - or block every other reap behind it."""
    job_id = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    _expire(clean_engine)

    with Session(clean_engine) as completing, completing.begin():
        queue.complete(completing, lease)
        with Session(clean_engine) as reaper, reaper.begin():
            reaper.execute(text("SET LOCAL lock_timeout = '500ms'"))
            assert queue.reap_expired(reaper, 60) == []
    assert _job(clean_engine, job_id).status == "done"


# --- the zombie worker ------------------------------------------------------


@pytest.mark.postgres
def test_a_reaped_worker_that_comes_back_cannot_finish_the_job(clean_engine: Engine):
    """Worker A stalls (GC pause, laptop lid, network partition), is reaped,
    and B takes the job. When A wakes up it still believes it owns the job.
    Every write A attempts must be refused; B's must succeed."""
    job_id = _enqueue(clean_engine)
    a = _claim(clean_engine, "A")
    _expire(clean_engine)
    _reap(clean_engine)
    b = _claim(clean_engine, "B")

    for zombie_write in (queue.heartbeat, queue.complete):
        with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
            zombie_write(s, a)
    with pytest.raises(LeaseLost):
        _fail(clean_engine, a)

    with Session(clean_engine) as s, s.begin():
        queue.complete(s, b)
    row = _job(clean_engine, job_id)
    assert (row.status, row.locked_by, row.attempts) == ("done", "B", 2)


@pytest.mark.postgres
def test_a_reaped_worker_cannot_finish_the_job_before_anyone_reclaims_it(
    clean_engine: Engine,
):
    """The gap between reap and re-claim: the job is pending and unowned. A's
    late completion must not mark it done - nobody has produced its results
    under a valid lease."""
    job_id = _enqueue(clean_engine)
    a = _claim(clean_engine, "A")
    _expire(clean_engine)
    _reap(clean_engine)
    with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
        queue.complete(s, a)
    assert _job(clean_engine, job_id).status == "pending"


@pytest.mark.postgres
def test_a_worker_restarted_under_the_same_name_fences_out_its_own_zombie(
    clean_engine: Engine,
):
    """Benchmarks restart 'worker-1' as 'worker-1'. If the old process is still
    alive, locked_by matches for both - only the attempt number tells them apart."""
    _enqueue(clean_engine)
    old = _claim(clean_engine, "worker-1")
    _expire(clean_engine)
    _reap(clean_engine)
    new = _claim(clean_engine, "worker-1")
    assert (old.worker_id, old.attempt, new.attempt) == ("worker-1", 1, 2)

    with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
        queue.complete(s, old)
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, new)
