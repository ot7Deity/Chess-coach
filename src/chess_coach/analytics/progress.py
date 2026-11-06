"""Sync and analysis progress for one player (`PlayerProgress` in the contract)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from chess_coach.models import TIME_CLASSES


@dataclass(frozen=True)
class PlayerRow:
    id: int
    username: str
    sync_status: str
    sync_error: str | None
    last_synced_at: dt.datetime | None


@dataclass(frozen=True)
class TimeClassProgress:
    time_class: str
    games: int
    analyzed: int
    pending: int
    failed: int
    skipped: int
    current_rating: int | None


@dataclass(frozen=True)
class Progress:
    player: PlayerRow
    analyzed_last_hour: int
    time_classes: list[TimeClassProgress]


# "Rating after the most recent rated standard game in this class." Ties on
# played_at break on id, the same order the rating-change LAG uses, so the two
# can never disagree about which game was last.
CURRENT_RATING_SQL = """
    SELECT g.player_rating FROM games g
    WHERE g.player_id = :player_id AND g.time_class = {time_class}
      AND g.rated AND g.rules = 'chess' AND g.player_rating IS NOT NULL
    ORDER BY g.played_at DESC, g.id DESC
    LIMIT 1
"""

# Every time class, in contract order, even with no games: a fixed shape means
# the UI never has to guess whether a missing class is "zero" or "not loaded".
_CLASSES = ", ".join(f"('{tc}', {i})" for i, tc in enumerate(TIME_CLASSES))

_PROGRESS = text(
    f"""
    WITH tc(time_class, ord) AS (VALUES {_CLASSES})
    SELECT tc.time_class,
           count(g.id) AS games,
           count(g.id) FILTER (WHERE g.analysis_status = 'complete') AS analyzed,
           count(g.id) FILTER (WHERE g.analysis_status IN ('pending', 'running')) AS pending,
           count(g.id) FILTER (WHERE g.analysis_status = 'error') AS failed,
           count(g.id) FILTER (WHERE g.analysis_status = 'skipped') AS skipped,
           ({CURRENT_RATING_SQL.format(time_class="tc.time_class")}) AS current_rating
    FROM tc
    LEFT JOIN games g ON g.player_id = :player_id AND g.time_class = tc.time_class
    GROUP BY tc.time_class, tc.ord
    ORDER BY tc.ord
    """
)


def find_player(session: Session, username: str) -> PlayerRow | None:
    row = session.execute(
        text(
            "SELECT id, username, sync_status, sync_error, last_synced_at "
            "FROM players WHERE username = :u"
        ),
        {"u": username},
    ).one_or_none()
    return PlayerRow(*row) if row is not None else None


def progress(session: Session, player: PlayerRow) -> Progress:
    classes = [
        TimeClassProgress(*row) for row in session.execute(_PROGRESS, {"player_id": player.id})
    ]
    last_hour = session.execute(
        text(
            "SELECT count(*) FROM games WHERE player_id = :player_id "
            "AND analyzed_at > now() - interval '1 hour'"
        ),
        {"player_id": player.id},
    ).scalar_one()
    return Progress(player=player, analyzed_last_hour=last_hour, time_classes=classes)
