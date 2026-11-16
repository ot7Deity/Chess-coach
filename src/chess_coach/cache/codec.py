"""How an evaluation is stored in Redis.

A compact JSON array:

    [1, "sf_18", 16, 23, null, "e2e4", 1234567, 412]
     |   |       |   |   |     |       |        compute_ms
     |   |       |   |   |     |       nodes
     |   |       |   |   |     best_move
     |   |       |   |   mate_in
     |   |       |   eval_cp
     |   |       depth
     |   engine_version
     format version

Why this shape:

* **Self-describing about what it is an answer to.** The key already names the
  engine version and depth, but the value repeats them and `decode` refuses a
  value whose engine/depth differ from what was asked for. A key-building bug
  then surfaces as a miss (and a `corrupt_values` count) instead of a wrong
  evaluation served as if it were right - the one failure this project's cache
  design exists to rule out. Cost: about 12 bytes per entry.
* **Versioned.** A future format change cannot misread old entries as new ones;
  an unknown version is rejected and refilled from Postgres.
* **A string rather than a Redis hash.** Not for memory - measured with
  `MEMORY USAGE`, one entry is 184 bytes as a string and 200 as a small hash,
  because Redis packs small hashes compactly. For the commands: a string is
  read *and* has its TTL refreshed by one `GETEX`, and is replaced whole by one
  `SET`, so a reader can never see half of an old value and half of a new one.
  A hash needs `HGETALL` plus `EXPIRE`.
* **JSON rather than a packed struct or msgpack.** Those would save a few bytes
  but are unreadable in `redis-cli`, and msgpack is a new dependency.
* **Strict on read.** Anything that is not exactly this shape raises
  `CorruptValue`; the caller treats it as a miss. Redis is a cache, so a bad
  entry must never be an error that fails a job.
"""

from __future__ import annotations

import json

from chess_coach.positions import Evaluation

FORMAT_VERSION = 1


class CorruptValue(ValueError):
    """A Redis value that is unreadable or is not the evaluation asked for."""


def encode(engine_version: str, depth: int, evaluation: Evaluation) -> bytes:
    return json.dumps(
        [
            FORMAT_VERSION,
            engine_version,
            depth,
            evaluation.eval_cp,
            evaluation.mate_in,
            evaluation.best_move,
            evaluation.nodes,
            evaluation.compute_ms,
        ],
        separators=(",", ":"),
    ).encode("ascii")


def _is_int(value: object) -> bool:
    # bool is a subclass of int; `true` must not decode as eval_cp=1.
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_int(value: object, field: str) -> int | None:
    if value is None or _is_int(value):
        return value  # type: ignore[return-value]
    raise CorruptValue(f"{field} is not an integer")


def decode(raw: bytes | str, engine_version: str, depth: int) -> Evaluation:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptValue(f"not JSON: {exc}") from None

    if not isinstance(data, list) or len(data) != 8:
        raise CorruptValue("not an 8-element array")
    version, value_engine, value_depth, eval_cp, mate_in, best_move, nodes, compute_ms = data
    if not _is_int(version) or version != FORMAT_VERSION:
        raise CorruptValue(f"unknown format version {version!r}")
    if value_engine != engine_version or not _is_int(value_depth) or value_depth != depth:
        raise CorruptValue(
            f"value is for ({value_engine!r}, {value_depth!r}), "
            f"asked for ({engine_version!r}, {depth!r})"
        )
    # Exactly as strict as positions.best_move (VARCHAR(6)) and no stricter:
    # decode must accept everything encode can produce from a valid row, or
    # that row would be "corrupt" in Redis forever and always served from
    # Postgres.
    if best_move is not None and not (isinstance(best_move, str) and len(best_move) <= 6):
        raise CorruptValue("best_move is not a UCI move")

    try:
        return Evaluation(
            eval_cp=_optional_int(eval_cp, "eval_cp"),
            mate_in=_optional_int(mate_in, "mate_in"),
            best_move=best_move,
            nodes=_optional_int(nodes, "nodes"),
            compute_ms=_optional_int(compute_ms, "compute_ms"),
        )
    except ValueError as exc:  # Evaluation's own score-xor-mate check
        if isinstance(exc, CorruptValue):
            raise
        raise CorruptValue(str(exc)) from None
