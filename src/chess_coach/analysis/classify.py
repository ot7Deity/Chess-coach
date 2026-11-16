"""Turning two engine scores into a verdict on a move. Pure functions.

Point of view is the whole difficulty here. The engine scores a position for
the side *to move* in it (the `positions.Evaluation` contract). A move's
"before" position is scored for the mover; its "after" position is scored for
the opponent, who is now to move. So:

    mover's eval before ply k  =   engine score of the position before it
    mover's eval after  ply k  = - engine score of the position after it

and because one engine call per position serves both plies around it,
`eval_after(k) == -eval_before(k + 1)` always holds - a free consistency check
the tests use.

The one value negation cannot handle is a mate that has already happened.
`mate_in = 0` means "the side to move is checkmated", and `-0 == 0`, so
negating it would silently turn "I just delivered mate" into "I am mated".
`mover_after` handles it explicitly, and in the mover's terms
`mate_in_after = 0` means "this move gave checkmate". (`mate_in_before = 0`
cannot occur: the mover had a legal move.)
"""

from __future__ import annotations

from dataclasses import dataclass

import chess

from chess_coach.positions import Evaluation

# Both evaluations are clamped to +/-CLAMP_CP before subtracting, so "already
# winning by a mile" and "already lost" each act as one state and swings inside
# them stop counting. -1100 -> -1500 counts for nothing. -900 -> -1300 is a
# 400 cp "blunder" raw but only the 100 cp down to the clamp counts, so it is a
# mistake - the clamp softens that case rather than erasing it. See
# INTERVIEW_PREP.md.
CLAMP_CP = 1000

BLUNDER_CP = 300
MISTAKE_CP = 100
INACCURACY_CP = 50

# Phase thresholds. Non-pawn material in pawn units, both sides together; the
# starting position has 62 (2 x (Q9 + R5 + R5 + B3 + B3 + N3 + N3)).
_PIECE_VALUES = {chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
ENDGAME_MATERIAL = 26  # e.g. two rooks and a minor piece each, or less
OPENING_LAST_MOVE = 10  # moves 1-10 are the opening unless already an endgame


@dataclass(frozen=True)
class MoverScore:
    """A score from the mover's point of view, as stored in `moves`.

    Exactly one of `cp` / `mate` is set. `mate > 0`: the mover mates in that
    many; `mate < 0`: the mover is mated in that many; `mate == 0`: the mover
    has just given mate (only ever an "after" score).
    """

    cp: int | None
    mate: int | None

    def clamped(self) -> int:
        """Centipawns in [-CLAMP_CP, CLAMP_CP]; a forced mate is the end of the
        scale in its direction, and a mate already given is the top of it."""
        if self.mate is not None:
            return -CLAMP_CP if self.mate < 0 else CLAMP_CP
        assert self.cp is not None
        return max(-CLAMP_CP, min(CLAMP_CP, self.cp))


def mover_before(ev: Evaluation) -> MoverScore:
    """The mover is the side to move in the "before" position: no change."""
    if ev.mate_in == 0:
        raise ValueError("a position with the side to move already mated has no move to grade")
    return MoverScore(cp=ev.eval_cp, mate=ev.mate_in)


def mover_after(ev: Evaluation) -> MoverScore:
    """The engine scored the "after" position for the opponent: negate it -
    except a mate already given, which is the mover's best possible result."""
    if ev.mate_in == 0:
        # Opponent to move and checkmated. Not `-ev.mate_in`: that is the same 0
        # with its meaning flipped by convention rather than by arithmetic.
        return MoverScore(cp=None, mate=0)
    if ev.mate_in is not None:
        return MoverScore(cp=None, mate=-ev.mate_in)
    assert ev.eval_cp is not None
    return MoverScore(cp=-ev.eval_cp, mate=None)


def classify(before_cp: int, after_cp: int, played_uci: str, best_uci: str | None) -> str:
    """Verdict on one move from clamped mover-POV scores.

    Playing the engine's own choice is 'best' whatever the arithmetic says: the
    score after a move is a fresh search one ply deeper, so it can differ from
    the score before by more than noise, and a player cannot be faulted for
    finding the top move.
    """
    if best_uci is not None and played_uci == best_uci:
        return "best"
    loss = before_cp - after_cp
    if loss >= BLUNDER_CP:
        return "blunder"
    if loss >= MISTAKE_CP:
        return "mistake"
    if loss >= INACCURACY_CP:
        return "inaccuracy"
    return "good"


def classify_move(before: Evaluation, after: Evaluation, played_uci: str) -> str:
    """Verdict on one move from the engine's side-to-move evaluations of the
    positions before and after it.

    Giving checkmate is always 'best'. Without that rule it would usually come
    out 'best' anyway, but not always: in a position with two mates in one the
    engine names one, and the other would be graded on clamped arithmetic.
    """
    if after.mate_in == 0:
        return "best"
    return classify(
        mover_before(before).clamped(), mover_after(after).clamped(), played_uci, before.best_move
    )


def non_pawn_material(board: chess.Board) -> int:
    return sum(
        value * len(board.pieces(piece, color))
        for piece, value in _PIECE_VALUES.items()
        for color in chess.COLORS
    )


def phase(board: chess.Board) -> str:
    """Game phase of the position a move was played *from*.

    A deliberately simple, explainable rule: an endgame is decided by material
    alone (so a game set up from an endgame position is an endgame from move
    one), the opening is the first ten moves, everything else is middlegame.
    Recomputable from the PGN without the engine if it is ever refined.
    """
    if non_pawn_material(board) <= ENDGAME_MATERIAL:
        return "endgame"
    if board.fullmove_number <= OPENING_LAST_MOVE:
        return "opening"
    return "middlegame"
