"""The contract between the analysis worker (M2) and the position cache (M3).

Both milestones key on the same thing - a position - so the definition of
"the same position" lives here, once. If the worker hashed positions one way and
the cache another, every lookup would miss and nothing would fail: the cache
would just never hit, which looks like "caching doesn't help much".

The rules:

* A position is identified by `fen_hash(board)`: SHA-256 of `normalize_fen`,
  32 raw bytes. `moves.fen_hash` and `positions.fen_hash` both hold this.
* An evaluation is identified by (fen_hash, engine_version, depth). See the
  `Position` model for why engine version and depth are part of the key.
* `Evaluation` scores are from the point of view of **the side to move** in the
  evaluated position, as UCI reports them. Converting to the mover's point of
  view for `moves.eval_cp_*` is the analysis worker's job, not the cache's.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import chess


def normalize_fen(board: chess.Board) -> str:
    """The FEN fields that decide an evaluation, and nothing else.

    Dropped: the halfmove clock and fullmove number. The same position reached
    on move 12 or move 40 is the same position to the engine at a fixed depth,
    and keeping the counters would split one cache entry into dozens.

    Kept only when it matters: the en-passant square, and only if an en-passant
    capture is actually legal. A FEN records the square after every double pawn
    push, so without this rule "1. e4" and a transposition to the same position
    without a double push would hash differently.
    """
    return board.epd(en_passant="legal")


def fen_hash(board: chess.Board) -> bytes:
    return hashlib.sha256(normalize_fen(board).encode("ascii")).digest()


@dataclass(frozen=True)
class Evaluation:
    """One engine's verdict on one position at one depth.

    Exactly one of `eval_cp` / `mate_in` is set, mirroring the CHECK constraint
    on `positions`. `mate_in` is signed: positive means the side to move mates,
    negative means it is being mated.
    """

    eval_cp: int | None
    mate_in: int | None
    best_move: str | None  # UCI, e.g. 'e2e4'; None for a position with no legal moves
    nodes: int | None = None
    compute_ms: int | None = None

    def __post_init__(self) -> None:
        if (self.eval_cp is None) == (self.mate_in is None):
            raise ValueError("exactly one of eval_cp and mate_in must be set")


class EvalCache(Protocol):
    """What the analysis worker needs from a cache. M3 provides the real one.

    `get` returns None on a miss. `put` must be safe to call twice with the
    same key (two workers can evaluate the same position concurrently); the
    first write wins and the second is a no-op, because at a fixed engine
    version and depth both results are the same.
    """

    def get(self, key: bytes, engine_version: str, depth: int) -> Evaluation | None: ...

    def put(
        self, key: bytes, fen: str, engine_version: str, depth: int, evaluation: Evaluation
    ) -> None: ...


class NullCache:
    """Always misses. The M2 worker's default until M3's cache is wired in, and
    the 'without cache' side of the engine-seconds-saved benchmark."""

    def get(self, key: bytes, engine_version: str, depth: int) -> Evaluation | None:
        return None

    def put(
        self, key: bytes, fen: str, engine_version: str, depth: int, evaluation: Evaluation
    ) -> None:
        return None
