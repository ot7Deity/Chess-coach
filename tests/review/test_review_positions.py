"""Try to break the M2/M3 position contract (src/chess_coach/positions.py).

Two kinds of test here:

* Identity: positions that are the same must hash the same, positions that
  differ must not. These are pure python-chess.
* Evidence (engine-marked): the contract says a cached evaluation is a function
  of (position, engine version, depth) - "at a fixed engine version and depth
  both results are the same". These tests run the real Stockfish and show what
  else the number depends on, so M2 and M3 know what the worker has to pin for
  that sentence to be true.
"""

from __future__ import annotations

import io

import chess
import chess.engine
import chess.pgn
import pytest

from chess_coach.positions import Evaluation, fen_hash, normalize_fen

# --- identity -----------------------------------------------------------------


def test_transpositions_hash_the_same():
    a, b = chess.Board(), chess.Board()
    for san in ["Nf3", "Nf6", "Nc3", "Nc6"]:
        a.push_san(san)
    for san in ["Nc3", "Nc6", "Nf3", "Nf6"]:
        b.push_san(san)
    assert fen_hash(a) == fen_hash(b)


def test_a_pseudo_legal_but_illegal_en_passant_is_dropped():
    """The pawn on b5 could capture c6 en passant, except it would expose its
    own king on a5 to the rook on h5. 'legal', not 'pseudo-legal', is the rule."""
    pinned = chess.Board("8/8/8/KPp4r/8/8/8/7k w - c6 0 1")
    assert pinned.has_pseudo_legal_en_passant() and not pinned.has_legal_en_passant()
    assert fen_hash(pinned) == fen_hash(chess.Board("8/8/8/KPp4r/8/8/8/7k w - - 0 1"))


def test_castling_rights_that_cannot_exist_are_dropped():
    """A FEN claiming Q-side rights with no rook on a1 is the same position as
    one without them; python-chess cleans the rights before writing the EPD."""
    claimed = chess.Board("r3k3/8/8/8/8/8/8/4K2R w KQkq - 0 1")
    honest = chess.Board("r3k3/8/8/8/8/8/8/4K2R w Kq - 0 1")
    assert normalize_fen(claimed) == normalize_fen(honest) == "r3k3/8/8/8/8/8/8/4K2R w Kq -"


