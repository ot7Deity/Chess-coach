"""Replaying a game and grading every ply, with a fake evaluator (pure) and
with the real engine (`engine` tests)."""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

import chess
import pytest

from chess_coach.analysis.game import BadGame, analyze_game, replay
from chess_coach.engine import StockfishEngine
from chess_coach.positions import Evaluation, fen_hash, normalize_fen
from tests.games import HANGS_A_BISHOP, LONG_GAME, SCHOLARS_MATE, chesscom_pgn, clocks_for


class FakeEvaluator:
    """Deterministic made-up scores, side-to-move POV, and a call log."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, board: chess.Board) -> Evaluation:
        self.calls.append(normalize_fen(board))
        if board.is_checkmate():
            return Evaluation(eval_cp=None, mate_in=0, best_move=None)
        digest = hashlib.sha256(normalize_fen(board).encode()).digest()
        score = int.from_bytes(digest[:2], "big") % 600 - 300
        best = next(iter(board.legal_moves)).uci()
        if digest[2] < 8:  # sprinkle in some mate scores
            return Evaluation(eval_cp=None, mate_in=(digest[3] % 5) - 2 or 3, best_move=best)
        return Evaluation(eval_cp=score, mate_in=None, best_move=best)


def _analyze(sans=LONG_GAME, **kw):
    evaluator = kw.pop("evaluate", FakeEvaluator())
    params = dict(start_fen=None, player_color="white", time_control="180+2")
    params.update(kw)
    pgn = params.pop("pgn", None) or chesscom_pgn(sans, time_control=params["time_control"])
    return analyze_game(pgn, evaluate=evaluator, **params), evaluator


def test_every_ply_becomes_one_row_numbered_from_one():
    rows, _ = _analyze()
    assert [r.ply for r in rows] == list(range(1, len(LONG_GAME) + 1))
    assert [r.san for r in rows] == LONG_GAME


def test_one_evaluation_per_position():
    """N plies, N + 1 positions, N + 1 calls - each position serves as the
    "after" of one ply and the "before" of the next."""
    rows, evaluator = _analyze()
    assert len(evaluator.calls) == len(rows) + 1


def test_the_after_score_of_one_ply_is_minus_the_before_score_of_the_next():
    """The point-of-view invariant, over a whole game. A sign error anywhere in
    the conversion breaks it."""
    rows, _ = _analyze()
    for this, following in itertools.pairwise(rows):
        if this.eval_cp_after is not None:
            assert this.eval_cp_after == -following.eval_cp_before
        else:
            assert this.mate_in_after == -following.mate_in_before


def test_exactly_one_of_cp_and_mate_is_set_on_both_sides():
    rows, _ = _analyze()
    for r in rows:
        assert (r.eval_cp_before is None) != (r.mate_in_before is None)
        assert (r.eval_cp_after is None) != (r.mate_in_after is None)


def test_player_moves_follow_the_tracked_players_colour():
    white_rows, _ = _analyze(player_color="white")
    black_rows, _ = _analyze(player_color="black")
    assert [r.is_player_move for r in white_rows[:4]] == [True, False, True, False]
    assert [r.is_player_move for r in black_rows[:4]] == [False, True, False, True]


def test_fen_hash_is_the_position_the_move_was_played_from():
    rows, _ = _analyze(sans=["e4", "e5"])
    board = chess.Board()
    assert rows[0].fen_hash == fen_hash(board)
    board.push_san("e4")
    assert rows[1].fen_hash == fen_hash(board)


def test_clock_readings_and_move_times_come_from_the_pgn():
    rows, _ = _analyze()
    expected_clocks = clocks_for(LONG_GAME, 180, 2)
    assert [r.clock_seconds for r in rows] == pytest.approx(expected_clocks)
    # clocks_for charges (k % 7) seconds on ply index k, first moves free.
    assert [r.move_time_seconds for r in rows[:6]] == [0, 0, 2, 3, 4, 5]


def test_a_daily_game_has_clocks_but_no_move_times():
    rows, _ = _analyze(sans=["e4", "e5"], time_control="1/86400")
    assert [r.move_time_seconds for r in rows] == [None, None]


def test_checkmate_is_the_movers_best_move_and_needs_no_engine_call_semantics():
    rows, _ = _analyze(sans=SCHOLARS_MATE)
    last = rows[-1]
    assert last.san == "Qxf7#"
    assert (last.eval_cp_after, last.mate_in_after) == (None, 0)
    assert last.classification == "best"


def test_a_game_from_a_set_up_position_is_replayed_from_start_fen():
    fen = "4k3/8/8/8/8/8/4P3/R3K2R w KQ - 0 1"
    sans = ["O-O", "Kd7", "Rfd1+"]
    pgn = chesscom_pgn(sans, fen=fen)
    rows, evaluator = _analyze(sans=sans, pgn=pgn, start_fen=fen)
    assert [r.san for r in rows] == sans
    assert evaluator.calls[0] == normalize_fen(chess.Board(fen))
    assert rows[0].phase == "endgame"


def test_moves_that_do_not_fit_start_fen_are_rejected_not_graded():
    """If start_fen and the moves disagree, grading them on the wrong board
    would produce confident nonsense."""
    pgn = chesscom_pgn(["e4", "e5"])
    with pytest.raises(BadGame):
        _analyze(pgn=pgn, start_fen="4k3/8/8/8/8/8/8/4K3 w - - 0 1")


@pytest.mark.parametrize("pgn", ["", "1. e4 e5 2. Ke3", "1. e4 Zz9 2. Nf3"])
def test_an_unreadable_pgn_is_a_bad_game(pgn):
    """python-chess does not raise on a bad move: it stops, records the error
    on `game.errors` and returns the moves so far. Grading that prefix as if it
    were the game would silently drop the rest, so any error rejects the game."""
    with pytest.raises(BadGame):
        replay(pgn, None)


def test_a_game_with_no_moves_has_no_rows_and_costs_no_engine_calls():
    rows, evaluator = _analyze(sans=[])
    assert rows == []
    assert evaluator.calls == []


def test_a_failing_heartbeat_abandons_the_game():
    calls = []

    def on_position():
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError):
        _analyze(on_position=on_position)
    assert len(calls) == 3


# --- with the real engine ---------------------------------------------------


@pytest.mark.engine
def test_hanging_a_bishop_is_a_blunder_from_the_movers_point_of_view(stockfish_path: Path):
    with StockfishEngine(stockfish_path, depth=8, hash_mb=16) as engine:
        rows, _ = _analyze(sans=HANGS_A_BISHOP, evaluate=engine.evaluate)
    ba6, bxa6 = rows[2], rows[3]
    assert ba6.san == "Ba6" and ba6.is_player_move
    assert ba6.classification == "blunder"
    # White's own view after giving the bishop away: clearly worse.
    assert ba6.eval_cp_after < -150
    # Black's view before taking it: clearly better - the same engine call.
    assert bxa6.eval_cp_before == -ba6.eval_cp_after
    assert bxa6.eval_cp_before > 300
    # Not "best": the engine prefers Nxa6, keeping the pawns intact (+452 vs
    # +377 for Black at depth 16, a 75 cp inaccuracy; at depth 8 the gap is
    # 126 cp). Taking with the pawn is still nowhere near a blunder.
    assert bxa6.classification in ("inaccuracy", "mistake")
