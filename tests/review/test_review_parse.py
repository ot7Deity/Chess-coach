"""Try to break the game parser with inputs the existing tests do not use."""

from __future__ import annotations

import pytest

from chess_coach.chesscom import GameParseError, parse_game
from chess_coach.chesscom.parse import DRAW_CODES, _opening_name
from tests.chesscom_fake import make_game

ME = "deitythemenace"


@pytest.mark.parametrize("code", sorted(DRAW_CODES))
def test_every_draw_code_is_a_draw_from_either_side(code):
    for white, black in [(ME, "rival"), ("rival", ME)]:
        row = parse_game(
            make_game("g", white=white, black=black, white_result=code, black_result=code), ME
        )
        assert (row["result"], row["termination"]) == ("draw", code)


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (
            "https://www.chess.com/openings/Sicilian-Defense-Alapin-Variation-2...d5-3.exd5",
            "Sicilian Defense Alapin Variation",
        ),
        # Digits that are part of the name, not a move number, survive.
        ("https://www.chess.com/openings/Pirc-Defense-150-Attack", "Pirc Defense 150 Attack"),
        ("https://www.chess.com/openings/Queens-Pawn-Opening-1...d5", "Queens Pawn Opening"),
        ("https://www.chess.com/openings/Kings-Pawn-Opening/", "Kings Pawn Opening"),
        ("https://www.chess.com/openings/Caro-Kann-Defense?x=1", "Caro Kann Defense"),
        (None, None),
        ("", None),
    ],
)
def test_opening_names_from_url_slugs(url, name):
    assert _opening_name(url) == name


@pytest.mark.parametrize("eco", ["", "?", "A1", "F00", "a00", "A000"])
def test_a_junk_eco_header_is_stored_as_null(eco):
    row = parse_game(make_game("g", white=ME, black="rival", eco=eco), ME)
    assert row["opening_eco"] is None


def test_a_setup_header_with_the_normal_start_counts_as_the_normal_start():
    """Some PGNs carry SetUp/FEN for an ordinary game. That must not cost the
    game its opening code or send the worker down the custom-start path."""
    std = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    row = parse_game(make_game("g", white=ME, black="rival", fen_header=std), ME)
    assert row["start_fen"] is None
    assert row["opening_eco"] == "C00"


def test_a_custom_start_in_standard_chess_is_analysed_from_that_position():
    fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    row = parse_game(make_game("g", white=ME, black="rival", fen_header=fen), ME)
    assert row["start_fen"] == fen
    assert row["analysis_status"] == "pending"
    assert row["opening_eco"] is None  # an opening code means nothing from here


def test_unrated_games_are_kept_and_flagged():
    game = make_game("g", white=ME, black="rival")
    game["rated"] = False
    assert parse_game(game, ME)["rated"] is False


def test_a_missing_side_is_a_parse_error_not_a_crash():
    game = make_game("g", white=ME, black="rival")
    game["black"] = "rival"  # a string where an object belongs
    with pytest.raises(GameParseError):
        parse_game(game, ME)


def test_a_float_end_time_is_rejected():
    game = make_game("g", white=ME, black="rival")
    game["end_time"] = 1_790_000_000.5
    with pytest.raises(GameParseError):
        parse_game(game, ME)
