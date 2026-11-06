"""The position-identity contract shared by the analysis worker and the cache."""

from __future__ import annotations

import chess
import pytest

from chess_coach.positions import Evaluation, NullCache, fen_hash, normalize_fen


def test_move_counters_do_not_split_a_position():
    a = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 17 40")
    assert fen_hash(a) == fen_hash(b)


def test_an_en_passant_square_counts_only_when_the_capture_is_legal():
    after_e4 = chess.Board()
    after_e4.push_san("e4")  # FEN says e3, but no black pawn can take
    same_without_square = chess.Board(after_e4.fen().replace(" e3 ", " - "))
    assert fen_hash(after_e4) == fen_hash(same_without_square)

    ep_live = chess.Board("rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 3")
    ep_gone = chess.Board("rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 3")
    assert fen_hash(ep_live) != fen_hash(ep_gone)


def test_side_to_move_and_castling_rights_do_split_a_position():
    fen = "r3k2r/8/8/8/8/8/8/R3K2R {} {} - 0 1"
    assert fen_hash(chess.Board(fen.format("w", "KQkq"))) != fen_hash(
        chess.Board(fen.format("b", "KQkq"))
    )
    assert fen_hash(chess.Board(fen.format("w", "KQkq"))) != fen_hash(
        chess.Board(fen.format("w", "-"))
    )


def test_hash_is_32_raw_bytes_of_the_normalised_fen():
    board = chess.Board()
    assert len(fen_hash(board)) == 32
    assert normalize_fen(board) == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


@pytest.mark.parametrize(("cp", "mate"), [(None, None), (30, 2)])
def test_an_evaluation_is_a_score_or_a_mate_never_both_or_neither(cp, mate):
    with pytest.raises(ValueError):
        Evaluation(eval_cp=cp, mate_in=mate, best_move="e2e4")


def test_null_cache_always_misses():
    cache = NullCache()
    key = fen_hash(chess.Board())
    cache.put(key, "fen", "sf_18", 16, Evaluation(eval_cp=20, mate_in=None, best_move="e2e4"))
    assert cache.get(key, "sf_18", 16) is None
