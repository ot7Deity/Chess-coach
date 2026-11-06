"""Migration 0002 with data in the table, not just on an empty schema."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_player(conn, username: str) -> int:
    return conn.scalar(
        text("INSERT INTO players (username, sync_status) VALUES (:u, 'complete') RETURNING id"),
        {"u": username},
    )


def _insert_game(conn, player_id: int, uuid: str) -> None:
    conn.execute(
        text(
            "INSERT INTO games (chesscom_uuid, player_id, color, result, time_class, "
            "time_control, rated, rules, pgn, played_at, analysis_status) VALUES "
            "(:uuid, :pid, 'white', 'win', 'blitz', '180', true, 'chess', '*', :at, 'complete')"
        ),
        {"uuid": uuid, "pid": player_id, "at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC)},
    )


def _unique_constraints(engine: Engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_unique_constraints("games")}


def _revision(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def test_downgrade_refuses_when_two_players_share_a_game_and_changes_nothing(
    database_url: str, clean_engine: Engine
):
    """0002's downgrade comment: it 'fails, deliberately, if two tracked
    players share a game'. Check it fails, and that the failure is atomic -
    the schema is still at 0002 with its constraint, not half-downgraded."""
    with clean_engine.begin() as conn:
        a, b = _insert_player(conn, "alice"), _insert_player(conn, "bob")
        _insert_game(conn, a, "shared")
        _insert_game(conn, b, "shared")

    before = _revision(clean_engine)  # head; 0002 or later
    with pytest.raises(Exception, match=r"games_chesscom_uuid_key|duplicate key"):
        command.downgrade(_config(database_url), "0001")

    # Every step of the downgrade ran in one transaction, so even the steps
    # above 0002 that succeeded are rolled back.
    assert _revision(clean_engine) == before
    assert "uq_games_player_uuid" in _unique_constraints(clean_engine)


def test_upgrade_from_0001_keeps_existing_games(database_url: str, clean_engine: Engine):
    config = _config(database_url)
    try:
        command.downgrade(config, "0001")
        with clean_engine.begin() as conn:
            pid = _insert_player(conn, "carol")
            _insert_game(conn, pid, "kept")
        command.upgrade(config, "head")
        with clean_engine.connect() as conn:
            status = conn.scalar(
                text("SELECT analysis_status FROM games WHERE chesscom_uuid = 'kept'")
            )
        assert status == "complete"
        assert "uq_games_player_uuid" in _unique_constraints(clean_engine)
    finally:
        command.upgrade(config, "head")
