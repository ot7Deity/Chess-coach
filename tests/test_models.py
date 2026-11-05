"""Tests for the constraints that encode design decisions.

These are not "does SQLAlchemy work" tests. Each one pins a decision that the
rest of the system relies on, and would otherwise be free to rot silently.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chess_coach.models import Archive, Game, Player, Position

pytestmark = pytest.mark.postgres


def _player(session: Session, username: str = "deitythemenace") -> Player:
    player = Player(username=username)
    session.add(player)
    session.flush()
    return player


def _game(player: Player, uuid: str, **overrides) -> Game:
    defaults = dict(
        chesscom_uuid=uuid,
        player_id=player.id,
        color="white",
        result="win",
        time_class="bullet",
        time_control="60+1",
        pgn='[Event "Live Chess"]\n\n1. e4 e5 1-0',
        played_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    defaults.update(overrides)
    return Game(**defaults)


# --- the position cache key -------------------------------------------------


def test_same_position_can_be_stored_at_two_depths(db_session: Session):
    """The whole correction to the design doc, as an executable statement.

    An evaluation belongs to one engine at one depth. A depth-12 result and a
    depth-16 result for the same board are two different facts and must be able
    to coexist - with a bare `fen_hash` primary key, the first one written would
    shadow the other forever.
    """
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0"
    fen_hash = hashlib.sha256(fen.encode()).digest()

    db_session.add_all(
        [
            Position(fen_hash=fen_hash, engine_version="sf_18", depth=12, fen=fen, eval_cp=20),
            Position(fen_hash=fen_hash, engine_version="sf_18", depth=16, fen=fen, eval_cp=35),
            # A different engine version is also a different fact.
            Position(fen_hash=fen_hash, engine_version="sf_17", depth=16, fen=fen, eval_cp=28),
        ]
    )
    db_session.flush()

    stored = db_session.query(Position).filter_by(fen_hash=fen_hash).count()
    assert stored == 3


def test_identical_position_engine_and_depth_cannot_be_stored_twice(db_session: Session):
    """Two workers racing on the same position must collide rather than write
    two rows, so the cache has one answer per (position, engine, depth)."""
    fen = "8/8/8/8/8/8/8/K6k w - - 0"
    fen_hash = hashlib.sha256(fen.encode()).digest()

    db_session.add(
        Position(fen_hash=fen_hash, engine_version="sf_18", depth=16, fen=fen, eval_cp=0)
    )
    db_session.flush()

    db_session.add(
        Position(fen_hash=fen_hash, engine_version="sf_18", depth=16, fen=fen, eval_cp=999)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_position_is_either_an_evaluation_or_a_mate_never_both(db_session: Session):
    """A forced mate is not a centipawn value. Encoding it as one (say 30000)
    would corrupt every average that touches it, so the schema forbids it."""
    fen = "6k1/5ppp/8/8/8/8/8/R6K w - - 0"
    fen_hash = hashlib.sha256(fen.encode()).digest()

    db_session.add(
        Position(
            fen_hash=fen_hash,
            engine_version="sf_18",
            depth=16,
            fen=fen,
            eval_cp=500,
            mate_in=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_position_must_carry_one_of_them(db_session: Session):
    fen = "6k1/5ppp/8/8/8/8/8/R6K b - - 0"
    fen_hash = hashlib.sha256(fen.encode()).digest()

    db_session.add(Position(fen_hash=fen_hash, engine_version="sf_18", depth=16, fen=fen))
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- ingestion idempotency --------------------------------------------------


def test_the_same_chesscom_game_cannot_be_ingested_twice(db_session: Session):
    """Ingestion upserts on chesscom_uuid. This constraint is what makes
    re-fetching a month, or crashing halfway through one, safe."""
    player = _player(db_session)
    db_session.add(_game(player, "1b94ef29-a8e0-11ee-8c7e-6cfe544c0428"))
    db_session.flush()

    db_session.add(_game(player, "1b94ef29-a8e0-11ee-8c7e-6cfe544c0428", result="loss"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_month_is_tracked_once_per_player(db_session: Session):
    player = _player(db_session)
    db_session.add(Archive(player_id=player.id, month="2026-11"))
    db_session.flush()

    db_session.add(Archive(player_id=player.id, month="2026-11"))
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("month", ["2026-1", "202611", "2026/11", "nov-2026", ""])
def test_malformed_months_are_rejected(db_session: Session, month: str):
    """The month string is interpolated straight into the archive URL, so a
    malformed value becomes a 404 during ingestion rather than an obvious error
    at the point it was written."""
    player = _player(db_session)
    db_session.add(Archive(player_id=player.id, month=month))
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- vocabularies -----------------------------------------------------------


def test_unknown_time_class_is_rejected(db_session: Session):
    """Every report groups by time_class. An unexpected value would silently
    create a category no report knows how to display."""
    player = _player(db_session)
    db_session.add(_game(player, "uuid-bad-class", time_class="hyperbullet"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_unnormalised_result_is_rejected(db_session: Session):
    """Chess.com reports 'resigned', 'timeout', 'checkmated' and so on. Those
    belong in `termination`; `result` must already be normalised, or
    'win/loss/draw' reporting silently misses rows."""
    player = _player(db_session)
    db_session.add(_game(player, "uuid-raw-result", result="resigned"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_variant_games_are_stored_rather_than_dropped(db_session: Session):
    """Stockfish cannot evaluate chess960, so those games are ingested and
    marked skipped. Dropping them would make our game count disagree with
    Chess.com's, which looks exactly like an ingestion bug."""
    player = _player(db_session)
    game = _game(
        player,
        "uuid-960",
        rules="chess960",
        analysis_status="skipped",
        analysis_skip_reason="unsupported rules: chess960",
        start_fen="bbqnnrkr/pppppppp/8/8/8/8/PPPPPPPP/BBQNNRKR w KQkq - 0 1",
    )
    db_session.add(game)
    db_session.flush()

    stored = db_session.query(Game).filter_by(chesscom_uuid="uuid-960").one()
    assert stored.analysis_status == "skipped"
    assert stored.rules == "chess960"
    assert stored.start_fen is not None


def test_usernames_are_unique(db_session: Session):
    _player(db_session, "deitythemenace")
    db_session.add(Player(username="deitythemenace"))
    with pytest.raises(IntegrityError):
        db_session.flush()
