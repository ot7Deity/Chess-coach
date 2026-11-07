"""One Stockfish process, searched to a fixed depth, made to give reproducible answers.

The position cache (M3) stores evaluations under (position, engine version,
depth), which is only honest if the evaluation is a *function* of those three.
Out of the box, Stockfish's is not. Measured on this project's pinned sf_18 at
depth 16, the same position searched three times after different preceding
searches came back +264, +266 and +269: the transposition table carries
information from earlier searches into later ones. Three things close that gap:

* **The hash is cleared before every position** (`ucinewgame`). Measured cost:
  ~29 ms per position at 128 MB, about 7% of a depth-16 game. With it, the same
  position gave the same score and the same node count every time.
* **The engine sees exactly what the cache key contains.** The position is sent
  as its normalised FEN with no move history and zeroed counters, so nothing
  outside `fen_hash` - repetition history, the fifty-move counter - can change
  the answer stored under it.
* **One search thread** (see `Settings.engine_threads`). Multi-threaded search
  is non-deterministic by design.

The engine version is read from the binary's own UCI `id name`, not configured.
A configured string can disagree with the binary actually running; the binary
cannot. It does not include the SIMD build, because every build of one release
returns the same evaluations - only the speed differs.

It *does* include the hash size ('sf_18-h128'). With the table cleared before
every position, the hash size is the one engine setting left that changes the
answer: measured at depth 16, 16 MB and 128 MB disagreed on one position in
six, by 2 cp. Small, but a value that depends on an input the key does not
name is exactly the bug the key exists to prevent, so the input goes in the key.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import re
import time
from pathlib import Path
from types import TracebackType

import chess
import chess.engine

from chess_coach.logging import get_logger
from chess_coach.positions import Evaluation, normalize_fen

log = get_logger(__name__)

_INFO = chess.engine.INFO_BASIC | chess.engine.INFO_SCORE | chess.engine.INFO_PV
# How long to wait for a killed engine process to actually exit.
_KILL_WAIT_SEC = 5.0


class EngineError(Exception):
    """The engine could not produce an evaluation. Retryable: the engine has
    already been restarted by the time this is raised."""


class EngineTimeout(EngineError):
    pass


class EngineCrashed(EngineError):
    pass


def engine_version(name: str, *, hash_mb: int) -> str:
    """The cache's engine identifier: release plus every setting that changes
    the evaluation. 'Stockfish 18', 128 MB -> 'sf_18-h128'."""
    return f"{engine_version_from_name(name)}-h{hash_mb}"


def engine_version_from_name(name: str) -> str:
    """'Stockfish 18' -> 'sf_18', matching the release tag the binary is pinned by.

    'Stockfish 17.1' -> 'sf_17.1'; a development build keeps its full id
    ('Stockfish dev-20250101-abc' -> 'sf_dev-20250101-abc'), so two different
    dev builds can never share cache entries.
    """
    match = re.fullmatch(r"Stockfish\s+(\S+)", name.strip())
    if not match:
        raise EngineError(f"not a recognised Stockfish build: {name!r}")
    return f"sf_{match.group(1)}"


class StockfishEngine:
    """A long-lived engine process for one worker.

    `evaluate` returns scores from the point of view of the side to move, as
    UCI reports them - the `positions.Evaluation` contract. A search that takes
    longer than `timeout_sec` kills and restarts the process and raises
    `EngineTimeout`; a crashed process is restarted and raises `EngineCrashed`.
    The next call gets a fresh engine either way.
    """

    def __init__(
        self,
        command: str | Path | list[str],
        *,
        depth: int,
        threads: int = 1,
        hash_mb: int = 128,
        timeout_sec: float = 30.0,
    ) -> None:
        self._command = [str(c) for c in command] if isinstance(command, list) else str(command)
        self.depth = depth
        self._threads = threads
        self._hash_mb = hash_mb
        self._timeout_sec = timeout_sec
        self.restarts = 0
        if threads != 1:
            # Not a warning: multi-threaded search is non-deterministic (measured:
            # -186 and -213 for one position on two runs), and no cache key can
            # name "which thread finished first". Scale with processes instead.
            raise ValueError(
                f"engine threads must be 1 for reproducible evaluations, got {threads}"
            )
        self._engine = self._start()
        # 'sf_18' - the release, for logs.
        self.release = engine_version_from_name(self._engine.id.get("name", ""))
        # 'sf_18-h128' - what cached evaluations are filed under.
        self.version = engine_version(self._engine.id.get("name", ""), hash_mb=hash_mb)

    # --- lifecycle ----------------------------------------------------------

    def _start(self) -> chess.engine.SimpleEngine:
        engine = chess.engine.SimpleEngine.popen_uci(self._command, timeout=self._timeout_sec)
        try:
            engine.configure({"Threads": self._threads, "Hash": self._hash_mb})
        except Exception:
            self._kill(engine)
            raise
        return engine

    @staticmethod
    def _kill(engine: chess.engine.SimpleEngine) -> None:
        """Kill the process outright. `quit` is a request a hung engine ignores."""

        def _kill_on_loop() -> None:
            with contextlib.suppress(ProcessLookupError):
                engine.transport.kill()

        # RuntimeError: the event loop is already gone, so the process has exited.
        with contextlib.suppress(RuntimeError):
            engine.protocol.loop.call_soon_threadsafe(_kill_on_loop)
        engine.close()
        try:
            engine.returncode.result(timeout=_KILL_WAIT_SEC)
        except concurrent.futures.TimeoutError:
            log.warning("engine.kill_unconfirmed", pid=engine.transport.get_pid())

    def restart(self, reason: str) -> None:
        log.warning("engine.restart", reason=reason, restarts=self.restarts + 1)
        self._kill(self._engine)
        self.restarts += 1
        self._engine = self._start()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._engine.quit()
        self._kill(self._engine)

    def __enter__(self) -> StockfishEngine:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- evaluation ---------------------------------------------------------

    def evaluate(self, board: chess.Board) -> Evaluation:
        """Evaluate `board` to `self.depth`, side-to-move point of view."""
        if not any(board.generate_legal_moves()):
            # Nothing to search. A mated side to move is 'mate in 0' against
            # it, which is how UCI reports it too; stalemate is a dead draw.
            if board.is_check():
                return Evaluation(eval_cp=None, mate_in=0, best_move=None, nodes=0, compute_ms=0)
            return Evaluation(eval_cp=0, mate_in=None, best_move=None, nodes=0, compute_ms=0)

        # History-free and with zeroed counters: exactly what fen_hash covers.
        root = chess.Board(normalize_fen(board) + " 0 1")
        started = time.perf_counter()
        # SimpleEngine.analyse() applies no timeout at all to a depth-limited
        # search (its timeout only covers time-limited ones), so the coroutine
        # is driven directly and the wait is bounded here. A fresh `game`
        # object makes python-chess send `ucinewgame`, which clears the hash.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._engine.protocol.analyse(
                    root, chess.engine.Limit(depth=self.depth), game=object(), info=_INFO
                ),
                self._engine.protocol.loop,
            )
            info = future.result(timeout=self._timeout_sec)
        except concurrent.futures.TimeoutError:
            # Deliberately no future.cancel(). Cancelling python-chess's command
            # mid-search leaves it half-finished; killing the process then
            # raises InvalidStateError inside its callback, the process's exit
            # is never delivered, and the engine's (non-daemon) event-loop
            # thread blocks interpreter shutdown forever. Killing the process
            # instead fails the in-flight command the normal way.
            self.restart(f"no result within {self._timeout_sec}s")
            raise EngineTimeout(
                f"depth {self.depth} search exceeded {self._timeout_sec}s on {root.fen()}"
            ) from None
        except (chess.engine.EngineError, chess.engine.EngineTerminatedError, RuntimeError) as exc:
            self.restart(f"{type(exc).__name__}: {exc}")
            raise EngineCrashed(f"engine failed on {root.fen()}: {exc}") from exc
        compute_ms = round((time.perf_counter() - started) * 1000)

        score = info["score"].relative
        pv = info.get("pv") or []
        mate = score.mate()
        return Evaluation(
            eval_cp=None if mate is not None else score.score(),
            mate_in=mate,
            best_move=pv[0].uci() if pv else None,
            nodes=info.get("nodes"),
            compute_ms=compute_ms,
        )
