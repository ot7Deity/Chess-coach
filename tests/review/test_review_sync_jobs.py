"""Adversarial checks on the sync-player job (M4 #50) and POST /players (#51).

The progress page (web/.../progress/Progress.tsx) polls only while
`sync_status` is pending/syncing or analysis is pending. So whenever a sync is
queued or waiting to retry, `players.sync_status` must not say "error" or
"complete" from the previous run - otherwise the page shows a finished or
failed download and stops polling while work is still queued.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

jobs = pytest.importorskip("chess_coach.ingest.jobs")
fake_mod = pytest.importorskip("tests.chesscom_fake")

from chess_coach.config import Settings  # noqa: E402

pytestmark = pytest.mark.postgres

ME = "deitythemenace"


def _settings() -> Settings:
    return Settings(_env_file=None, worker_poll_interval_sec=0.05, job_retry_base_sec=30)


def _player(engine: Engine, status: str = "pending", error: str | None = None) -> int:
    with engine.begin() as c:
        return c.execute(
            text(
                "INSERT INTO players (username, sync_status, sync_error) "
                "VALUES (:u, :s, :e) RETURNING id"
            ),
            {"u": ME, "s": status, "e": error},
        ).scalar_one()


def _status(engine: Engine) -> tuple[str, str | None]:
    with engine.connect() as c:
        return tuple(c.execute(text("SELECT sync_status, sync_error FROM players")).one())


def test_a_sync_waiting_to_retry_is_not_reported_as_failed(clean_engine: Engine):
    pid = _player(clean_engine)
    with Session(clean_engine) as s, s.begin():
        jobs.enqueue_sync(s, pid, ME, max_attempts=5)
    fake = fake_mod.FakeChessCom()
    fake.add_player(ME)
    fake.fail(f"player/{ME}/games/archives", *([503] * 10))
    jobs.SyncWorker(clean_engine, fake.client(), worker_id="r1", settings=_settings()).run(
        max_jobs=1
    )
    with clean_engine.connect() as c:
        assert c.execute(text("SELECT status FROM jobs")).scalar_one() == "pending"
    assert _status(clean_engine)[0] in ("pending", "syncing")


def test_re_posting_a_failed_player_reports_the_queued_sync(clean_engine: Engine):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    get_db = pytest.importorskip("chess_coach.api.players").get_db
    from chess_coach.api.main import app

    pid = _player(clean_engine, "error", "ChessComUnavailable: 503")
    with clean_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO jobs (type, payload, status, attempts, max_attempts, dedupe_key) "
                "VALUES (:t, CAST(:p AS jsonb), 'dead', 5, 5, :k)"
            ),
            {
                "t": jobs.SYNC_JOB_TYPE,
                "p": f'{{"player_id": {pid}, "username": "{ME}"}}',
                "k": jobs.sync_dedupe_key(pid),
            },
        )
    app.dependency_overrides[get_db] = lambda: clean_engine
    try:
        r = fastapi_testclient.TestClient(app).post("/players", json={"username": ME})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 202
    with clean_engine.connect() as c:
        assert c.execute(text("SELECT status FROM jobs")).scalar_one() == "pending"
    assert (r.json()["sync_status"], r.json()["sync_error"]) == ("pending", None)
