"""Checks on the pinned engine and the lock file that pins it.

The lock-file tests need no network and run everywhere. The engine tests are
marked `engine` and skip when no binary is present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

LOCK = json.loads(
    (Path(__file__).resolve().parent.parent / "tools" / "stockfish.lock.json").read_text(
        encoding="utf-8"
    )
)


def test_every_preferred_asset_has_a_pinned_checksum():
    """A preference entry with no checksum would be downloaded and executed
    unverified, which is the whole thing the lock file exists to prevent."""
    for platform_name, assets in LOCK["preference"].items():
        for asset in assets:
            assert asset in LOCK["assets"], f"{asset} ({platform_name}) has no pinned checksum"
            assert len(LOCK["assets"][asset]["sha256"]) == 64
            assert LOCK["assets"][asset]["size"] > 0


def test_both_platforms_are_pinned_to_the_same_release():
    """Dev runs on Windows and the workers run in Linux containers. A cached
    evaluation is only valid for the engine version that produced it, so the two
    platforms must resolve to one release tag."""
    assert LOCK["tag"] == "sf_18"
    assert set(LOCK["preference"]) == {"linux", "windows"}


def test_fallback_chain_is_ordered_fastest_first():
    """AVX-VNNI first for modern Intel, then AVX2, then the baseline build that
    runs anywhere. Out of order, slow hardware would be fine but the fast path
    would never be taken."""
    for assets in LOCK["preference"].values():
        order = ["avxvnni", "avx2", "sse41-popcnt"]
        found = [next(o for o in order if o in a) for a in assets]
        assert found == order


@pytest.mark.engine
def test_engine_starts_and_reports_pinned_version(stockfish_path):
    import chess.engine

    with chess.engine.SimpleEngine.popen_uci(str(stockfish_path)) as engine:
        assert "Stockfish" in engine.id["name"]
        assert "18" in engine.id["name"], f"expected the pinned sf_18, got {engine.id['name']!r}"


@pytest.mark.engine
def test_engine_finds_a_mate_in_one(stockfish_path):
    """Sanity check that we are driving the engine correctly, not just starting
    it. Back-rank mate: Ra8#."""
    import chess
    import chess.engine

    board = chess.Board("6k1/5ppp/8/8/8/8/8/R6K w - - 0 1")
    with chess.engine.SimpleEngine.popen_uci(str(stockfish_path)) as engine:
        info = engine.analyse(board, chess.engine.Limit(depth=12))

    score = info["score"].white()
    assert score.is_mate(), f"expected a mate score, got {score}"
    assert score.mate() == 1, f"expected mate in 1, got mate in {score.mate()}"


@pytest.mark.engine
def test_fixed_depth_is_reproducible(stockfish_path):
    """The position cache is only sound if the same position at the same depth
    gives the same evaluation every time. This is why the project uses fixed
    depth rather than a movetime budget."""
    import chess
    import chess.engine

    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 1")
    evals = []
    for _ in range(2):
        with chess.engine.SimpleEngine.popen_uci(str(stockfish_path)) as engine:
            engine.configure({"Threads": 1, "Hash": 64})
            info = engine.analyse(board, chess.engine.Limit(depth=14))
            evals.append(info["score"].white().score())

    assert evals[0] == evals[1], f"same position and depth gave {evals[0]} then {evals[1]}"
