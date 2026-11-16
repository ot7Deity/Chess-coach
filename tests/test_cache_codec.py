"""The Redis value format. Pure functions, no infrastructure."""

from __future__ import annotations

import json

import pytest

from chess_coach.cache.codec import CorruptValue, decode, encode
from chess_coach.positions import Evaluation


@pytest.mark.parametrize(
    "evaluation",
    [
        Evaluation(eval_cp=23, mate_in=None, best_move="e2e4", nodes=123456, compute_ms=412),
        Evaluation(eval_cp=None, mate_in=-3, best_move="e7e8q"),
        Evaluation(eval_cp=0, mate_in=None, best_move=None, nodes=0, compute_ms=0),
        Evaluation(eval_cp=0, mate_in=None, best_move="0000"),  # UCI null move
    ],
)
def test_round_trip(evaluation):
    assert decode(encode("sf_18", 16, evaluation), "sf_18", 16) == evaluation


def test_value_is_compact():
    raw = encode("sf_18", 16, Evaluation(23, None, "e2e4", 1234567, 412))
    assert raw == b'[1,"sf_18",16,23,null,"e2e4",1234567,412]'


@pytest.mark.parametrize(("engine", "depth"), [("sf_18", 12), ("sf_17", 16), ("sf_18 ", 16)])
def test_a_value_for_another_search_is_never_returned(engine, depth):
    """Defence in depth against a key-building bug: even if the wrong key were
    read, the value says which search produced it, and decode refuses it."""
    raw = encode("sf_18", 16, Evaluation(23, None, "e2e4"))
    with pytest.raises(CorruptValue, match="asked for"):
        decode(raw, engine, depth)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"{}",
        b"[]",
        b'[2,"sf_18",16,23,null,"e2e4",null,null]',  # unknown format version
        b'[true,"sf_18",16,23,null,"e2e4",null,null]',  # bool is not a version
        b'[1,"sf_18",true,23,null,"e2e4",null,null]',  # bool depth
        b'[1,"sf_18",16,true,null,"e2e4",null,null]',  # bool score
        b'[1,"sf_18",16,"23",null,"e2e4",null,null]',  # string score
        b'[1,"sf_18",16,23.5,null,"e2e4",null,null]',  # float score
        b'[1,"sf_18",16,23,2,"e2e4",null,null]',  # score and mate
        b'[1,"sf_18",16,null,null,"e2e4",null,null]',  # neither
        b'[1,"sf_18",16,23,null,"e2e4e5e6",null,null]',  # longer than the column
        b'[1,"sf_18",16,23,null,42,null,null]',
        b'[1,"sf_18",16,23,null,"e2e4",null]',  # too short
        b'[1,"sf_18",16,23,null,"e2e4",null,null,0]',  # too long
    ],
)
def test_anything_malformed_is_corrupt_not_a_crash(raw):
    with pytest.raises(CorruptValue):
        decode(raw, "sf_18", 16)


def test_corrupt_value_is_a_value_error():
    """Callers that only know ValueError still treat it as bad data."""
    assert issubclass(CorruptValue, ValueError)


def test_decoder_accepts_str_too():
    raw = encode("sf_18", 16, Evaluation(None, 1, "h7h8"))
    assert decode(raw.decode(), "sf_18", 16).mate_in == 1
    assert json.loads(raw)[0] == 1
