"""Hand-built games and moves for the report tests, column by column.

The report SQL is tested against rows whose expected answers can be worked out
by hand, so every value the queries read is set explicitly here.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from sqlalchemy import Engine, text

_uuids = itertools.count(1)


def add_player(engine: Engine, username: str = "deitythemenace", **cols: Any) -> int:
    values = {"username": username, "sync_status": "complete", **cols}
    names = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    with engine.begin() as c:
        return c.execute(
            text(f"INSERT INTO players ({names}) VALUES ({params}) RETURNING id"), values
        ).scalar_one()


def day(d: str, hour: int = 12, minute: int = 0) -> dt.datetime:
    """'2026-10-01' -> that day at hour:minute UTC."""
    y, m, dd = (int(p) for p in d.split("-"))
    return dt.datetime(y, m, dd, hour, minute, tzinfo=dt.UTC)


def add_game(engine: Engine, player_id: int, **cols: Any) -> int:
    values: dict[str, Any] = {
        "chesscom_uuid": f"u{next(_uuids)}",
        "player_id": player_id,
        "color": "white",
        "result": "win",
        "time_class": "blitz",
        "time_control": "180+2",
        "rated": True,
        "rules": "chess",
        "pgn": "*",
        "played_at": day("2026-10-01"),
        "analysis_status": "pending",
        **cols,
    }
    names = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    with engine.begin() as c:
        return c.execute(
            text(f"INSERT INTO games ({names}) VALUES ({params}) RETURNING id"), values
        ).scalar_one()


def add_moves(engine: Engine, game_id: int, moves: list[dict[str, Any]]) -> None:
    """Moves numbered from ply 1 in list order. Each dict overrides defaults."""
    rows = []
    for ply, m in enumerate(moves, start=1):
        rows.append(
            {
                "game_id": game_id,
                "ply": ply,
                "fen_hash": bytes(32),
                "san": "e4",
                "uci": "e2e4",
                "clock_seconds": None,
                "classification": "good",
                "phase": "middlegame",
                "is_player_move": ply % 2 == 1,
                **m,
            }
        )
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO moves (game_id, ply, fen_hash, san, uci, clock_seconds, "
                "classification, phase, is_player_move) VALUES (:game_id, :ply, :fen_hash, "
                ":san, :uci, :clock_seconds, :classification, :phase, :is_player_move)"
            ),
            rows,
        )
