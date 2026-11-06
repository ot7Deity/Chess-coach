"""The parser, against game JSON shaped like real archive responses."""

from __future__ import annotations

import datetime as dt

import pytest

from chess_coach.chesscom import GameParseError, parse_game
from tests.chesscom_fake import make_game

ME = "deitythemenace"


def test_color_matching_is_case_insensitive():
    """The API returns display case ('DeityTheMenace'); players are stored lowercase."""
    row = parse_game(make_game("g1", white="Rival", black="DeityTheMenace"), ME)
    assert row["color"] == "black"
    assert row["opponent_username"] == "Rival"


@pytest.mark.parametrize(
    ("white_result", "black_result", "as_white", "result", "termination"),
    [
        ("win", "resigned", True, "win", "resigned"),
        ("win", "timeout", False, "loss", "timeout"),
        ("checkmated", "win", True, "loss", "checkmated"),
        ("abandoned", "win", False, "win", "abandoned"),
        ("repetition", "repetition", True, "draw", "repetition"),
        ("timevsinsufficient", "timevsinsufficient", False, "draw", "timevsinsufficient"),
        # A loss code Chess.com has not invented yet is still a loss, because
        # the winner is always marked 'win'.
        ("win", "some-new-way-to-lose", False, "loss", "some-new-way-to-lose"),
    ],
)
def test_results_are_normalised_with_the_losers_reason_kept(
    white_result, black_result, as_white, result, termination
):
    white, black = (ME, "rival") if as_white else ("rival", ME)
    game = make_game(
        "g1", white=white, black=black, white_result=white_result, black_result=black_result
    )
    row = parse_game(game, ME)
    assert (row["result"], row["termination"]) == (result, termination)


def test_an_unrecognised_non_decisive_result_is_rejected_not_guessed():
    game = make_game("g1", white=ME, black="rival", white_result="??", black_result="??")
    with pytest.raises(GameParseError):
        parse_game(game, ME)


def test_eco_comes_from_the_pgn_header_not_the_json_url():
    """The JSON `eco` field is a URL. Storing it as `opening_eco` would break
    "results by opening", one of the four report features."""
    row = parse_game(make_game("g1", white=ME, black="rival", eco="B24"), ME)
    assert row["opening_eco"] == "B24"
    assert row["opening_name"] == "French Defense Kings Indian Attack"


def test_variants_are_kept_but_skipped_for_analysis():
    fen = "nrqnbbkr/pppppppp/8/8/8/8/PPPPPPPP/NRQNBBKR w HBhb - 0 1"
    game = make_game(
        "g960",
        white=ME,
        black="rival",
        rules="chess960",
        eco="A20",
        fen_header=fen,
        initial_setup="nrqnbbkr/pppppppp/8/8/8/8/PPPPPPPP/NRQNBBKR w KQkq - 0 1",
    )
    row = parse_game(game, ME)
    assert row["analysis_status"] == "skipped"
    assert row["analysis_skip_reason"] == "unsupported rules: chess960"
    # The PGN header's Shredder castling rights, not initial_setup's 'KQkq'.
    assert row["start_fen"] == fen
    # Chess.com tags 960 games with an ECO code; for a shuffled start it is noise.
    assert row["opening_eco"] is None
    assert row["opening_name"] is None


def test_normal_start_is_stored_as_null():
    row = parse_game(make_game("g1", white=ME, black="rival"), ME)
    assert row["start_fen"] is None
    assert row["analysis_status"] == "pending"


def test_played_at_is_end_time_in_utc_and_accuracies_are_per_side():
    end = dt.datetime(2026, 10, 31, 23, 59, tzinfo=dt.UTC)
    game = make_game(
        "g1",
        white="rival",
        black=ME,
        end_time=end,
        accuracies={"white": 71.5, "black": 88.2},
    )
    row = parse_game(game, ME)
    assert row["played_at"] == end
    assert (row["accuracy_player"], row["accuracy_opponent"]) == (88.2, 71.5)


def test_missing_accuracies_are_null():
    row = parse_game(make_game("g1", white=ME, black="rival"), ME)
    assert row["accuracy_player"] is None


def test_every_row_has_the_same_keys():
    """A multi-row INSERT needs identical keys in every row."""
    a = parse_game(make_game("g1", white=ME, black="rival"), ME)
    b = parse_game(make_game("g2", white="rival", black=ME, rules="chess960"), ME)
    assert a.keys() == b.keys()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g.pop("uuid"),
        lambda g: g.update(time_class="hyperbullet"),
        lambda g: g.update(pgn=""),
        lambda g: g["white"].update(username="someone-else"),
    ],
)
def test_malformed_games_raise(mutate):
    game = make_game("g1", white=ME, black="rival")
    mutate(game)
    with pytest.raises(GameParseError):
        parse_game(game, ME)
