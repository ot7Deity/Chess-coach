"""Point of view, clamping, thresholds and phase. All pure."""

from __future__ import annotations

import chess
import pytest

from chess_coach.analysis.classify import (
    CLAMP_CP,
    MoverScore,
    classify,
    classify_move,
    mover_after,
    mover_before,
    non_pawn_material,
    phase,
)
from chess_coach.positions import Evaluation


def cp(value: int, best: str | None = "e2e4") -> Evaluation:
    return Evaluation(eval_cp=value, mate_in=None, best_move=best)


def mate(n: int, best: str | None = "e2e4") -> Evaluation:
    return Evaluation(eval_cp=None, mate_in=n, best_move=None if n == 0 else best)


# --- point of view ----------------------------------------------------------


def test_the_before_score_is_already_the_movers():
    assert mover_before(cp(35)) == MoverScore(cp=35, mate=None)
    assert mover_before(mate(-3)) == MoverScore(cp=None, mate=-3)


def test_the_after_score_is_negated_because_the_opponent_is_to_move():
    """+250 for the opponent to move is -250 for the player who just moved."""
    assert mover_after(cp(250)) == MoverScore(cp=-250, mate=None)
    assert mover_after(cp(-80)) == MoverScore(cp=80, mate=None)


def test_a_mate_against_the_opponent_is_a_mate_for_the_mover():
    # Opponent to move, being mated in 2 -> the mover mates in 2.
    assert mover_after(mate(-2)) == MoverScore(cp=None, mate=2)
    # Opponent to move, mating in 1 -> the mover is mated in 1: a blunder.
    assert mover_after(mate(1)) == MoverScore(cp=None, mate=-1)


def test_a_mate_already_given_is_handled_explicitly_not_by_negating_zero():
    """mate 0 = the side to move is checkmated. After a move, the side to move
    is the opponent, so the mover gave mate - the best possible outcome. Since
    -0 == 0, arithmetic cannot express this; the conversion has to."""
    after = mover_after(mate(0))
    assert after == MoverScore(cp=None, mate=0)
    assert after.clamped() == CLAMP_CP


def test_a_before_position_cannot_already_be_mate():
    with pytest.raises(ValueError):
        mover_before(mate(0))


# --- clamping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (MoverScore(cp=0, mate=None), 0),
        (MoverScore(cp=999, mate=None), 999),
        (MoverScore(cp=1300, mate=None), CLAMP_CP),
        (MoverScore(cp=-4000, mate=None), -CLAMP_CP),
        (MoverScore(cp=None, mate=5), CLAMP_CP),
        (MoverScore(cp=None, mate=-1), -CLAMP_CP),
        (MoverScore(cp=None, mate=0), CLAMP_CP),
    ],
)
def test_clamping(score, expected):
    assert score.clamped() == expected


# --- thresholds -------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [
        (0, -300, "blunder"),  # exactly at the threshold counts
        (0, -299, "mistake"),
        (0, -100, "mistake"),
        (0, -99, "inaccuracy"),
        (0, -50, "inaccuracy"),
        (0, -49, "good"),
        (0, 40, "good"),  # gained ground
    ],
)
def test_thresholds_are_inclusive(before, after, verdict):
    assert classify(before, after, "a2a3", "e2e4") == verdict


def test_the_engines_own_move_is_best_whatever_the_arithmetic_says():
    """The after-score is a fresh, one-ply-deeper search, so the engine's top
    move can still 'lose' centipawns on paper."""
    assert classify(0, -120, "e2e4", "e2e4") == "best"


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [
        # Already lost on both sides of the clamp: -1100 -> -1500 changes nothing.
        (cp(-1100), cp(1500), "good"),
        # The decision log's own example, -900 -> -1300: a 400 cp "blunder" raw,
        # but only the 100 cp down to the clamp counts, so it is a mistake.
        (cp(-900), cp(1300), "mistake"),
        # Already won: mate in 3 -> +1500 is still winning by a mile.
        (mate(3), cp(-1500), "good"),
        # The move that throws a drawn game: only 300 cp, but it is the one that matters.
        (cp(50), cp(250), "blunder"),
        # Walking into a forced mate from an equal position.
        (cp(0), mate(2), "blunder"),
        # Missing a forced mate but staying at +600: 1000 - 600 by the binding rule.
        (mate(2), cp(-600), "blunder"),
    ],
)
def test_classification_uses_clamped_scores(before, after, verdict):
    """The binding decision: clamp to +/-1000 before subtracting, so swings
    inside an already-decided position do not register."""
    assert classify_move(before, after, "a2a3") == verdict


def test_a_mating_move_is_best_even_when_the_engine_named_another_mate():
    """Two mates in one; the engine names the other. Giving mate is still the
    best possible result, never graded on arithmetic."""
    before = mate(1, best="h5f7")
    assert classify_move(before, mate(0), "d1h5") == "best"


def test_a_mating_move_is_best_from_any_starting_score():
    assert classify_move(cp(-2000), mate(0), "a1a8") == "best"


# --- phase ------------------------------------------------------------------


def test_the_start_position_is_the_opening_with_full_material():
    board = chess.Board()
    assert non_pawn_material(board) == 62
    assert phase(board) == "opening"


def test_move_eleven_with_material_on_is_middlegame():
    board = chess.Board("r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - 4 11")
    assert phase(board) == "middlegame"


def test_material_decides_the_endgame_even_on_move_one():
    """A game set up from an endgame position is an endgame from its first move."""
    assert phase(chess.Board("4k3/8/8/8/8/8/4P3/R3K2R w KQ - 0 1")) == "endgame"


@pytest.mark.parametrize(
    ("fen", "expected"),
    [
        # Two rooks and a bishop each = 26: endgame.
        ("2r1rbk1/5ppp/8/8/8/8/5PPP/2R1RBK1 w - - 0 30", "endgame"),
        # Queen and rook each = 28: still a middlegame.
        ("3qr1k1/5ppp/8/8/8/8/5PPP/3QR1K1 w - - 0 30", "middlegame"),
    ],
)
def test_the_endgame_threshold(fen, expected):
    assert phase(chess.Board(fen)) == expected
