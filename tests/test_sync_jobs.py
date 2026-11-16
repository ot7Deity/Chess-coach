"""Sync as a queued job: enqueueing it from the API side, and the sync worker."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.analysis.jobs import JOB_TYPE as ANALYZE
from chess_coach.config import Settings
from chess_coach.ingest.jobs import SYNC_JOB_TYPE, SyncWorker, enqueue_sync, sync_dedupe_key
from chess_coach.queue import LeaseLost
from tests.chesscom_fake import FakeChessCom, make_game

pytestmark = pytest.mark.postgres

ME = "deitythemenace"


def _settings() -> Settings:
    return Settings(_env_file=None, worker_poll_interval_sec=0.05, job_retry_base_sec=30)


def _add_player(engine: Engine, username: str = ME) -> int:
    """What POST /players does: a bare row, no Chess.com call."""
    with engine.begin() as c:
        return c.execute(
            text("INSERT INTO players (username, sync_status) VALUES (:u, 'pending') RETURNING id"),
            {"u": username},
        ).scalar_one()


def _enqueue(engine: Engine, player_id: int, username: str = ME) -> int | None:
    with Session(engine) as s, s.begin():
        return enqueue_sync(s, player_id, username, max_attempts=5)


def _one(engine: Engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).one()


def _fake_history() -> FakeChessCom:
    fake = FakeChessCom()
    fake.add_player(ME)
    for month in ("2026-09", "2026-10"):
        for j in range(3):
            fake.add_game(ME, month, make_game(f"{month}-{j}", white=ME, black=f"opp{j}"))
    return fake


# --- enqueue_sync -----------------------------------------------------------


def test_a_sync_is_enqueued_once_while_one_is_queued(clean_engine: Engine):
    player_id = _add_player(clean_engine)
    first = _enqueue(clean_engine, player_id)
    assert first is not None
    assert _enqueue(clean_engine, player_id) is None
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.type, job.payload, job.dedupe_key, job.priority) == (
        SYNC_JOB_TYPE,
        {"player_id": player_id, "username": ME},
        sync_dedupe_key(player_id),
        0,
    )


def test_a_running_sync_is_not_enqueued_again(clean_engine: Engine):
    player_id = _add_player(clean_engine)
    _enqueue(clean_engine, player_id)
    with Session(clean_engine) as s, s.begin():
        queue.claim(s, "w", [SYNC_JOB_TYPE])
    assert _enqueue(clean_engine, player_id) is None
    assert _one(clean_engine, "SELECT status FROM jobs").status == "running"


@pytest.mark.parametrize("final_status", ["done", "failed", "dead"])
def test_a_finished_sync_is_re_armed_not_duplicated(clean_engine: Engine, final_status: str):
    """One job row per player for ever: re-syncing re-arms it. Attempts are
    never reset - they are the lease's fencing token - so the attempt budget
    is extended instead."""
    player_id = _add_player(clean_engine)
    job_id = _enqueue(clean_engine, player_id)
    with clean_engine.begin() as c:
        c.execute(
            text("UPDATE jobs SET status = :s, attempts = 3, finished_at = now()"),
            {"s": final_status},
        )
    assert _enqueue(clean_engine, player_id) == job_id
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.status, job.attempts, job.max_attempts, job.finished_at) == (
        "pending",
        3,
        8,
        None,
    )


def test_a_re_armed_sync_fences_out_a_lease_from_its_previous_run(clean_engine: Engine):
    player_id = _add_player(clean_engine)
    _enqueue(clean_engine, player_id)
    with Session(clean_engine) as s, s.begin():
        old = queue.claim(s, "w", [SYNC_JOB_TYPE])
    with Session(clean_engine) as s, s.begin():
        queue.complete(s, old)
    _enqueue(clean_engine, player_id)
    with Session(clean_engine) as s, s.begin():
        new = queue.claim(s, "w", [SYNC_JOB_TYPE])
    assert new.attempt == old.attempt + 1
    with Session(clean_engine) as s, s.begin(), pytest.raises(LeaseLost):
        queue.complete(s, old)


# --- the sync worker --------------------------------------------------------


def test_the_sync_worker_syncs_and_then_queues_analysis(clean_engine: Engine):
    player_id = _add_player(clean_engine)
    _enqueue(clean_engine, player_id)
    worker = SyncWorker(
        clean_engine, _fake_history().client(), worker_id="s1", settings=_settings()
    )
    assert worker.run(until_empty=True) == 1

    player = _one(clean_engine, "SELECT * FROM players WHERE id = :id", id=player_id)
    assert player.sync_status == "complete"
    assert _one(clean_engine, "SELECT count(*) AS n FROM games").n == 6
    sync = _one(clean_engine, "SELECT * FROM jobs WHERE type = :t", t=SYNC_JOB_TYPE)
    assert sync.status == "done"
    analysis = _one(clean_engine, "SELECT count(*) AS n FROM jobs WHERE type = :t", t=ANALYZE)
    assert analysis.n == 6


def test_an_account_that_does_not_exist_fails_permanently_and_says_so(clean_engine: Engine):
    """The API cannot check the account exists (it never calls Chess.com), so
    a typo is discovered here: recorded on the player for the progress page,
    and not retried."""
    player_id = _add_player(clean_engine, "no-such-user")
    _enqueue(clean_engine, player_id, "no-such-user")
    SyncWorker(clean_engine, FakeChessCom().client(), worker_id="s1", settings=_settings()).run(
        until_empty=True
    )
    job = _one(clean_engine, "SELECT * FROM jobs")
    assert (job.status, job.attempts) == ("failed", 1)
    player = _one(clean_engine, "SELECT * FROM players WHERE id = :id", id=player_id)
    assert player.sync_status == "error"
    assert player.sync_error


def test_chess_com_being_down_is_retried_later(clean_engine: Engine):
    player_id = _add_player(clean_engine)
    _enqueue(clean_engine, player_id)
    fake = _fake_history()
    fake.fail(f"player/{ME}/games/archives", *([503] * 10))
    worker = SyncWorker(clean_engine, fake.client(), worker_id="s1", settings=_settings())
    worker.run(max_jobs=1)
    job = _one(clean_engine, "SELECT *, run_after > now() AS later FROM jobs")
    assert (job.status, job.attempts, job.later) == ("pending", 1, True)
    assert "ChessComUnavailable" in job.last_error


def test_a_sync_worker_that_died_mid_sync_leaves_the_player_pending_not_syncing(
    clean_engine: Engine,
):
    player_id = _add_player(clean_engine)
    _enqueue(clean_engine, player_id)
    with Session(clean_engine) as s, s.begin():
        queue.claim(s, "dead-worker", [SYNC_JOB_TYPE])
    with clean_engine.begin() as c:
        c.execute(text("UPDATE players SET sync_status = 'syncing'"))
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    worker = SyncWorker(
        clean_engine, _fake_history().client(), worker_id="s1", settings=_settings()
    )
    (reaped,) = worker.reap()
    assert reaped.status == "pending"
    assert _one(clean_engine, "SELECT sync_status FROM players").sync_status == "pending"


def test_a_sync_worker_does_not_reap_analysis_jobs(clean_engine: Engine):
    """Each kind of worker reaps its own job types, so the hook that fixes a
    reaped job's domain status is always the right one."""
    with Session(clean_engine) as s, s.begin():
        queue.enqueue(s, ANALYZE, {"game_id": 1}, dedupe_key="a:1")
        queue.claim(s, "dead-analysis-worker", [ANALYZE])
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    worker = SyncWorker(
        clean_engine, _fake_history().client(), worker_id="s1", settings=_settings()
    )
    assert worker.reap() == []
    assert _one(clean_engine, "SELECT status FROM jobs").status == "running"
