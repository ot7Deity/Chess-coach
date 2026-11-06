"""The report for one player and one time class (`Report` in the contract).

Every query is scoped to one player, one time class and standard chess
(`rules = 'chess'`). Results are never mixed across time classes, and variants
have their own Chess.com ratings, so counting them would make our totals
disagree with the player's Chess.com stats page.

This module holds the sections built from ingested games - complete as soon
as the sync is. The sections built from engine analysis follow.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from chess_coach.analytics.progress import CURRENT_RATING_SQL

# The opening *family*: the shortest prefix of Chess.com's opening name that
# ends in one of these words, or the whole name if none does. "Queens Pawn
# Opening Zukertort Variation" -> "Queens Pawn Opening". From the contract.
OPENING_FAMILY_SQL = (
    "COALESCE(substring(g.opening_name FROM "
    "'^(.*?(Opening|Defense|Defence|Game|Gambit|System|Attack))( |$)'), g.opening_name)"
)
MIN_OPENING_GAMES = 5


@dataclass(frozen=True)
class Summary:
    games: int
    wins: int
    draws: int
    losses: int
    win_rate_pct: float | None
    current_rating: int | None


@dataclass(frozen=True)
class RatingPoint:
    date: dt.date
    rating: int
    games: int


@dataclass(frozen=True)
class OpeningResult:
    opening: str
    color: str
    games: int
    wins: int
    draws: int
    losses: int
    win_rate_pct: float
    rating_change: int


_SCOPE = "g.player_id = :player_id AND g.time_class = :time_class AND g.rules = 'chess'"


def _pct(part: int, whole: int) -> float | None:
    """0-100, unrounded, as a float - never Postgres numeric's 20 digits."""
    return 100.0 * part / whole if whole else None


def summary(session: Session, player_id: int, time_class: str) -> Summary:
    row = session.execute(
        text(
            f"""
            SELECT count(*) AS games,
                   count(*) FILTER (WHERE g.result = 'win') AS wins,
                   count(*) FILTER (WHERE g.result = 'draw') AS draws,
                   count(*) FILTER (WHERE g.result = 'loss') AS losses,
                   ({CURRENT_RATING_SQL.format(time_class=":time_class")}) AS current_rating
            FROM games g
            WHERE {_SCOPE}
            """
        ),
        {"player_id": player_id, "time_class": time_class},
    ).one()
    return Summary(
        games=row.games,
        wins=row.wins,
        draws=row.draws,
        losses=row.losses,
        win_rate_pct=_pct(row.wins, row.games),
        current_rating=row.current_rating,
    )


def rating_trend(session: Session, player_id: int, time_class: str) -> list[RatingPoint]:
    """One point per UTC day with a rated game: the rating after that day's last one.

    The UTC day, explicitly - never the server's zone, and never Chess.com's
    archive month, which is filed in US Pacific time.
    """
    rows = session.execute(
        text(
            f"""
            SELECT (g.played_at AT TIME ZONE 'UTC')::date AS day,
                   (array_agg(g.player_rating ORDER BY g.played_at DESC, g.id DESC))[1]
                       AS rating,
                   count(*) AS games
            FROM games g
            WHERE {_SCOPE} AND g.rated AND g.player_rating IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"player_id": player_id, "time_class": time_class},
    )
    return [RatingPoint(date=r.day, rating=r.rating, games=r.games) for r in rows]


def openings(session: Session, player_id: int, time_class: str) -> list[OpeningResult]:
    """Results and rating change per (opening family, colour), groups of 5+ games.

    `games.player_rating` is the rating *after* the game, so a game's rating
    change is its rating minus the previous rated standard game's in the same
    class. The LAG runs over every rated game in the class *before* games with
    no opening are filtered out - those still count as "the previous game".
    The first rated game has no previous one and contributes 0.
    """
    rows = session.execute(
        text(
            f"""
            WITH g AS (
                SELECT g.color, g.result,
                       CASE WHEN g.opening_name IS NULL OR g.opening_name = 'Undefined'
                            THEN NULL ELSE {OPENING_FAMILY_SQL} END AS opening,
                       CASE WHEN g.rated THEN
                           g.player_rating - lag(g.player_rating) OVER (
                               PARTITION BY g.rated ORDER BY g.played_at, g.id)
                       END AS rating_change
                FROM games g
                WHERE {_SCOPE}
            )
            SELECT opening, color,
                   count(*) AS games,
                   count(*) FILTER (WHERE result = 'win') AS wins,
                   count(*) FILTER (WHERE result = 'draw') AS draws,
                   count(*) FILTER (WHERE result = 'loss') AS losses,
                   COALESCE(sum(rating_change), 0)::int AS rating_change
            FROM g
            WHERE opening IS NOT NULL
            GROUP BY opening, color
            HAVING count(*) >= :min_games
            ORDER BY rating_change, games DESC, opening, color
            """
        ),
        {"player_id": player_id, "time_class": time_class, "min_games": MIN_OPENING_GAMES},
    )
    return [
        OpeningResult(
            opening=r.opening,
            color=r.color,
            games=r.games,
            wins=r.wins,
            draws=r.draws,
            losses=r.losses,
            win_rate_pct=100.0 * r.wins / r.games,
            rating_change=r.rating_change,
        )
        for r in rows
    ]
