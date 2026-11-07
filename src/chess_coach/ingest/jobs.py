"""Syncing a player as a queued job, so the API can ask for one without ever
calling Chess.com itself.

`POST /players` inserts the player and calls `enqueue_sync`; a sync worker
(`chess-coach-worker --kind sync`) claims the job, runs `sync_player`, and
enqueues analysis for whatever the sync found.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach import queue
from chess_coach.analysis.jobs import enqueue_pending_games
from chess_coach.chesscom import ChessComClient, PlayerNotFound, normalize_username
from chess_coach.config import Settings
from chess_coach.ingest.sync import sync_player
from chess_coach.jobrunner import Heartbeat, JobRunner, PermanentJobError
from chess_coach.logging import get_logger
from chess_coach.queue import Lease

log = get_logger(__name__)

SYNC_JOB_TYPE = "sync-player"
# Ahead of every analysis job. Sync jobs are claimed by sync workers only, so
# this does not slow analysis down; it orders syncs ahead of anything else a
# future mixed worker might take.
SYNC_PRIORITY = 0


def sync_dedupe_key(player_id: int) -> str:
    return f"{SYNC_JOB_TYPE}:{player_id}"


def enqueue_sync(
    session: Session, player_id: int, username: str, *, max_attempts: int
) -> int | None:
    """Queue a sync unless one is already queued or running; return its id or None.

    One job row per player, re-armed rather than re-inserted: a finished,
    failed or dead sync job goes back to `pending`. The dedupe key stays
    unique, and two concurrent requests cannot create two syncs.

    Re-arming does not reset `attempts` - it raises `max_attempts` instead.
    `attempts` is the lease's fencing token, and resetting it would let a
    new claim reuse a number an old, possibly still-running zombie holds.
    """
    row = session.execute(
        text(
            """
            INSERT INTO jobs (type, payload, priority, max_attempts, dedupe_key)
            VALUES (:type, CAST(:payload AS jsonb), :priority, :max_attempts, :key)
            ON CONFLICT (dedupe_key) DO UPDATE
            SET status = 'pending',
                run_after = now(),
                max_attempts = jobs.attempts + :max_attempts,
                locked_by = NULL,
                locked_at = NULL,
                finished_at = NULL,
                updated_at = now()
            WHERE jobs.status IN ('done', 'failed', 'dead')
            RETURNING id
            """
        ),
        {
            "type": SYNC_JOB_TYPE,
            "payload": json.dumps(
                {"player_id": int(player_id), "username": normalize_username(username)}
            ),
            "priority": SYNC_PRIORITY,
            "max_attempts": max_attempts,
            "key": sync_dedupe_key(player_id),
        },
    ).one_or_none()
    return row.id if row is not None else None


class _HeartbeatingClient:
    """The Chess.com client, renewing the sync job's lease before every request.

    `sync_player` has no progress callback, and a full history is a minute or
    more at one request per second. Every request is progress, so that is
    where the heartbeat goes - from inside the work, like the analysis
    worker's heartbeat between positions.
    """

    def __init__(self, client: ChessComClient, heartbeat: Heartbeat) -> None:
        self._client = client
        self._heartbeat = heartbeat

    @property
    def requests_sent(self) -> int:
        return self._client.requests_sent

    def profile(self, username: str) -> dict[str, Any]:
        self._heartbeat()
        return self._client.profile(username)

    def archive_months(self, username: str) -> list[str]:
        self._heartbeat()
        return self._client.archive_months(username)

    def month(self, username: str, month: str, *, etag: str | None = None):
        self._heartbeat()
        return self._client.month(username, month, etag=etag)


class SyncWorker(JobRunner):
    """Runs `sync-player` jobs. Needs no engine; needs the shared rate limiter."""

    job_types = (SYNC_JOB_TYPE,)

    def __init__(
        self, db: Engine, client: ChessComClient, *, worker_id: str, settings: Settings
    ) -> None:
        super().__init__(db, worker_id=worker_id, settings=settings)
        self.client = client

    def handle(self, lease: Lease) -> None:
        try:
            player_id = int(lease.payload["player_id"])
            username = normalize_username(str(lease.payload["username"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentJobError(f"bad sync payload {lease.payload!r}: {exc}") from exc

        with Session(self.db) as session, session.begin():
            queue.heartbeat(session, lease)  # still ours, and the clock restarts

        client = _HeartbeatingClient(self.client, self.heartbeat(lease))
        try:
            result = sync_player(self.db, client, username)  # type: ignore[arg-type]
        except PlayerNotFound as exc:
            # sync_player has already recorded it on the player (sync_status
            # 'error'); a username with no account will not gain one by retrying.
            raise PermanentJobError(f"no Chess.com account {username!r}") from exc

        with Session(self.db) as session, session.begin():
            queue.complete(session, lease)
            queued = enqueue_pending_games(
                session, max_attempts=self.settings.job_max_attempts, player_id=player_id
            )
        log.info(
            "job.done",
            job_id=lease.job_id,
            username=username,
            games_new=result.games_new,
            requests=result.requests,
            analysis_enqueued=queued,
        )

    def on_reaped(self, session: Session, job: queue.ReapedJob) -> None:
        """A sync worker died mid-sync: the player is no longer 'syncing'."""
        player_id = job.payload.get("player_id")
        if player_id is None:
            return
        if job.status == "dead":
            values = "sync_status = 'error', sync_error = 'sync abandoned: worker kept dying'"
        else:
            values = "sync_status = 'pending'"
        session.execute(
            text(f"UPDATE players SET {values} WHERE id = :id AND sync_status = 'syncing'"),
            {"id": int(player_id)},
        )
