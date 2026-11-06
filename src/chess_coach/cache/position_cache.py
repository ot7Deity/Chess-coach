"""The position cache: Redis in front of the Postgres `positions` table.

Implements `chess_coach.positions.EvalCache`. The rules it keeps:

* **Postgres is the source of truth; Redis is a rebuildable copy of it.** Every
  value ever written to Redis is a value Postgres has already committed, so the
  two can only disagree by Redis *lacking* an entry (expired, evicted, never
  copied) - never by holding a different one. A missing entry costs one
  Postgres read, after which it is backfilled.

* **The first write wins, and only the winner writes Redis.** `INSERT ... ON
  CONFLICT DO NOTHING RETURNING` tells the writer whether it won. Two workers
  that evaluate the same position concurrently can produce slightly different
  numbers: at a fixed depth Stockfish is reproducible only from a cleared hash
  table, and a worker's is always warm from the previous positions (measured:
  10/10 scores moved by 2-27 cp at depth 14). If both wrote Redis, Redis could
  end up holding the loser's value while Postgres holds the winner's; writing
  Redis before Postgres would have the same flaw. The loser's write is a no-op
  everywhere.

* **The key is (fen_hash, engine_version, depth), in both layers.** The Redis
  key embeds all three, and the Redis value repeats engine version and depth so
  that a value for the wrong search can never be returned (see `codec`).

* **A corrupt Redis value is a miss.** It is counted, logged, and overwritten
  by the Postgres backfill (or by the next put, if Postgres lacks it too).

* **Expiry is sliding.** Every hit refreshes the TTL (`GETEX`, in the same round
  trip), so positions that recur - openings - stay resident while the one-off
  middlegame positions that make up most of a game age out. Redis memory tracks
  the working set, not the whole history.

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
import redis
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chess_coach.cache.codec import CorruptValue, decode, encode
from chess_coach.logging import get_logger
from chess_coach.models import Position
from chess_coach.positions import Evaluation, fen_hash, normalize_fen

log = get_logger("chess_coach.cache")

DEFAULT_KEY_PREFIX = "chess_coach:pos:v1:"
DEFAULT_TTL_SEC = 7 * 24 * 3600

_MAX_ENGINE_VERSION_LEN = 32  # positions.engine_version is VARCHAR(32)
_MAX_DEPTH = 32767  # positions.depth is SMALLINT
_PG_CHUNK = 1000  # keys per `fen_hash IN (...)` query


def redis_key(prefix: str, key: bytes, engine_version: str, depth: int) -> str:
    """`<prefix><engine_version>:<depth>:<sha256 hex>`.

    Unambiguous even if an engine version contains ':' - the hash is fixed
    length and the depth is digits only, so the key parses from the right.
    Hex rather than raw bytes so keys are readable in `redis-cli`.
    """
    return f"{prefix}{engine_version}:{depth}:{key.hex()}"


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
    redis_errors: int = 0
    corrupt_values: int = 0

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
    """`EvalCache` backed by Postgres, with Redis as an optional hot layer.

    `redis_client=None` gives a Postgres-only cache, which is also a benchmark
    configuration ("what does Redis add?"). Safe to share between threads; each
    call checks out its own connection.
    """

    def __init__(
        self,
        engine: Engine,
        redis_client: redis.Redis | None = None,
        *,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        ttl_sec: int = DEFAULT_TTL_SEC,
    ) -> None:
        if ttl_sec < 0:
            raise ValueError("ttl_sec must be >= 0 (0 disables expiry)")
        self._engine = engine
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_sec or None
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
        if inserted:
            self._redis_set({key: evaluation}, engine_version, depth)

    # --- batch ----------------------------------------------------------------

    def get_many(
        self, keys: Iterable[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        """Look up a whole game's positions at once.

        The worker replays the PGN before evaluating anything, so it knows every
        position up front. At most three round trips however many keys: one
        pipelined Redis read, one Postgres query for what Redis lacked, one
        pipelined Redis backfill. Returns only the hits; a key absent from the
        result is a miss.
        """
        _check_search(engine_version, depth)
        unique = list(dict.fromkeys(keys))
        for key in unique:
            _check_key(key)
        if not unique:
            return {}

        found = self._redis_get(unique, engine_version, depth) if self._redis_usable() else {}
        from_redis = list(found.values())

        missing = [k for k in unique if k not in found]
        from_postgres = self._postgres_get(missing, engine_version, depth) if missing else {}
        if from_postgres:
            self._redis_set(from_postgres, engine_version, depth)
            found.update(from_postgres)

        self._record(from_redis, list(from_postgres.values()), len(missing) - len(from_postgres))
        return found

    # --- maintenance ----------------------------------------------------------

    def purge_redis(self, engine_version: str | None = None, depth: int | None = None) -> int:
        """Delete Redis entries under this cache's prefix; Postgres is untouched.

        The one case where Redis could outlive Postgres is someone deleting
        `positions` rows by hand (say, throwing away a bad engine build's
        evaluations). Do this alongside such a delete. Returns keys deleted.
        """
        if self._redis is None:
            return 0
        if depth is not None and engine_version is None:
            raise ValueError("purging by depth needs an engine_version too")

        match = _glob_escape(self._prefix) + "*"
        deleted = 0
        batch: list[str | bytes] = []
        for raw in self._redis.scan_iter(match=match, count=1000):
            name = raw.decode() if isinstance(raw, bytes) else raw
            if engine_version is not None:
                parts = name[len(self._prefix) :].rsplit(":", 2)
                if len(parts) != 3 or parts[0] != engine_version:
                    continue
                if depth is not None and parts[1] != str(depth):
                    continue
            batch.append(raw)
            if len(batch) >= 500:
                deleted += self._redis.delete(*batch)
                batch.clear()
        if batch:
            deleted += self._redis.delete(*batch)
        return deleted

    @property
    def stats(self) -> CacheStats:
        """A snapshot of this instance's counters."""
        with self._lock:
            return CacheStats(**asdict(self._stats))

    @property
    def redis_active(self) -> bool:
        return self._redis_usable()

    # --- layers -----------------------------------------------------------------

    def _rkey(self, key: bytes, engine_version: str, depth: int) -> str:
        return redis_key(self._prefix, key, engine_version, depth)

    def _redis_usable(self) -> bool:
        return self._redis is not None

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

    def _redis_get(
        self, keys: Sequence[bytes], engine_version: str, depth: int
    ) -> dict[bytes, Evaluation]:
        assert self._redis is not None
        names = [self._rkey(k, engine_version, depth) for k in keys]
        pipe = self._redis.pipeline(transaction=False)
        for name in names:
            if self._ttl:
                pipe.getex(name, ex=self._ttl)
            else:
                pipe.get(name)
        raws = pipe.execute(raise_on_error=False)

        out: dict[bytes, Evaluation] = {}
        bad: list[str] = []
        for key, name, raw in zip(keys, names, raws, strict=True):
            if raw is None:
                continue
            if isinstance(raw, Exception):  # a per-key error such as WRONGTYPE
                bad.append(f"{name}: {raw}")
                continue
            try:
                out[key] = decode(raw, engine_version, depth)
            except CorruptValue as exc:
                bad.append(f"{name}: {exc}")
        if bad:
            with self._lock:
                self._stats.corrupt_values += len(bad)
            log.warning("cache.corrupt_values", count=len(bad), first=bad[0])
        return out

    def _redis_set(self, items: dict[bytes, Evaluation], engine_version: str, depth: int) -> None:
        if not items or not self._redis_usable():
            return
        assert self._redis is not None
        pipe = self._redis.pipeline(transaction=False)
        for key, evaluation in items.items():
            pipe.set(
                self._rkey(key, engine_version, depth),
                encode(engine_version, depth, evaluation),
                ex=self._ttl,
            )
        results = pipe.execute(raise_on_error=False)

        # Per-command errors (e.g. OOM under `maxmemory` + `noeviction`) mean
        # Redis is up but refusing writes. Postgres already has the value, so
        # this is counted, not raised.
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            with self._lock:
                self._stats.redis_errors += len(errors)
            log.warning("cache.redis_write_rejected", count=len(errors), error=str(errors[0]))

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


def _glob_escape(text: str) -> str:
    """Escape Redis glob metacharacters so a prefix matches only itself."""
    return "".join("\\" + c if c in "\\*?[]" else c for c in text)