def test_castling_rights_that_do_exist_split_the_position():
    assert fen_hash(chess.Board("4k3/8/8/8/8/8/8/4K2R w K - 0 1")) != fen_hash(
        chess.Board("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    )


def test_a_promoted_piece_is_just_a_piece():
    """Crazyhouse marks promoted pieces with '~'; a standard board must not, or
    a promoted queen and an original one would be two positions."""
    board = chess.Board("8/P7/8/8/8/8/8/k6K w - - 0 1")
    board.push_san("a8=Q")
    assert "~" not in normalize_fen(board)
    assert fen_hash(board) == fen_hash(chess.Board("Q7/8/8/8/8/8/8/k6K b - - 0 1"))


def test_move_history_is_not_part_of_the_key():
    """Same placement reached with and without a move stack: same key."""
    played = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6"]:
        played.push_san(san)
    assert fen_hash(played) == fen_hash(chess.Board(played.fen()))


def test_a_chess960_board_and_a_standard_board_agree_on_a_standard_position():
    """If M2 ever builds boards with chess960=True (start_fen games), a
    standard-looking position must not get a second key."""
    std = chess.Board()
    c960 = chess.Board(chess.STARTING_FEN, chess960=True)
    assert normalize_fen(std) == normalize_fen(c960)


def test_a_chess960_position_keeps_shredder_castling_distinct_from_none():
    """Skipped today (rules != chess), but if 960 is ever analysed the rights
    must survive normalisation."""
    fen = "nrqnbbkr/pppppppp/8/8/8/8/PPPPPPPP/NRQNBBKR w HBhb - 0 1"
    with_rights = chess.Board(fen, chess960=True)
    without = chess.Board(fen.replace("HBhb", "-"), chess960=True)
    assert fen_hash(with_rights) != fen_hash(without)


def test_mate_zero_is_a_legal_evaluation():
    """Stockfish reports a checkmated side to move as 'mate 0'. The contract
    must accept it (best_move None) - and see the engine tests below for why 0
    is a dangerous value to negate."""
    Evaluation(eval_cp=None, mate_in=0, best_move=None)


# --- evidence: what else the engine's number depends on ----------------------

# A 34-ply Najdorf, English Attack.
_GAME = (
    "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 6. Be3 e5 7. Nb3 Be6 "
    "8. f3 Be7 9. Qd2 O-O 10. O-O-O Nbd7 11. g4 b5 12. g5 b4 13. Ne2 Ne8 "
    "14. f4 a5 15. f5 a4 16. Nbd4 exd4 17. Nxd4 b3 *"
)
_DEPTH = 12


@pytest.fixture(scope="module")
def engine(stockfish_path):
    eng = chess.engine.SimpleEngine.popen_uci(str(stockfish_path))
    eng.configure({"Threads": 1, "Hash": 16})
    yield eng
    eng.quit()


def _cp(info: chess.engine.InfoDict) -> int:
    return info["score"].relative.score(mate_score=100_000)


@pytest.mark.engine
def test_eval_depends_on_the_halfmove_clock_that_the_key_drops(engine):
    """positions.py:432-434 says the halfmove clock does not matter 'to the
    engine at a fixed depth'. It does: Stockfish damps its eval as the 50-move
    counter rises. Same normalised FEN, same depth, one cache key - and the
    value ranges from 'winning' to 'dead draw'. Whichever game writes first
    decides what every other game is told."""
    evals = {}
    for clock in (0, 90):
        board = chess.Board(f"8/5k2/8/8/3R4/8/5K2/8 w - - {clock} 80")
        evals[clock] = _cp(engine.analyse(board, chess.engine.Limit(depth=_DEPTH), game=object()))
    key0 = fen_hash(chess.Board("8/5k2/8/8/3R4/8/5K2/8 w - - 0 80"))
    key90 = fen_hash(chess.Board("8/5k2/8/8/3R4/8/5K2/8 w - - 90 80"))
    assert key0 == key90
    assert evals[0] > 300
    assert abs(evals[90]) < 100


@pytest.mark.engine
def test_fixed_depth_is_not_reproducible_with_a_warm_hash(engine):
    """positions.py:472-474: 'the first write wins ... because at a fixed engine
    version and depth both results are the same'. Only with a cleared hash
    table. Analysing a game's positions in order (hash kept between them, which
    is what a worker naturally does) gives different scores and often different
    best moves than analysing each position cold."""
    game = chess.pgn.read_game(io.StringIO(_GAME))
    boards, board = [], game.board()
    for move in game.mainline_moves():
        board.push(move)
        boards.append(board.copy())

    limit = chess.engine.Limit(depth=_DEPTH)
    warm_game = object()  # one 'game' -> no ucinewgame between positions
    warm = [_cp(engine.analyse(b, limit, game=warm_game)) for b in boards]
    cold = [_cp(engine.analyse(b, limit, game=object())) for b in boards]
    cold_again = [_cp(engine.analyse(b, limit, game=object())) for b in boards]

    # Cold, single-threaded search is reproducible ...
    assert cold == cold_again
    # ... warm search is not the same function of the key.
    differing = [abs(w - c) for w, c in zip(warm, cold, strict=True) if w != c]
    assert len(differing) > len(boards) // 2
    assert max(differing) >= 20


@pytest.mark.engine
def test_a_checkmated_position_scores_mate_zero_and_negation_loses_the_sign(engine):
    """After a mating move, the side to move is mated: Stockfish says 'mate 0'.
    The contract stores side-to-move POV, so `mate_in=0`; the worker converts
    to mover POV by negating, and -0 == 0. A worker that reads 'mate_in > 0 means
    I am mating' will treat the mating move as not winning - a likely source of
    'checkmate classified as a blunder'. 0 needs an explicit rule in the
    contract."""
    board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert board.is_checkmate()
    info = engine.analyse(board, chess.engine.Limit(depth=5))
    relative = info["score"].relative
    assert relative.mate() == 0
    assert (-relative).mate() == 0  # the sign does not survive as an int
    assert relative < chess.engine.Cp(0) < -relative  # python-chess keeps it


# --- m1/6: engine_board -------------------------------------------------------


@pytest.mark.parametrize(
    "fen",
    [
        "rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 3",  # live en passant
        "8/8/8/KPp4r/8/8/8/7k w - c6 0 1",  # pinned, so no en passant
        "r3k3/8/8/8/8/8/8/4K2R w KQkq - 37 60",  # impossible rights, high clock
        "8/5k2/8/8/3R4/8/5K2/8 w - - 90 80",
    ],
)
def test_engine_board_describes_exactly_the_key(fen):
    """The board the engine sees must hash to the same key and carry nothing
    else: no move stack, halfmove clock 0."""
    from chess_coach.positions import engine_board

    played = chess.Board(fen)
    legal = list(played.legal_moves)
    played.push(legal[0])  # give it a move stack
    played.pop()
    played.push(legal[0])
    eb = engine_board(played)
    assert fen_hash(eb) == fen_hash(played)
    assert eb.halfmove_clock == 0
    assert eb.move_stack == []


@pytest.mark.engine
def test_a_cold_eval_still_depends_on_the_hash_size_which_the_key_omits(stockfish_path):
    """m1/6 removes the clock and the warm hash, but the hash table's *size* is
    a third input: cleared hash, one thread, same board, same depth, and Hash=1
    vs Hash=128 MB score most of this game differently. (At the production
    depth of 16, Hash=16 vs 128 differed on 14 of 34 positions.) ENGINE_HASH_MB
    is a setting, so a test run and production - or two workers configured
    differently - write different values under one key. Threads > 1 is worse:
    not reproducible from one run to the next."""
    from chess_coach.positions import engine_board

    game = chess.pgn.read_game(io.StringIO(_GAME))
    boards, board = [], game.board()
    for move in game.mainline_moves():
        board.push(move)
        boards.append(engine_board(board))

    def scores(hash_mb: int) -> list[int]:
        eng = chess.engine.SimpleEngine.popen_uci(str(stockfish_path))
        try:
            eng.configure({"Threads": 1, "Hash": hash_mb})
            limit = chess.engine.Limit(depth=_DEPTH)
            return [_cp(eng.analyse(b, limit, game=object())) for b in boards]
        finally:
            eng.quit()

    small, big = scores(1), scores(128)
    assert scores(128) == big  # each configuration is reproducible ...
    differing = sum(s != b for s, b in zip(small, big, strict=True))
    assert differing > len(boards) // 2  # ... but they disagree with each other
