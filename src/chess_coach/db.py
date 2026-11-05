"""Database engine and session management.

Sync SQLAlchemy throughout. The workers are CPU-bound on a Stockfish
subprocess, so async would add complexity to the queue's transaction handling
and buy no throughput. See INTERVIEW_PREP.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from chess_coach.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Process-wide engine, created on first use.

    Each worker is its own process with its own pool. `pool_pre_ping` matters
    here: an analysis worker can sit inside a multi-second Stockfish call, and a
    connection that the database closed meanwhile would otherwise surface as a
    failed job rather than a reconnect.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine and session factory.

    Needed by tests that point the process at a different database after the
    engine was already built.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
