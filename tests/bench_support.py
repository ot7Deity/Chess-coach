"""Shared workload for the benchmark tests: six games over three openings."""

from __future__ import annotations

import datetime as dt
import random

import chess
import chess.pgn
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from chess_coach.models import Game, Player

OPENINGS = [
    ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
    ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"],
    ["d4", "d5", "c4", "e6", "Nc3", "Nf6"],
]


def make_pgn(opening: list[str], seed: int) -> str:
    rng = random.Random(seed)
    board = chess.Board()
    for san in opening:
        board.push_san(san)
    for _ in range(10):
        if board.is_game_over():
            break
        board.push(rng.choice(sorted(board.legal_moves, key=lambda m: m.uci())))
    return str(chess.pgn.Game.from_board(board))


def create_workload(engine: Engine, games: int = 6) -> tuple[list[int], list[str]]:
    """Insert `games` games for one player; positions repeat across games."""
    pgns = [make_pgn(OPENINGS[i % 3], seed=i) for i in range(games)]
    with Session(engine) as session:
        player = Player(username="benchplayer")
        session.add(player)
        session.flush()
        rows = [
            Game(
                chesscom_uuid=f"bench-{i}",
                player_id=player.id,
                color="white" if i % 2 == 0 else "black",
                result="draw",
                time_class="blitz",
                time_control="180+2",
                pgn=pgn,
                played_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(hours=i),
            )
            for i, pgn in enumerate(pgns)
        ]
        session.add_all(rows)
        session.commit()
        return [g.id for g in rows], pgns
