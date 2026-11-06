"""The position-identity contract shared by the analysis worker and the cache."""

from __future__ import annotations

import chess
import pytest

from chess_coach.positions import Evaluation, NullCache, engine_board, fen_hash, normalize_fen


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


def test_engine_board_drops_history_and_the_halfmove_clock():
    """Two routes to one position must give the engine identical input."""
    a = chess.Board()
    for san in ["Nf3", "Nf6", "Ng1", "Ng8", "e4"]:  # a detour: clock and history differ
        a.push_san(san)
    b = chess.Board()
    b.push_san("e4")

    assert fen_hash(a) == fen_hash(b)
    assert engine_board(a).fen() == engine_board(b).fen()
    assert engine_board(a).move_stack == []
    assert engine_board(a).halfmove_clock == 0


def test_engine_board_keeps_chess960_castling():
    board = chess.Board("nrqnbbkr/pppppppp/8/8/8/8/PPPPPPPP/NRQNBBKR w HBhb - 0 1", chess960=True)
    assert engine_board(board).chess960
    assert engine_board(board).castling_rights == board.castling_rights


@pytest.mark.engine
def test_an_evaluation_is_a_function_of_its_cache_key(stockfish_path):
    """The reviewer's counter-example, turned into the guarantee.

    The same KRK position with the halfmove clock at 0 and at 90 scores very
    differently when sent as-is (Stockfish scales towards the 50-move draw).
    Through engine_board(), with a cleared hash and one thread, both routes
    must produce the identical evaluation - which is what makes first-write-
    wins in the cache correct.
    """
    import chess.engine

    fresh = chess.Board("8/8/8/4k3/8/8/8/4K2R w K - 0 1")
    stale = chess.Board("8/8/8/4k3/8/8/8/4K2R w K - 90 120")
    assert fen_hash(fresh) == fen_hash(stale)

    with chess.engine.SimpleEngine.popen_uci(str(stockfish_path)) as engine:
        engine.configure({"Threads": 1, "Hash": 16})
        limit = chess.engine.Limit(depth=12)
        raw = [
            engine.analyse(b, limit, game=object())["score"].pov(chess.WHITE)
            for b in (fresh, stale)
        ]
        normalised = [
            engine.analyse(engine_board(b), limit, game=object())["score"].pov(chess.WHITE)
            for b in (fresh, stale)
        ]

    assert raw[0] != raw[1], "precondition: the clock really does change the raw score"
    assert normalised[0] == normalised[1]
