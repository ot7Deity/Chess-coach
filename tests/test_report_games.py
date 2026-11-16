"""Report sections built from ingested games, against hand-computed answers.

These same functions were also checked against the dashboard's real-data
fixture (deitythemenace, 4,549 standard games): every summary count, every
rating-trend point and every opening row matched in all four time classes.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach.analytics import report
from tests.report_data import add_game, add_player, day

pytestmark = pytest.mark.postgres


def _s(engine: Engine) -> Session:
    return Session(engine)


# --- summary ----------------------------------------------------------------


def test_the_record_counts_only_rated_standard_games_of_this_class_and_player(
    clean_engine: Engine,
):
    me, other = add_player(clean_engine), add_player(clean_engine, "someone")
    for result in ("win", "win", "win", "draw", "loss"):
        add_game(clean_engine, me, result=result)
    add_game(clean_engine, me, result="win", rated=False)  # unrated: not in the record
    add_game(clean_engine, me, result="win", rules="chess960")  # variant: no
    add_game(clean_engine, me, result="win", time_class="rapid")  # other class: no
    add_game(clean_engine, other, result="loss")  # other player: no
    with _s(clean_engine) as s:
        got = report.summary(s, me, "blitz")
        total = report.standard_games(s, me, "blitz")
    # Rated only, so it reconciles with Chess.com's stats page.
    assert (got.games, got.wins, got.draws, got.losses) == (5, 3, 1, 1)
    # Draws are not half-wins.
    assert got.win_rate_pct == pytest.approx(100 * 3 / 5)
    # The rest of the report's population includes the unrated game.
    assert total == 6


def test_summary_with_no_games_is_zeros_and_nulls(clean_engine: Engine):
    me = add_player(clean_engine)
    with _s(clean_engine) as s:
        got = report.summary(s, me, "daily")
    assert got == report.Summary(0, 0, 0, 0, None, None)


# --- rating trend -----------------------------------------------------------


def test_one_point_per_utc_day_with_the_rating_after_its_last_game(clean_engine: Engine):
    me = add_player(clean_engine)
    add_game(clean_engine, me, player_rating=1000, played_at=day("2026-10-01", 9))
    add_game(clean_engine, me, player_rating=1012, played_at=day("2026-10-01", 23, 30))
    add_game(clean_engine, me, player_rating=1007, played_at=day("2026-10-01", 15))
    # 00:30 UTC on the 2nd is still the 1st in New York; it is the 2nd here.
    add_game(clean_engine, me, player_rating=1020, played_at=day("2026-10-02", 0, 30))
    add_game(clean_engine, me, player_rating=1500, rated=False, played_at=day("2026-10-03"))
    with _s(clean_engine) as s:
        points = report.rating_trend(s, me, "blitz")
    assert points == [
        report.RatingPoint(dt.date(2026, 10, 1), 1012, 3),
        report.RatingPoint(dt.date(2026, 10, 2), 1020, 1),
    ]


def test_the_trend_uses_the_utc_day_whatever_the_session_time_zone(clean_engine: Engine):
    """A session in Los Angeles must not move a 02:00 UTC game to the day before."""
    me = add_player(clean_engine)
    add_game(clean_engine, me, player_rating=1000, played_at=day("2026-10-02", 2))
    with _s(clean_engine) as s:
        s.execute(text("SET TIME ZONE 'America/Los_Angeles'"))
        (point,) = report.rating_trend(s, me, "blitz")
    assert point.date == dt.date(2026, 10, 2)


# --- openings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("Queens Pawn Opening Zukertort Variation", "Queens Pawn Opening"),
        ("Queens Pawn Opening Zukertort Chigorin Variation", "Queens Pawn Opening"),
        ("Indian Game London System", "Indian Game"),
        ("Englund Gambit", "Englund Gambit"),
        ("Caro Kann Defense Advance Variation", "Caro Kann Defense"),
        ("Kings Pawn", "Kings Pawn"),  # no family word: the whole name
        ("Gameboy Variation", "Gameboy Variation"),  # 'Game' must be a whole word
    ],
)
def test_opening_family(clean_engine: Engine, name: str, family: str):
    with clean_engine.connect() as c:
        got = c.scalar(
            text(f"SELECT {report.OPENING_FAMILY_SQL} FROM (SELECT :n AS opening_name) g"),
            {"n": name},
        )
    assert got == family


def test_openings_rating_change_and_order_hand_computed(clean_engine: Engine):
    """Ratings are the rating *after* each game, so change = rating - previous
    rated game's rating, over every rated game in the class in time order."""
    me = add_player(clean_engine)
    t = iter(range(1, 100))

    def game(rating, opening, *, color="white", result="win", rated=True):
        add_game(
            clean_engine,
            me,
            player_rating=rating,
            opening_name=opening,
            color=color,
            result=result,
            rated=rated,
            played_at=day("2026-10-01") + dt.timedelta(minutes=next(t)),
        )

    game(1000, "Italian Game Two Knights Defense")  # first rated game: 0
    game(1010, "Italian Game")  # +10
    game(1005, None)  # -5, not in any opening row, but it is the previous game below
    game(1020, "Italian Game Giuoco Piano", result="draw")  # +15 (from 1005)
    game(1012, "Italian Game", result="loss")  # -8
    game(2000, "Italian Game", rated=False)  # unrated: counted, no rating change
    game(1030, "Italian Game")  # +18 (from 1012; the unrated game is skipped)
    game(1025, "Sicilian Defense Najdorf", color="black", result="loss")  # -5
    game(1000, "Sicilian Defense", color="black", result="loss")  # -25
    game(990, "Sicilian Defense Dragon", color="black", result="loss")  # -10
    game(1001, "Sicilian Defense", color="black")  # +11
    game(995, "Sicilian Defense", color="black", result="draw")  # -6
    for rating in (1000, 1001, 1002, 1003):  # only four: below the threshold
        game(rating, "Caro Kann Defense")
    game(1004, "Undefined")  # never an opening row
    game(1010, "Italian Game", color="black")  # same family, other colour: its own row

    with _s(clean_engine) as s:
        rows = report.openings(s, me, "blitz")
    assert [
        (r.opening, r.color, r.games, r.wins, r.draws, r.losses, r.rating_change) for r in rows
    ] == [
        ("Sicilian Defense", "black", 5, 1, 1, 3, -35),
        ("Italian Game", "white", 6, 4, 1, 1, 35),
    ]
    assert rows[1].win_rate_pct == pytest.approx(100 * 4 / 6)


def test_an_opening_needs_five_games(clean_engine: Engine):
    me = add_player(clean_engine)
    for _ in range(4):
        add_game(clean_engine, me, opening_name="French Defense")
    with _s(clean_engine) as s:
        assert report.openings(s, me, "blitz") == []
    add_game(clean_engine, me, opening_name="French Defense Winawer")
    with _s(clean_engine) as s:
        (row,) = report.openings(s, me, "blitz")
    assert (row.opening, row.games) == ("French Defense", 5)


def test_ties_sort_by_games_then_name_then_colour(clean_engine: Engine):
    me = add_player(clean_engine)
    for opening, color, n in (
        ("Scandinavian Defense", "white", 5),
        ("Alekhine Defense", "white", 5),
        ("Alekhine Defense", "black", 5),
        ("Zukertort Opening", "white", 6),
    ):
        for _ in range(n):
            add_game(clean_engine, me, opening_name=opening, color=color, rated=False)
    with _s(clean_engine) as s:
        rows = report.openings(s, me, "blitz")
    assert [(r.opening, r.color) for r in rows] == [
        ("Zukertort Opening", "white"),
        ("Alekhine Defense", "black"),
        ("Alekhine Defense", "white"),
        ("Scandinavian Defense", "white"),
    ]
