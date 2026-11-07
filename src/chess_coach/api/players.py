"""`/players`: add a player, read their progress (and, later, their report).

The API's only write is here, and it never calls Chess.com: adding a player
inserts a row and a `sync-player` job, and a sync worker does the rest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach.analytics import report
from chess_coach.analytics.progress import PlayerRow, find_player, progress
from chess_coach.api.schemas import (
    AddPlayerRequest,
    PlayerProgress,
    Report,
    Summary,
    TimeClass,
    iso_z,
)
from chess_coach.chesscom import normalize_username
from chess_coach.config import get_settings
from chess_coach.db import get_engine
from chess_coach.ingest.jobs import enqueue_sync

router = APIRouter(prefix="/players", tags=["players"])


def get_db() -> Engine:
    """The database, as a dependency so tests can point it at their own."""
    return get_engine()


def _progress_body(session: Session, player: PlayerRow) -> PlayerProgress:
    p = progress(session, player)
    return PlayerProgress(
        username=player.username,
        sync_status=player.sync_status,
        sync_error=player.sync_error,
        last_synced_at=iso_z(player.last_synced_at),
        analyzed_last_hour=p.analyzed_last_hour,
        time_classes=[asdict(tc) for tc in p.time_classes],
    )


def tracked_player(session: Session, username: str) -> PlayerRow:
    """The player, or 404. A name that is not even valid cannot be tracked."""
    try:
        name = normalize_username(username)
    except ValueError:
        name = None
    player = find_player(session, name) if name else None
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return player


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=PlayerProgress)
def add_player(body: AddPlayerRequest, db: Engine = Depends(get_db)) -> PlayerProgress:
    """Start tracking a player and queue a sync. Idempotent.

    Posting a tracked player again queues a re-sync unless one is already
    queued or running (two Chess.com requests when nothing is new).
    """
    with Session(db) as session, session.begin():
        session.execute(
            text(
                "INSERT INTO players (username, sync_status) VALUES (:u, 'pending') "
                "ON CONFLICT (username) DO NOTHING"
            ),
            {"u": body.username},
        )
        player = find_player(session, body.username)
        assert player is not None
        enqueue_sync(
            session, player.id, player.username, max_attempts=get_settings().job_max_attempts
        )
    with Session(db) as session:
        return _progress_body(session, player)


@router.get("/{username}", response_model=PlayerProgress)
def get_player(username: str, db: Engine = Depends(get_db)) -> PlayerProgress:
    with Session(db) as session:
        return _progress_body(session, tracked_player(session, username))


@router.get("/{username}/report", response_model=Report)
def get_report(
    username: str,
    time_class: TimeClass = Query(...),
    db: Engine = Depends(get_db),
) -> Report:
    """Everything the overview shows for one time class.

    All sections are read in one REPEATABLE READ snapshot. Workers keep
    completing games while the report is built; without one snapshot,
    `analyzed_games` could be counted before a game finished and
    `blunders_by_phase` after, and the parts would not add up.
    """
    with (
        db.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        Session(bind=conn) as session,
    ):
        player = tracked_player(session, username)
        pid = player.id
        summary = report.summary(session, pid, time_class)
        moves = report.move_summary(session, pid, time_class)
        analyzed = moves.analyzed_games > 0
        return Report(
            username=player.username,
            time_class=time_class,
            generated_at=iso_z(dt.datetime.now(dt.UTC)),
            games=report.standard_games(session, pid, time_class),
            analyzed_games=moves.analyzed_games,
            summary=Summary(**asdict(summary), blunders_per_game=moves.blunders_per_game),
            rating_trend=[
                {"date": p.date.isoformat(), "rating": p.rating, "games": p.games}
                for p in report.rating_trend(session, pid, time_class)
            ],
            openings=[asdict(o) for o in report.openings(session, pid, time_class)],
            blunders_by_phase=(
                [asdict(p) for p in report.blunders_by_phase(session, pid, time_class)]
                if analyzed
                else None
            ),
            # Daily clocks run in days; "under 30 seconds" means nothing there.
            time_pressure=(
                asdict(report.time_pressure(session, pid, time_class))
                if analyzed and time_class != "daily"
                else None
            ),
        )
