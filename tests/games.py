"""Game fixtures shaped like Chess.com's PGNs, clock comments included.

`LONG_GAME` is a 90-ply game generated once by sf_18 self-play at depth 5 with
some randomness (so it has real mistakes in it), then frozen here. Clock
readings are synthetic but follow Chess.com's rule: the reading after a move
is the mover's remaining time with the increment already added.
"""

from __future__ import annotations

import datetime as dt

import chess
from sqlalchemy import Engine, text

LONG_GAME = [
    "e4",
    "e5",
    "Bc4",
    "Qg5",
    "Nf3",
    "Qh5",
    "Nc3",
    "Nf6",
    "Nb5",
    "Bd6",
    "d4",
    "O-O",
    "O-O",
    "Ne8",
    "Nxe5",
    "Qxd1",
    "Rxd1",
    "Be7",
    "Nc3",
    "c6",
    "Bf1",
    "Na6",
    "d5",
    "Nb4",
    "Bd2",
    "d6",
    "Nc4",
    "Bg4",
    "dxc6",
    "bxc6",
    "f3",
    "Be6",
    "Ne3",
    "c5",
    "Rac1",
    "Bf6",
    "g4",
    "Rb8",
    "Be2",
    "Nc7",
    "Kg2",
    "Rfe8",
    "Be1",
    "Be5",
    "Ra1",
    "h5",
    "Bg3",
    "hxg4",
    "fxg4",
    "Bd4",
    "Nc4",
    "Bxc4",
    "Bxd6",
    "Nxc2",
    "Bxc7",
    "Bxe2",
    "Nxe2",
    "Rxb2",
    "Rac1",
    "Ne3+",
    "Kf3",
    "Nxd1",
    "Nxd4",
    "Nf2",
    "Nb3",
    "Nh3",
    "Nxc5",
    "Rc8",
    "e5",
    "Ng5+",
    "Kf4",
    "Rxc7",
    "Kxg5",
    "Re2",
    "e6",
    "Rxe6",
    "Nd3",
    "Rcc6",
    "Rb1",
    "Re8",
    "Rb7",
    "Rd6",
    "Rb3",
    "a6",
    "Ra3",
    "Re3",
    "Rb3",
    "Rd5+",
    "Kf4",
    "Rdxd3",
]

# 1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6?? 4. Qxf7#
SCHOLARS_MATE = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]

# White simply gives away a bishop on move 2.
HANGS_A_BISHOP = ["e4", "e5", "Ba6", "bxa6"]


def clocks_for(sans: list[str], base: float, increment: float) -> list[float]:
    """Deterministic readings: ply k costs (k % 7) seconds, first moves free."""
    remaining = {chess.WHITE: base, chess.BLACK: base}
    readings = []
    for k in range(len(sans)):
        mover = chess.WHITE if k % 2 == 0 else chess.BLACK
        spent = 0 if k < 2 else k % 7
        remaining[mover] = remaining[mover] - spent + increment
        readings.append(remaining[mover])
    return readings


def _clk(seconds: float) -> str:
    whole = int(seconds)
    tenths = round((seconds - whole) * 10)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" + (f".{tenths}" if tenths else "")


def chesscom_pgn(
    sans: list[str],
    *,
    time_control: str = "180+2",
    white: str = "alice",
    black: str = "bob",
    fen: str | None = None,
    clocks: bool = True,
) -> str:
    """A PGN in Chess.com's layout: `1. e4 {[%clk 0:03:02]} 1... e5 {...}`."""
    if "/" in time_control:  # daily, '1/86400': a clock that resets every move
        readings = [float(time_control.split("/")[1])] * len(sans)
    else:
        base, _, inc = time_control.partition("+")
        readings = clocks_for(sans, float(base), float(inc or 0))
    board = chess.Board(fen) if fen else chess.Board()
    setup = f'[SetUp "1"]\n[FEN "{fen}"]\n' if fen else ""
    parts = []
    for san, reading in zip(sans, readings, strict=True):
        number = board.fullmove_number
        if board.turn == chess.WHITE:
            parts.append(f"{number}. {san}")
        else:
            parts.append(f"{number}... {san}")
        if clocks:
            parts.append(f"{{[%clk {_clk(reading)}]}}")
        board.push_san(san)
    result = board.result(claim_draw=False) if board.is_game_over() else "*"
    return (
        f'[Event "Live Chess"]\n[Site "Chess.com"]\n[White "{white}"]\n[Black "{black}"]\n'
        f'[Result "{result}"]\n{setup}[TimeControl "{time_control}"]\n\n'
        + " ".join(parts)
        + f" {result}\n"
    )


def insert_game(
    engine: Engine,
    sans: list[str],
    *,
    username: str = "alice",
    uuid: str = "g-1",
    color: str = "white",
    time_class: str = "blitz",
    time_control: str = "180+2",
    analysis_status: str = "pending",
    start_fen: str | None = None,
    pgn: str | None = None,
) -> int:
    """Insert a player (if needed) and one game; return games.id."""
    with engine.begin() as c:
        player_id = c.execute(
            text(
                "INSERT INTO players (username, sync_status) VALUES (:u, 'complete') "
                "ON CONFLICT (username) DO UPDATE SET username = excluded.username RETURNING id"
            ),
            {"u": username},
        ).scalar_one()
        return c.execute(
            text(
                """
                INSERT INTO games (chesscom_uuid, player_id, color, result, time_class,
                    time_control, rated, rules, pgn, start_fen, played_at, analysis_status)
                VALUES (:uuid, :pid, :color, 'win', :tc, :tcv, true, 'chess', :pgn, :fen,
                    :played, :status)
                RETURNING id
                """
            ),
            {
                "uuid": uuid,
                "pid": player_id,
                "color": color,
                "tc": time_class,
                "tcv": time_control,
                "pgn": pgn
                if pgn is not None
                else chesscom_pgn(sans, time_control=time_control, fen=start_fen),
                "fen": start_fen,
                "played": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
                "status": analysis_status,
            },
        ).scalar_one()
