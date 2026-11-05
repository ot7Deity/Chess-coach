from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- engine -----------------------------------------------------------------


def _resolve_stockfish() -> Path | None:
    """Find a usable Stockfish, or None so engine tests can skip cleanly.

    Checked in order: STOCKFISH_PATH, the vendored binary, then PATH. Engine
    tests must never be a hard requirement for running the unit suite, or a
    fresh clone cannot run tests until an 80 MB download finishes.
    """
    env = os.environ.get("STOCKFISH_PATH")
    if env:
        candidate = Path(env)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        if candidate.exists():
            return candidate

    for name in ("stockfish.exe", "stockfish"):
        candidate = REPO_ROOT / "vendor" / "stockfish" / name
        if candidate.exists():
            return candidate

    found = shutil.which("stockfish")
    return Path(found) if found else None


@pytest.fixture(scope="session")
def stockfish_path() -> Path:
    path = _resolve_stockfish()
    if path is None:
        pytest.skip("no Stockfish binary (run: uv run python tools/fetch_stockfish.py)")
    return path


# --- database ---------------------------------------------------------------


def _redact(url: str) -> str:
    """Strip credentials so a skip or failure message is safe to print."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


@pytest.fixture(scope="session")
def database_url() -> str:
    """A reachable Postgres URL, or skip.

    Postgres-backed tests are the ones that matter most in this project - the
    queue's correctness is `FOR UPDATE SKIP LOCKED` behaviour - but they cannot
    be a precondition for running any tests at all, so they skip when no
    database is around. CI always has one.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        from chess_coach.config import get_settings

        url = get_settings().database_url

    # This fixture's schema reset is destructive, so refuse anything that is not
    # visibly a test database. A typo in DATABASE_URL should not be able to drop
    # a real schema.
    db_name = (urlsplit(url).path or "").lstrip("/")
    if "test" not in db_name:
        pytest.skip(
            f"refusing to run destructive fixtures against {db_name!r}: "
            "the database name must contain 'test'"
        )

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"no Postgres at {_redact(url)}: {type(exc).__name__}")
    finally:
        engine.dispose()

    return url


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    """A Postgres with the migrations applied from an empty schema.

    Running the real migrations rather than `Base.metadata.create_all` means the
    migrations themselves are tested on every run - a schema that only ever gets
    created from the models is a schema whose migrations have never been proven
    to work.
    """
    from alembic import command
    from alembic.config import Config

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back, so tests cannot affect each other.

    The outer transaction is never committed: a test's `session.commit()` only
    commits a nested SAVEPOINT, so visibility within the test behaves normally
    while the database is left untouched.
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
