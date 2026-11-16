"""Replay one game and grade every ply. No database, no engine of its own.

The evaluator is passed in, so this is the same code whether positions come
from Stockfish, from M3's cache, or from a dictionary in a unit test.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import chess
import chess.pgn

from chess_coach.analysis.classify import classify_move, mover_after, mover_before, phase
from chess_coach.analysis.clock import move_times, parse_time_control
from chess_coach.jobrunner import PermanentJobError
from chess_coach.positions import Evaluation, fen_hash

Evaluator = Callable[[chess.Board], Evaluation]


class BadGame(PermanentJobError):
    """The stored game cannot be replayed. Permanent: retrying will not fix it."""


@dataclass(frozen=True)
class Ply:
    """One move of a replayed game, before any engine work."""

    ply: int  # from 1
    board_before: chess.Board
    move: chess.Move
    san: str
    clock_seconds: float | None


@dataclass(frozen=True)
class MoveRow:
    """Column values for one `moves` row (minus game_id)."""

    ply: int
    fen_hash: bytes
    san: str
    uci: str
    clock_seconds: float | None
    move_time_seconds: float | None
    eval_cp_before: int | None
    eval_cp_after: int | None
    mate_in_before: int | None
    mate_in_after: int | None
    classification: str
    phase: str
    is_player_move: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def replay(pgn: str, start_fen: str | None) -> tuple[list[Ply], chess.Board]:
    """Every ply of the game's main line, and the final position.

    Moves are replayed onto `start_fen` when set (`games.start_fen`, the
    column ingestion fills from the PGN's SetUp/FEN headers), the normal start
    otherwise, and each one is checked for legality on that board. A game whose
    moves do not fit its starting position is rejected rather than graded on
    the wrong board.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise BadGame("no game in PGN")
    if game.errors:
        raise BadGame(f"unreadable PGN: {game.errors[0]}")

    try:
        board = chess.Board(start_fen) if start_fen else chess.Board()
    except ValueError as exc:
        raise BadGame(f"invalid start_fen {start_fen!r}: {exc}") from exc

    plies: list[Ply] = []
    for number, node in enumerate(game.mainline(), start=1):
        move = node.move
        if not board.is_legal(move):
            raise BadGame(f"ply {number}: {move.uci()} is illegal in {board.fen()}")
        plies.append(
            Ply(
                ply=number,
                board_before=board.copy(stack=False),
                move=move,
                san=board.san(move),
                clock_seconds=node.clock(),
            )
        )
        board.push(move)
    return plies, board.copy(stack=False)


def analyze_game(
    pgn: str,
    *,
    start_fen: str | None,
    player_color: str,
    time_control: str | None,
    evaluate: Evaluator,
    on_position: Callable[[], None] | None = None,
) -> list[MoveRow]:
    """Grade every ply. Calls `evaluate` exactly once per position.

    A game of N plies has N + 1 positions; position k is "after" ply k and
    "before" ply k + 1, so each evaluation is used twice. `on_position` runs
    after each evaluation - the worker heartbeats its lease from there, and an
    exception it raises (a lost lease) abandons the game.
    """
    plies, final = replay(pgn, start_fen)
    if not plies:
        return []

    boards = [p.board_before for p in plies] + [final]
    evals: list[Evaluation] = []
    for board in boards:
        evals.append(evaluate(board))
        if on_position is not None:
            on_position()

    player = chess.WHITE if player_color == "white" else chess.BLACK
    movers = [p.board_before.turn for p in plies]
    spent = move_times([p.clock_seconds for p in plies], movers, parse_time_control(time_control))

    rows = []
    for i, p in enumerate(plies):
        before = mover_before(evals[i])
        after = mover_after(evals[i + 1])
        rows.append(
            MoveRow(
                ply=p.ply,
                # The position the move was played from: that row in
                # `positions` holds the engine's best move to compare against.
                fen_hash=fen_hash(p.board_before),
                san=p.san,
                uci=p.move.uci(),
                clock_seconds=p.clock_seconds,
                move_time_seconds=spent[i],
                eval_cp_before=before.cp,
                eval_cp_after=after.cp,
                mate_in_before=before.mate,
                mate_in_after=after.mate,
                classification=classify_move(evals[i], evals[i + 1], p.move.uci()),
                phase=phase(p.board_before),
                is_player_move=movers[i] == player,
            )
        )
    return rows
