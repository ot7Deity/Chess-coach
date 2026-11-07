"""Report sections built from engine analysis, against hand-computed answers."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from chess_coach.analytics import report
from tests.report_data import add_game, add_moves, add_player

pytestmark = pytest.mark.postgres


def _mine(clock, cls, phase="middlegame"):
    return {"is_player_move": True, "clock_seconds": clock, "classification": cls, "phase": phase}


def _theirs(clock=100.0, cls="blunder", phase="middlegame"):
    # Opponent blunders everywhere: none of them may count.
    return {"is_player_move": False, "clock_seconds": clock, "classification": cls, "phase": phase}


@pytest.fixture
def data(clean_engine: Engine) -> int:
    me = add_player(clean_engine)

    # Game A, 180+2, the player is white (odd plies).
    a = add_game(clean_engine, me, analysis_status="complete", time_control="180+2")
    add_moves(
        clean_engine,
        a,
        [
            _mine(182.0, "good", "opening"),  # starts on the base 180: normal
            _theirs(),
            _mine(40.0, "blunder", "opening"),  # starts on 182: normal
            _theirs(),
            _mine(25.0, "mistake"),  # starts on 40: normal
            _theirs(),
            _mine(10.0, "blunder", "endgame"),  # starts on 25: LOW
            _theirs(),
            _mine(5.0, "best", "endgame"),  # starts on 10: LOW
        ],
    )

    # Game B, the player is black (even plies), with a missing clock reading.
    b = add_game(clean_engine, me, analysis_status="complete", color="black")
    add_moves(
        clean_engine,
        b,
        [
            _theirs(),
            _mine(None, "good", "opening"),  # first move: starts on the base, normal
            _theirs(),
            _mine(20.0, "good"),  # previous reading missing: in neither bucket
            _theirs(),
            _mine(15.0, "good"),  # starts on 20: LOW
        ],
    )

    # Neither of these may count: not analysed yet, and another time class.
    c = add_game(clean_engine, me, analysis_status="pending")
    add_moves(clean_engine, c, [_mine(5.0, "blunder")])
    d = add_game(clean_engine, me, analysis_status="complete", time_class="rapid")
    add_moves(clean_engine, d, [_mine(5.0, "blunder")])
    return me


def test_blunders_per_game_counts_only_the_players_own_blunders(clean_engine: Engine, data):
    with Session(clean_engine) as s:
        got = report.move_summary(s, data, "blitz")
    assert got == report.MoveSummary(analyzed_games=2, blunders_per_game=1.0)


def test_blunders_by_phase_is_three_rows_that_sum_to_the_headline(clean_engine: Engine, data):
    with Session(clean_engine) as s:
        rows = report.blunders_by_phase(s, data, "blitz")
    assert rows == [
        report.PhaseBlunders("opening", 3, 1),
        report.PhaseBlunders("middlegame", 3, 0),
        report.PhaseBlunders("endgame", 2, 1),
    ]
    assert sum(r.blunders for r in rows) / 2 == 1.0


def test_time_pressure_uses_the_clock_at_the_start_of_each_move(clean_engine: Engine, data):
    with Session(clean_engine) as s:
        got = report.time_pressure(s, data, "blitz")
    assert got.threshold_seconds == 30
    assert got.low_clock_games == 2
    assert (got.low_clock.moves, got.low_clock.blunders) == (3, 1)
    assert got.low_clock.accuracy_pct == pytest.approx(100 * 2 / 3)
    assert (got.normal_clock.moves, got.normal_clock.blunders) == (4, 1)
    assert got.normal_clock.accuracy_pct == pytest.approx(50.0)


def test_no_analysed_games_means_empty_sections(clean_engine: Engine):
    me = add_player(clean_engine)
    add_game(clean_engine, me)  # pending
    with Session(clean_engine) as s:
        assert report.move_summary(s, me, "blitz") == report.MoveSummary(0, None)
        assert [r.player_moves for r in report.blunders_by_phase(s, me, "blitz")] == [0, 0, 0]
        pressure = report.time_pressure(s, me, "blitz")
    assert pressure.low_clock == report.ClockBucket(0, None, 0)
    assert pressure.normal_clock == report.ClockBucket(0, None, 0)


def test_a_daily_games_first_move_has_no_base_time(clean_engine: Engine):
    me = add_player(clean_engine)
    g = add_game(
        clean_engine, me, analysis_status="complete", time_class="daily", time_control="1/86400"
    )
    add_moves(clean_engine, g, [_mine(86400.0, "good")])
    with Session(clean_engine) as s:
        got = report.time_pressure(s, me, "daily")
    assert got.low_clock.moves == got.normal_clock.moves == 0
