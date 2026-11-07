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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import chess


def normalize_fen(board: chess.Board) -> str:
    """The FEN fields that decide an evaluation, and nothing else.

    Dropped: the halfmove clock and fullmove number. Keeping the counters would
    split one cache entry into dozens. The halfmove clock DOES change
    Stockfish's score (it scales evaluations towards a draw as the 50-move rule
    approaches), which is why the engine must be given `engine_board(board)`,
    with the clock reset, rather than the game's own board - see below.

    Kept only when it matters: the en-passant square, and only if an en-passant
    capture is actually legal. A FEN records the square after every double pawn
    push, so without this rule "1. e4" and a transposition to the same position
    without a double push would hash differently.
    """
    return board.epd(en_passant="legal")


def fen_hash(board: chess.Board) -> bytes:
    return hashlib.sha256(normalize_fen(board).encode("ascii")).digest()


def engine_board(board: chess.Board) -> chess.Board:
    """The board to send to the engine for `fen_hash(board)`.

    A cached evaluation must be a function of its key alone, and the key is
    (fen_hash, engine_version, depth). Four things outside the board change a
    Stockfish score at a fixed depth. Each is either removed or made part of
    the key:

    * The halfmove clock - removed here. One KRK position scored +529, +359,
      +11 and 0 at depth 12 with the clock at 0, 50, 90 and 98. This board has
      the clock at 0 and no move stack, so the engine sees exactly what the key
      describes.
    * The engine's hash table, left over from earlier positions - removed by
      the caller: in one 34-ply game, 31 positions scored differently warm than
      cold. Clear it before every position (UCI `ucinewgame`; in python-chess,
      a fresh `game=` object per `analyse` call).
    * The number of search threads - removed by the caller: Threads=2 is not
      reproducible run to run. Use Threads=1.
    * The size of the hash table - made part of the key: even cleared, 16 MB
      and 128 MB disagreed on 14 of 34 positions at depth 16 (up to 9 cp, one
      different best move). The worker therefore includes it in
      `engine_version`, e.g. 'sf_18-h128'.

    Found by the adversarial review, with real-engine evidence; see
    INTERVIEW_PREP.md.
    """
    return chess.Board(normalize_fen(board) + " 0 1", chess960=board.chess960)


@dataclass(frozen=True)
class Evaluation:
    """One engine's verdict on one position at one depth.

    Exactly one of `eval_cp` / `mate_in` is set, mirroring the CHECK constraint
    on `positions`. `mate_in` is signed: positive means the side to move mates,
    negative means it is being mated, and 0 means the side to move is already
    checkmated (best_move is then None). Because -0 == 0, a 0 cannot be
    flipped to the other side's point of view by negation; convert it
    explicitly (after a mating move, the mover has delivered mate).
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
    first write wins and the second is a no-op. That is only correct because
    the worker evaluates `engine_board()` with a cleared hash and one thread,
    which makes the result a pure function of the key; a put that disagrees
    with the stored value means that rule was broken and should be reported,
    not silently resolved.
    """

    def get(self, key: bytes, engine_version: str, depth: int) -> Evaluation | None: ...

    def get_many(
        self, keys: Sequence[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        """Hits only, keyed by fen_hash; a missing key is a miss. Lets the worker
        look up a whole game's positions in one round trip after replaying it."""
        ...

    def put(
        self, key: bytes, fen: str, engine_version: str, depth: int, evaluation: Evaluation
    ) -> None: ...


class NullCache:
    """Always misses. The M2 worker's default until M3's cache is wired in, and
    the 'without cache' side of the engine-seconds-saved benchmark."""

    def get(self, key: bytes, engine_version: str, depth: int) -> Evaluation | None:
        return None

    def get_many(
        self, keys: Sequence[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        return {}

    def put(
        self, key: bytes, fen: str, engine_version: str, depth: int, evaluation: Evaluation
    ) -> None:
        return None
