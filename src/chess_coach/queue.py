"""Postgres as the job queue.

A job's life:

    pending --claim--> running --complete--> done
                          |
                          +--fail--> pending   attempts left; run_after pushed back
                          |      +-> dead      attempts exhausted (dead letter)
                          |      +-> failed    permanent: retrying cannot help
                          |
                          +--stops heartbeating, reaper--> pending, or dead

Three rules make "each job's effect happens exactly once" true even though a
job can be *executed* more than once:

1. **Claiming is `FOR UPDATE SKIP LOCKED`.** Concurrent claimers each lock a
   different row and walk past rows another claimer holds, so one row goes to
   exactly one worker and nobody blocks.

2. **A claim is a lease, and the lease has a fencing token.** Every claim
   increments `attempts`, so (job id, worker id, attempts) names one specific
   claim. A worker that is reaped while still alive - stalled, paused, on the
   wrong side of a network blip - still holds a stale token, and every write
   it attempts afterwards (heartbeat, complete, fail) is conditional on the
   token and matches zero rows. `locked_by` alone would not be enough: a worker
   restarted under the same id can legitimately re-claim the job it was reaped
   from, and then the zombie's `locked_by` would match. `attempts` cannot.

3. **Completion is fenced and atomic with the results.** `complete()` is meant
   to run in the same transaction that writes the job's output. It locks the
   job row and verifies the lease first, so either the results and `done` commit
   together, or neither does.

Time always comes from the database's `now()`, never a worker's clock, so a
worker with a skewed clock cannot make a lease look fresh or expired.

This module starts with claiming and completion; retries, dead-lettering and
the reaper build on the same lease.

Functions here take a `Session` and never commit: the caller owns the
transaction, which is what lets `complete()` share one with the results. A claim
should be committed immediately; holding it open holds the row lock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from chess_coach.models import Job


class LeaseLost(Exception):
    """This worker no longer owns the job: it was reaped, and maybe re-claimed.

    Whatever the worker computed must be discarded, not written. The job's new
    owner (or the next claimer) will produce it.
    """


@dataclass(frozen=True)
class Lease:
    """One claim of one job by one worker."""

    job_id: int
    type: str
    payload: dict[str, Any]
    worker_id: str
    # jobs.attempts as set by this claim. The fencing token.
    attempt: int
    max_attempts: int


# The predicate every write by a lease holder is conditional on.
_OWNED = "id = :job_id AND status = 'running' AND locked_by = :worker_id AND attempts = :attempt"


def _lease_params(lease: Lease) -> dict[str, Any]:
    return {"job_id": lease.job_id, "worker_id": lease.worker_id, "attempt": lease.attempt}


def enqueue(
    session: Session,
    type: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    priority: int = 100,
    max_attempts: int = 5,
) -> int | None:
    """Add a job. Returns its id, or None if `dedupe_key` is already queued.

    Idempotent on `dedupe_key`: enqueueing the same work twice is a no-op,
    whatever state the first job is in. That includes `done` - re-running
    finished work is a deliberate act, not something a retried enqueue should
    do by accident.
    """
    stmt = (
        insert(Job)
        .values(
            type=type,
            payload=payload,
            status="pending",
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
        .returning(Job.id)
    )
    return session.execute(stmt).scalar_one_or_none()


_CLAIM = text(
    """
    UPDATE jobs
    SET status = 'running',
        locked_by = :worker_id,
        locked_at = now(),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        -- Matches the partial index ix_jobs_claimable (priority, run_after)
        -- WHERE status = 'pending', so this walks the index in order and stops
        -- at the first row it can lock.
        SELECT id FROM jobs
        WHERE status = 'pending'
          AND run_after <= now()
          AND type = ANY(:types)
        ORDER BY priority, run_after
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, type, payload, attempts, max_attempts
    """
)


def claim(session: Session, worker_id: str, types: Sequence[str]) -> Lease | None:
    """Take the most urgent runnable job, or None if there is nothing to do.

    Commit straight after: until then the row lock is held and other workers
    skip the row, but the `running` status is invisible to them.
    """
    row = session.execute(_CLAIM, {"worker_id": worker_id, "types": list(types)}).one_or_none()
    if row is None:
        return None
    return Lease(
        job_id=row.id,
        type=row.type,
        payload=row.payload,
        worker_id=worker_id,
        attempt=row.attempts,
        max_attempts=row.max_attempts,
    )


def heartbeat(session: Session, lease: Lease) -> None:
    """Extend the lease. Raises `LeaseLost` if it has already expired.

    Workers call this between units of work, not from a background thread. A
    background heartbeat proves only that the process exists; a worker stuck
    inside a hung call would keep its lease forever. Heartbeating from the work
    loop means "still alive" has to mean "still making progress".
    """
    result = session.execute(
        text(f"UPDATE jobs SET locked_at = now(), updated_at = now() WHERE {_OWNED}"),
        _lease_params(lease),
    )
    if result.rowcount != 1:
        raise LeaseLost(f"job {lease.job_id} attempt {lease.attempt} is no longer ours")


def complete(session: Session, lease: Lease) -> None:
    """Mark the job done. Raises `LeaseLost`, writing nothing, if not ours.

    Call it in the transaction that writes the job's results, before writing
    them: the UPDATE takes the row lock, so from here until commit the reaper
    (which uses SKIP LOCKED) walks past this row, and a reaper that got there
    first has already changed the row so this matches nothing.
    """
    result = session.execute(
        text(
            "UPDATE jobs SET status = 'done', finished_at = now(), updated_at = now() "
            f"WHERE {_OWNED}"
        ),
        _lease_params(lease),
    )
    if result.rowcount != 1:
        raise LeaseLost(f"job {lease.job_id} attempt {lease.attempt} is no longer ours")


def has_unfinished(session: Session, types: Sequence[str]) -> bool:
    """Whether any job of these types is pending or running.

    "Nothing claimable right now" is not "drained": a job held by a dead worker
    is still running until the reaper frees it, and someone has to stay around
    to pick it up.
    """
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM jobs "
                "WHERE status IN ('pending', 'running') AND type = ANY(:types))"
            ),
            {"types": list(types)},
        ).scalar_one()
    )
