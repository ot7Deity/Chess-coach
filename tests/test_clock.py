"""Time controls and time spent per move. All pure."""

from __future__ import annotations

import chess
import pytest

from chess_coach.analysis.clock import TimeControl, move_times, parse_time_control

W, B = chess.WHITE, chess.BLACK


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("180+2", TimeControl(180, 2)),
        ("600", TimeControl(600, 0)),
        ("60+1", TimeControl(60, 1)),
        ("30+0.5", TimeControl(30, 0.5)),
        ("1/86400", None),  # daily: a per-move allowance, not a running clock
        ("", None),
        (None, None),
        ("-", None),
        ("abc+2", None),
        ("0", None),
    ],
)
def test_parse_time_control(value, expected):
    assert parse_time_control(value) == expected


def test_time_spent_uses_each_players_own_previous_reading_and_the_increment():
    # 180+2. Readings include the increment.
    clocks = [182.0, 182.0, 175.5, 170.0, 177.5]
    movers = [W, B, W, B, W]
    # White's first move: 180 - 182 + 2 = 0 (these synthetic readings spend nothing on it).
    # White's second: 182 - 175.5 + 2 = 8.5. Black's second: 182 - 170 + 2 = 14.
    assert move_times(clocks, movers, TimeControl(180, 2)) == [0.0, 0.0, 8.5, 14.0, 0.0]


def test_time_spent_without_increment():
    clocks = [180.0, 179.9, 177.1]
    assert move_times(clocks, [W, B, W], TimeControl(180, 0)) == [0.0, 0.1, 2.9]


def test_float_noise_is_rounded_away():
    """180 - 179.9 is 0.09999999999999432 in binary floating point."""
    (_, spent) = move_times([180.0, 179.9], [W, B], TimeControl(180, 0))
    assert spent == 0.1


def test_a_reading_that_rounds_above_the_previous_one_is_not_negative():
    """Clocks are shown to a tenth of a second; a near-instant move with the
    increment added can read 0.1 s higher than the arithmetic allows."""
    # 61.0 - 62.1 + 1 = -0.1 for the second move.
    spent = move_times([61.0, 62.1], [W, W], TimeControl(60, 1))
    assert spent == [0.0, 0.0]


def test_a_missing_reading_costs_that_move_and_the_next_one_by_the_same_player():
    clocks = [180.0, 180.0, None, 175.0, 170.0, 171.0]
    movers = [W, B, W, B, W, B]
    assert move_times(clocks, movers, TimeControl(180, 0)) == [0.0, 0.0, None, 5.0, None, 4.0]


def test_a_game_where_black_moves_first():
    """Games set up from a position can start with black to move."""
    assert move_times([300.0, 300.0, 290.0], [B, W, B], TimeControl(300, 0)) == [0.0, 0.0, 10.0]


def test_no_time_control_means_no_move_times():
    assert move_times([100.0, 99.0], [W, B], None) == [None, None]


def test_one_reading_per_ply_is_required():
    with pytest.raises(ValueError):
        move_times([1.0], [W, B], TimeControl(60, 0))
