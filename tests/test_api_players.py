"""POST /players and GET /players/{username} against a real Postgres."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from chess_coach.api.main import app
from chess_coach.api.players import get_db
from chess_coach.ingest.jobs import SYNC_JOB_TYPE
from tests.report_data import add_game, add_player, day

pytestmark = pytest.mark.postgres

ZERO = {"games": 0, "analyzed": 0, "pending": 0, "failed": 0, "skipped": 0, "current_rating": None}


@pytest.fixture
def client(clean_engine: Engine):
    app.dependency_overrides[get_db] = lambda: clean_engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _jobs(engine: Engine):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM jobs ORDER BY id")).all()


# --- POST /players ----------------------------------------------------------


def test_adding_a_player_returns_202_with_progress_and_queues_a_sync(
    client: TestClient, clean_engine: Engine
):
    r = client.post("/players", json={"username": "  DeityTheMenace "})
    assert r.status_code == 202
    assert r.json() == {
        "username": "deitythemenace",
        "sync_status": "pending",
        "sync_error": None,
        "last_synced_at": None,
        "analyzed_last_hour": 0,
        "time_classes": [
            {"time_class": tc, **ZERO} for tc in ("bullet", "blitz", "rapid", "daily")
        ],
    }
    (job,) = _jobs(clean_engine)
    assert (job.type, job.status, job.payload["username"]) == (
        SYNC_JOB_TYPE,
        "pending",
        "deitythemenace",
    )


def test_adding_a_player_twice_is_one_player_and_one_queued_sync(
    client: TestClient, clean_engine: Engine
):
    assert client.post("/players", json={"username": "deitythemenace"}).status_code == 202
    assert client.post("/players", json={"username": "DEITYTHEMENACE"}).status_code == 202
    with clean_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM players")) == 1
    assert len(_jobs(clean_engine)) == 1


def test_adding_a_synced_player_again_re_queues_a_sync(client: TestClient, clean_engine: Engine):
    client.post("/players", json={"username": "deitythemenace"})
    with clean_engine.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'done', attempts = 1"))
    client.post("/players", json={"username": "deitythemenace"})
    (job,) = _jobs(clean_engine)
    assert job.status == "pending"


@pytest.mark.parametrize("username", ["", "a b", "../etc", "x" * 51, "naïve"])
def test_an_invalid_username_is_a_422_with_fastapis_validation_array(
    client: TestClient, clean_engine: Engine, username: str
):
    r = client.post("/players", json={"username": username})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert _jobs(clean_engine) == []


# --- GET /players/{username} ------------------------------------------------


@pytest.mark.parametrize("username", ["nobody", "not a name"])
def test_an_untracked_player_is_a_404(client: TestClient, username: str):
    r = client.get(f"/players/{username}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Player not found"}


def test_progress_counts_every_status_and_always_has_four_classes(
    client: TestClient, clean_engine: Engine
):
    pid = add_player(
        clean_engine,
        last_synced_at=dt.datetime(2026, 11, 6, 2, 14, 5, 123456, tzinfo=dt.UTC),
    )
    for status in ("complete", "complete", "pending", "running", "error", "skipped"):
        rules = "chess960" if status == "skipped" else "chess"
        add_game(clean_engine, pid, time_class="blitz", analysis_status=status, rules=rules)
    add_game(clean_engine, pid, time_class="rapid", analysis_status="complete")

    body = client.get("/players/DeityTheMenace").json()
    assert body["last_synced_at"] == "2026-11-06T02:14:05Z"
    by_class = {tc["time_class"]: tc for tc in body["time_classes"]}
    assert [tc["time_class"] for tc in body["time_classes"]] == [
        "bullet",
        "blitz",
        "rapid",
        "daily",
    ]
    blitz = by_class["blitz"]
    assert (blitz["games"], blitz["analyzed"], blitz["pending"], blitz["failed"]) == (6, 2, 2, 1)
    assert blitz["skipped"] == 1
    for tc in body["time_classes"]:
        assert tc["analyzed"] + tc["pending"] + tc["failed"] + tc["skipped"] == tc["games"]
    assert by_class["bullet"] == {"time_class": "bullet", **ZERO}


def test_current_rating_is_the_latest_rated_standard_game_with_a_rating(
    client: TestClient, clean_engine: Engine
):
    pid = add_player(clean_engine)
    add_game(clean_engine, pid, player_rating=1500, played_at=day("2026-10-01"))
    add_game(clean_engine, pid, player_rating=1510, played_at=day("2026-10-02"))
    # Each of these is newer and must be ignored:
    add_game(clean_engine, pid, player_rating=999, rated=False, played_at=day("2026-10-03"))
    add_game(clean_engine, pid, player_rating=888, rules="chess960", played_at=day("2026-10-04"))
    add_game(clean_engine, pid, player_rating=None, played_at=day("2026-10-05"))
    blitz = client.get("/players/deitythemenace").json()["time_classes"][1]
    assert blitz["current_rating"] == 1510


def test_analyzed_last_hour_counts_recent_analyses_across_classes(
    client: TestClient, clean_engine: Engine
):
    pid = add_player(clean_engine)
    now = dt.datetime.now(dt.UTC)
    add_game(clean_engine, pid, analysis_status="complete", analyzed_at=now)
    add_game(clean_engine, pid, time_class="rapid", analysis_status="complete", analyzed_at=now)
    add_game(
        clean_engine,
        pid,
        analysis_status="complete",
        analyzed_at=now - dt.timedelta(hours=2),
    )
    assert client.get("/players/deitythemenace").json()["analyzed_last_hour"] == 2


# --- CORS -------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://localhost:3001"])
def test_the_dashboard_origins_may_post(client: TestClient, origin: str):
    r = client.options(
        "/players",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin


def test_other_origins_are_not_allowed(client: TestClient):
    r = client.options(
        "/players",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_timestamps_have_a_z_suffix_and_whole_seconds():
    from chess_coach.api.schemas import iso_z

    value = iso_z(dt.datetime(2026, 11, 6, 2, 14, 5, 999999, tzinfo=dt.UTC))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value)
    # A non-UTC aware time is converted, not relabelled.
    est = dt.timezone(dt.timedelta(hours=-5))
    assert iso_z(dt.datetime(2026, 11, 5, 21, 14, 5, tzinfo=est)) == "2026-11-06T02:14:05Z"
