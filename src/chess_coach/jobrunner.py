"""The worker loop every job type shares: reap, claim, run, record the outcome.

A job type is a subclass that names its `job_types` and implements `handle`.
Everything about leases, retries, dead-lettering and database outages lives
here once, so the analysis worker and the sync worker cannot drift apart on
the parts that decide correctness.

What a subclass owns:

* `handle(lease)` does the work and must call `queue.complete` - ideally in
  the transaction that writes its results, so a lost lease writes nothing.
  Raise `PermanentJobError` for bad input that no retry can fix; anything
  else is retried with backoff.
* `on_failed(session, lease, status)` and `on_reaped(session, job)` keep the
  job type's own status columns in step, inside the queue's transaction.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.config import Settings
from chess_coach.logging import get_logger
from chess_coach.queue import Lease, LeaseLost

log = get_logger(__name__)


class PermanentJobError(ValueError):
    """The job's input is bad. It is marked `failed` at once, not retried."""


@dataclass
class Heartbeat:
    """Renews a lease when called, at most once per `interval`.

    Called from inside the work - between positions, between HTTP requests -
    never from a background thread: "still alive" has to mean "still making
    progress", or a worker hung in a call would keep its lease forever.
    """

    db: Engine
    lease: Lease
    interval: float
    clock: Callable[[], float] = time.monotonic
    last: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.last = self.clock()

    def __call__(self) -> None:
        if self.clock() - self.last < self.interval:
            return
        with Session(self.db) as session, session.begin():
            queue.heartbeat(session, self.lease)
        self.last = self.clock()


class JobRunner:
    job_types: ClassVar[tuple[str, ...]] = ()

    def __init__(self, db: Engine, *, worker_id: str, settings: Settings) -> None:
        if len(worker_id) > 64:
            raise ValueError("worker id must fit jobs.locked_by (64 characters)")
        if not self.job_types:
            raise TypeError(f"{type(self).__name__} names no job types")
        self.db = db
        self.worker_id = worker_id
        self.settings = settings
        self.heartbeat_interval = settings.job_lock_timeout_sec / 3

    # --- for subclasses -----------------------------------------------------

    def handle(self, lease: Lease) -> None:
        raise NotImplementedError

    def on_failed(self, session: Session, lease: Lease, status: str) -> None:
        """The job just failed and is now `status` (pending, dead or failed)."""

    def on_reaped(self, session: Session, job: queue.ReapedJob) -> None:
        """The job's lease expired and it is now `job.status` (pending or dead)."""

    def describe(self) -> dict[str, object]:
        """Extra fields for the worker.start log line."""
        return {}

    def heartbeat(self, lease: Lease) -> Heartbeat:
        return Heartbeat(self.db, lease, self.heartbeat_interval)

    # --- loop ---------------------------------------------------------------

    def run(
        self,
        *,
        max_jobs: int | None = None,
        until_empty: bool = False,
        stop: threading.Event | None = None,
    ) -> int:
        """Process jobs until stopped; return how many were processed.

        `until_empty` exits once no job of this runner's types is pending or
        running - not merely when none is claimable, because a job held by a
        dead worker is still running until the reaper frees it, and somebody
        has to stay to pick it up. `max_jobs` counts attempts, failed ones
        included.
        """
        stop = stop or threading.Event()
        processed = 0
        log.info("worker.start", worker_id=self.worker_id, types=self.job_types, **self.describe())
        while not stop.is_set():
            try:
                self.reap()
                lease = self._claim()
                if lease is None:
                    if until_empty and not self._unfinished():
                        break
                    stop.wait(self.settings.worker_poll_interval_sec)
                    continue
            except OperationalError as exc:
                # The database is unreachable. Nothing is held that needs
                # releasing - an in-flight job's lease simply lapses - so wait
                # and try again rather than exit.
                log.warning("worker.db_unavailable", error=str(exc).splitlines()[0])
                stop.wait(self.settings.worker_poll_interval_sec)
                continue
            self.process(lease)
            processed += 1
            if max_jobs is not None and processed >= max_jobs:
                break
        log.info("worker.stop", worker_id=self.worker_id, processed=processed)
        return processed

    def _claim(self) -> Lease | None:
        with Session(self.db) as session, session.begin():
            return queue.claim(session, self.worker_id, self.job_types)

    def _unfinished(self) -> bool:
        with Session(self.db) as session:
            return queue.has_unfinished(session, self.job_types)

    def reap(self) -> list[queue.ReapedJob]:
        """Free expired leases of this runner's job types.

        Only its own types: the hook that puts a reaped job's domain status
        back in step (a game's, a player's) belongs to the runner for that type.
        """
        with Session(self.db) as session, session.begin():
            reaped = queue.reap_expired(
                session, self.settings.job_lock_timeout_sec, types=self.job_types
            )
            for job in reaped:
                self.on_reaped(session, job)
        for job in reaped:
            log.warning("job.reaped", job_id=job.job_id, type=job.type, status=job.status)
        return reaped

    # --- one job ------------------------------------------------------------

    def process(self, lease: Lease) -> None:
        bound = log.bind(job_id=lease.job_id, type=lease.type, attempt=lease.attempt)
        started = time.monotonic()
        try:
            self.handle(lease)
        except LeaseLost:
            # Reaped while working - presumably by someone who thought we were
            # dead. The job is theirs now; whatever we computed is discarded.
            bound.warning("job.lease_lost", seconds=round(time.monotonic() - started, 2))
        except PermanentJobError as exc:
            bound.error("job.bad_input", error=str(exc))
            self._fail(lease, exc, permanent=True)
        except Exception as exc:
            bound.exception("job.error", error=f"{type(exc).__name__}: {exc}")
            self._fail(lease, exc, permanent=False)

    def _fail(self, lease: Lease, exc: BaseException, *, permanent: bool) -> None:
        delay = queue.retry_delay(
            lease.attempt,
            base_sec=self.settings.job_retry_base_sec,
            cap_sec=self.settings.job_retry_cap_sec,
        )
        try:
            with Session(self.db) as session, session.begin():
                status = queue.fail(
                    session,
                    lease,
                    f"{type(exc).__name__}: {exc}",
                    retry_in_sec=delay,
                    permanent=permanent,
                )
                self.on_failed(session, lease, status)
        except LeaseLost:
            log.warning("job.lease_lost", job_id=lease.job_id, during="fail")
        except OperationalError as exc2:
            # The lease will lapse and the reaper will retry the job.
            log.warning("job.fail_unrecorded", job_id=lease.job_id, error=str(exc2))
