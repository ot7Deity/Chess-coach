"""Migrations must apply, reverse, and agree with the models.

The drift check is the important one. The initial migration is hand-written so
that its constraints and partial indexes read as deliberate, which means nothing
stops it from quietly disagreeing with `chess_coach.models` - except this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from chess_coach.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.postgres


def _config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migrations_create_every_table(migrated_engine: Engine):
    tables = set(inspect(migrated_engine).get_table_names())
    expected = {"players", "archives", "games", "moves", "positions", "jobs"}
    assert expected <= tables, f"missing: {expected - tables}"


def test_schema_matches_models(migrated_engine: Engine):
    """Fails if the hand-written migration has drifted from the models.

    `compare_metadata` is what `alembic revision --autogenerate` uses to decide
    what a new migration would contain. If it would contain anything, the
    migration and the models disagree.
    """
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "target_metadata": Base.metadata},
        )
        diff = compare_metadata(context, Base.metadata)

    # Indexes that Postgres reports differently than SQLAlchemy declares them
    # would show up here as noise; assert on the real set and print it when it
    # is not empty so the failure says what drifted.
    assert diff == [], "migration has drifted from models:\n" + "\n".join(repr(d) for d in diff)


def test_downgrade_removes_everything(database_url: str, migrated_engine: Engine):
    """A migration that cannot be reversed is a migration you cannot roll back
    in production, so the downgrade path is exercised rather than assumed."""
    config = _config(database_url)
    command.downgrade(config, "base")

    remaining = set(inspect(create_engine(database_url)).get_table_names())
    assert remaining <= {"alembic_version"}, f"left behind: {remaining}"

    # Leave the database migrated for any test that runs after this one.
    command.upgrade(config, "head")


def test_partial_indexes_are_actually_partial(migrated_engine: Engine):
    """The queue is polled in a loop, so the index workers hit must cover only
    pending rows - otherwise it grows forever with finished jobs and the whole
    point of the partial index is lost."""
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'")
        ).all()

    definitions = {name: definition for name, definition in rows}
    claimable = definitions.get("ix_jobs_claimable")
    assert claimable is not None, f"index missing; have {sorted(definitions)}"
    assert "WHERE" in claimable.upper(), f"not a partial index: {claimable}"
    assert "pending" in claimable
