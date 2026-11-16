"""The job queue against a real Postgres: claiming, leases and fencing.

Everything here is about concurrency, which is why none of it is mocked - the
guarantees under test are Postgres's row locks and SKIP LOCKED.
"""

from __future__ import annotations

import threading
from collections import Counter

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.queue import Lease, LeaseLost

pytestmark = pytest.mark.postgres

T = ["analyze-game"]


def _enqueue(engine: Engine, n: int = 1, **kw) -> list[int]:
    with Session(engine) as s, s.begin():
        return [
            queue.enqueue(s, "analyze-game", {"game_id": i}, dedupe_key=f"k:{i}:{kw}", **kw)
            for i in range(n)
        ]


def _claim(engine: Engine, worker: str = "w1", types=T) -> Lease | None:
    with Session(engine) as s, s.begin():
        return queue.claim(s, worker, types)


def _job(engine: Engine, job_id: int):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


# --- enqueue ----------------------------------------------------------------


def test_enqueue_is_idempotent_on_the_dedupe_key(clean_engine: Engine):
    with Session(clean_engine) as s, s.begin():
        first = queue.enqueue(s, "analyze-game", {"game_id": 1}, dedupe_key="analyze-game:1")
        again = queue.enqueue(s, "analyze-game", {"game_id": 1}, dedupe_key="analyze-game:1")
    assert first is not None
    assert again is None
    with clean_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM jobs")) == 1


def test_a_finished_job_is_not_re_enqueued_by_a_repeated_enqueue(clean_engine: Engine):
    """Re-running finished work must be deliberate. A sync that enqueues every
    pending game twice must not analyse the finished ones again."""
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, lease)
    assert _enqueue(clean_engine) == [None]
    assert _job(clean_engine, job_id).status == "done"


# --- claim ------------------------------------------------------------------


def test_claim_takes_the_most_urgent_job_first(clean_engine: Engine):
    with Session(clean_engine) as s, s.begin():
        bullet = queue.enqueue(s, "analyze-game", {"g": 1}, dedupe_key="b", priority=20)
        rapid = queue.enqueue(s, "analyze-game", {"g": 2}, dedupe_key="r", priority=10)
    assert _claim(clean_engine).job_id == rapid
    assert _claim(clean_engine).job_id == bullet
    assert _claim(clean_engine) is None


def test_claim_marks_the_job_running_and_counts_the_attempt(clean_engine: Engine):
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine, "worker-a")
    assert lease == Lease(
        job_id=job_id,
        type="analyze-game",
        payload={"game_id": 0},
        worker_id="worker-a",
        attempt=1,
        max_attempts=5,
    )
    row = _job(clean_engine, job_id)
    assert (row.status, row.locked_by, row.attempts) == ("running", "worker-a", 1)
    assert row.locked_at is not None


def test_a_job_is_not_claimed_before_its_run_after(clean_engine: Engine):
    (job_id,) = _enqueue(clean_engine)
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now() + interval '1 hour'"))
    assert _claim(clean_engine) is None
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now() - interval '1 second'"))
    assert _claim(clean_engine).job_id == job_id


def test_claim_only_takes_the_types_asked_for(clean_engine: Engine):
    with Session(clean_engine) as s, s.begin():
        queue.enqueue(s, "sync-player", {"username": "x"}, dedupe_key="sync:x")
    assert _claim(clean_engine, types=["analyze-game"]) is None
    assert _claim(clean_engine, types=["sync-player"]).type == "sync-player"


def test_a_held_claim_does_not_block_other_claimers(clean_engine: Engine):
    """SKIP LOCKED, directly: while one transaction holds a row lock, another
    claimer takes the next job immediately instead of queueing behind it.
    `lock_timeout` turns "would have blocked" into an error, so a regression to
    plain FOR UPDATE fails this test rather than hanging it."""
    first, second = _enqueue(clean_engine, 2)
    with Session(clean_engine) as holder, holder.begin():
        held = queue.claim(holder, "holder", T)
        assert held.job_id == first
        with Session(clean_engine) as other, other.begin():
            other.execute(text("SET LOCAL lock_timeout = '500ms'"))
            assert queue.claim(other, "other", T).job_id == second


