"""The position cache, backed by the Postgres `positions` table.

Implements `chess_coach.positions.EvalCache`. The rules it keeps:

* **Postgres is the source of truth.** Any faster layer added in front of it
  holds only values Postgres has already committed.

* **The first write wins.** `INSERT ... ON CONFLICT DO NOTHING RETURNING` tells
  the writer whether it won. Two workers that evaluate the same position
  concurrently can produce slightly different numbers: at a fixed depth
  Stockfish is reproducible only from a cleared hash table, and a worker's is
  always warm from the previous positions (measured: 10/10 scores moved by
  2-27 cp at depth 14). The second write is a no-op, so every reader sees the
  first.

* **The key is (fen_hash, engine_version, depth).** Never the position alone:
  a depth-12 result must never answer a depth-16 question.

* **Writes commit on their own.** An evaluation is a pure function of
  (position, engine, depth), so it stays valid if the job that computed it
  fails later, and other workers should see it at once - not when the job ends.

Lookups are counted once per *distinct* position asked for: a position repeated
inside one batch (a threefold repetition) is one lookup.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

import chess
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chess_coach.models import Position
from chess_coach.positions import Evaluation, fen_hash, normalize_fen

_MAX_ENGINE_VERSION_LEN = 32  # positions.engine_version is VARCHAR(32)
_MAX_DEPTH = 32767  # positions.depth is SMALLINT
_PG_CHUNK = 1000  # keys per `fen_hash IN (...)` query


def _check_search(engine_version: str, depth: int) -> None:
    if not isinstance(engine_version, str) or not (
        0 < len(engine_version) <= _MAX_ENGINE_VERSION_LEN
    ):
        raise ValueError(f"engine_version must be 1-{_MAX_ENGINE_VERSION_LEN} characters")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= _MAX_DEPTH:
        raise ValueError(f"depth must be an integer from 1 to {_MAX_DEPTH}")


def _check_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("a position key is fen_hash(board): 32 raw bytes")


def _check_fen_matches_key(key: bytes, fen: str) -> None:
    """The stored FEN must be the normalised FEN the key was hashed from.

    Catches a caller passing `board.fen()` (with move counters) or a FEN from
    the wrong ply, which would otherwise store a debugging FEN that does not
    describe the row's position.
    """
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise ValueError(f"not a FEN: {fen!r}") from exc
    if normalize_fen(board) != fen or fen_hash(board) != key:
        raise ValueError("fen must be normalize_fen(board) for the board whose fen_hash is key")


@dataclass
class CacheStats:
    """Per-process counters, for tests, logs and the benchmark."""

    redis_hits: int = 0
    postgres_hits: int = 0
    misses: int = 0
    # Sum of compute_ms over hits: the Stockfish time those hits replaced.
    engine_ms_saved: int = 0
    # Hits whose row has no compute_ms, so engine_ms_saved undercounts them.
    hits_without_compute_ms: int = 0
    writes_inserted: int = 0
    writes_existing: int = 0

    @property
    def hits(self) -> int:
        return self.redis_hits + self.postgres_hits

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.lookups if self.lookups else None

    def as_dict(self) -> dict[str, float | int | None]:
        return {**asdict(self), "lookups": self.lookups, "hit_rate": self.hit_rate}


class PositionCache:
    """`EvalCache` backed by Postgres.

    Safe to share between threads; each call checks out its own connection.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._stats = CacheStats()

    # --- EvalCache ------------------------------------------------------------

    def get(self, key: bytes, engine_version: str, depth: int) -> Evaluation | None:
        return self.get_many((key,), engine_version, depth).get(key)

    def put(
        self, key: bytes, fen: str, engine_version: str, depth: int, evaluation: Evaluation
    ) -> None:
        _check_search(engine_version, depth)
        _check_key(key)
        _check_fen_matches_key(key, fen)

        stmt = (
            pg_insert(Position)
            .values(
                fen_hash=key,
                engine_version=engine_version,
                depth=depth,
                fen=fen,
                best_move=evaluation.best_move,
                eval_cp=evaluation.eval_cp,
                mate_in=evaluation.mate_in,
                nodes=evaluation.nodes,
                compute_ms=evaluation.compute_ms,
            )
            .on_conflict_do_nothing(index_elements=["fen_hash", "engine_version", "depth"])
            .returning(Position.fen_hash)
        )
        with self._engine.begin() as conn:
            inserted = conn.execute(stmt).first() is not None

        with self._lock:
            if inserted:
                self._stats.writes_inserted += 1
            else:
                self._stats.writes_existing += 1

    # --- batch ----------------------------------------------------------------

    def get_many(
        self, keys: Iterable[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        """Look up a whole game's positions in one query.

        The worker replays the PGN before evaluating anything, so it knows every
        position up front; one round trip instead of one per ply. Returns only
        the hits; a key absent from the result is a miss.
        """
        _check_search(engine_version, depth)
        unique = list(dict.fromkeys(keys))
        for key in unique:
            _check_key(key)
        if not unique:
            return {}

        found = self._postgres_get(unique, engine_version, depth)
        self._record([], list(found.values()), len(unique) - len(found))
        return found

    @property
    def stats(self) -> CacheStats:
        """A snapshot of this instance's counters."""
        with self._lock:
            return CacheStats(**asdict(self._stats))

    # --- layers -----------------------------------------------------------------

    def _postgres_get(
        self, keys: Sequence[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        out: dict[bytes, Evaluation] = {}
        with self._engine.connect() as conn:
            for start in range(0, len(keys), _PG_CHUNK):
                rows = conn.execute(
                    select(
                        Position.fen_hash,
                        Position.eval_cp,
                        Position.mate_in,
                        Position.best_move,
                        Position.nodes,
                        Position.compute_ms,
                    ).where(
                        Position.engine_version == engine_version,
                        Position.depth == depth,
                        Position.fen_hash.in_(keys[start : start + _PG_CHUNK]),
                    )
                )
                for row in rows:
                    out[bytes(row.fen_hash)] = Evaluation(
                        eval_cp=row.eval_cp,
                        mate_in=row.mate_in,
                        best_move=row.best_move,
                        nodes=row.nodes,
                        compute_ms=row.compute_ms,
                    )
        return out

    # --- accounting ---------------------------------------------------------------

    def _record(
        self, from_redis: list[Evaluation], from_postgres: list[Evaluation], misses: int
    ) -> None:
        saved = 0
        unknown = 0
        for evaluation in from_redis + from_postgres:
            if evaluation.compute_ms is None:
                unknown += 1
            else:
                saved += evaluation.compute_ms
        with self._lock:
            self._stats.redis_hits += len(from_redis)
            self._stats.postgres_hits += len(from_postgres)
            self._stats.misses += misses
            self._stats.engine_ms_saved += saved
            self._stats.hits_without_compute_ms += unknown