def test_concurrent_claimers_never_share_a_job(clean_engine: Engine):
    """The property the whole queue exists for. Eight threads, each on its own
    connection, race to drain 200 jobs; every job must be claimed exactly once."""
    jobs = 200
    _enqueue(clean_engine, jobs)
    claimed: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def drain(name: str) -> None:
        try:
            start.wait()
            while (lease := _claim(clean_engine, name)) is not None:
                with lock:
                    claimed.append(lease.job_id)
        except BaseException as exc:  # surfaced below; a thread cannot fail the test
            errors.append(exc)

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    counts = Counter(claimed)
    assert len(counts) == jobs
    assert max(counts.values()) == 1


def test_the_claim_query_can_use_the_partial_index(clean_engine: Engine):
    """With seq scans disabled the planner must still find a plan through
    ix_jobs_claimable - i.e. the claim's WHERE and ORDER BY match the partial
    index. If they drifted apart, every claim would scan the whole table,
    finished jobs included."""
    with Session(clean_engine) as s, s.begin():
        s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in s.execute(
                text("EXPLAIN " + queue._CLAIM.text), {"worker_id": "w", "types": T}
            )
        )
    assert "ix_jobs_claimable" in plan, plan


# --- leases -----------------------------------------------------------------


def test_complete_marks_the_job_done(clean_engine: Engine):
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, lease)
    row = _job(clean_engine, job_id)
    assert row.status == "done"
    assert row.finished_at is not None


@pytest.mark.parametrize(
    "forged",
    [
        {"worker_id": "someone-else"},
        # Same worker id, older claim: what a worker restarted under the same
        # name looks like to its own zombie. locked_by alone would match.
        {"attempt": 0},
    ],
)
def test_only_the_current_lease_can_complete_or_heartbeat(clean_engine: Engine, forged):
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    stale = Lease(**{**lease.__dict__, **forged})

    with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
        queue.complete(s, stale)
    with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
        queue.heartbeat(s, stale)
    assert _job(clean_engine, job_id).status == "running"


def test_completion_rolls_back_with_the_results(clean_engine: Engine):
    """complete() shares the results' transaction. If writing the results
    fails, the job must still be running - not done with nothing to show."""
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with pytest.raises(RuntimeError), Session(clean_engine) as s, s.begin():
        queue.complete(s, lease)
        raise RuntimeError("writing moves failed")
    assert _job(clean_engine, job_id).status == "running"


def test_heartbeat_moves_the_lease_forward(clean_engine: Engine):
    (job_id,) = _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    with Session(clean_engine) as s, s.begin():
        queue.heartbeat(s, lease)
    with clean_engine.connect() as c:
        age = c.scalar(
            text("SELECT extract(epoch FROM now() - locked_at) FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
    assert age < 60


def test_has_unfinished_counts_running_jobs_too(clean_engine: Engine):
    """A job held by a dead worker is still unfinished: a worker told to run
    until the queue drains must wait for it, not exit."""
    with Session(clean_engine) as s:
        assert not queue.has_unfinished(s, T)
    _enqueue(clean_engine)
    lease = _claim(clean_engine)
    with Session(clean_engine) as s:
        assert queue.has_unfinished(s, T)
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, lease)
    with Session(clean_engine) as s:
        assert not queue.has_unfinished(s, T)


def test_a_job_inserted_with_plain_sql_needs_only_a_type_and_a_payload(clean_engine: Engine):
    """Benchmarks and psql sessions enqueue with raw SQL. Python-side defaults
    do nothing for them; the database's do (migration 0003)."""
    with clean_engine.begin() as c:
        c.execute(
            text("INSERT INTO jobs (type, payload) VALUES ('analyze-game', '{\"game_id\": 7}')")
        )
    lease = _claim(clean_engine)
    assert (lease.payload, lease.attempt, lease.max_attempts) == ({"game_id": 7}, 1, 5)
    assert _job(clean_engine, lease.job_id).priority == 100
