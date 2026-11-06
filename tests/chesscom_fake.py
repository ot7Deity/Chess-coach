"""An in-memory Chess.com, served through httpx.MockTransport.

The real `ChessComClient` runs against it unchanged, so ETag revalidation,
404s and retries are exercised at the HTTP level rather than by stubbing the
client's methods. Game JSON follows the shape of real archive responses.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx

from chess_coach.chesscom import ChessComClient


class NoopLimiter:
    def __init__(self) -> None:
        self.acquired = 0
        self.penalties: list[float] = []

    def acquire(self) -> None:
        self.acquired += 1

    def penalize(self, seconds: float) -> None:
        self.penalties.append(seconds)


def make_game(
    uuid: str,
    *,
    white: str,
    black: str,
    white_result: str = "win",
    black_result: str = "resigned",
    end_time: dt.datetime | int = 1_790_000_000,
    time_class: str = "blitz",
    time_control: str = "180",
    rules: str = "chess",
    eco: str = "C00",
    eco_url: str = "https://www.chess.com/openings/French-Defense-Kings-Indian-Attack...6.Bg2",
    accuracies: dict[str, float] | None = None,
    fen_header: str | None = None,
    initial_setup: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
) -> dict[str, Any]:
    if isinstance(end_time, dt.datetime):
        end_time = int(end_time.timestamp())
    setup = f'[SetUp "1"]\n[FEN "{fen_header}"]\n' if fen_header else ""
    pgn = (
        '[Event "Live Chess"]\n[Site "Chess.com"]\n'
        f'[White "{white}"]\n[Black "{black}"]\n[Result "1-0"]\n{setup}'
        f'[ECO "{eco}"]\n[ECOUrl "{eco_url}"]\n[TimeControl "{time_control}"]\n\n'
        "1. e4 {[%clk 0:03:00]} 1... e6 {[%clk 0:02:59.9]} 2. d3 {[%clk 0:02:57.1]} 1-0\n"
    )
    game: dict[str, Any] = {
        "url": f"https://www.chess.com/game/live/{uuid}",
        "pgn": pgn,
        "time_control": time_control,
        "end_time": end_time,
        "rated": True,
        "uuid": uuid,
        "initial_setup": initial_setup,
        "time_class": time_class,
        "rules": rules,
        "white": {"rating": 1500, "result": white_result, "username": white},
        "black": {"rating": 1480, "result": black_result, "username": black},
        "eco": eco_url,
    }
    if accuracies is not None:
        game["accuracies"] = accuracies
    return game


@dataclass
class FakeChessCom:
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    months: dict[str, dict[str, list[dict[str, Any]]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    # (method path) of every request received, in order.
    log: list[str] = field(default_factory=list)
    # path -> list of status codes to return before behaving normally.
    failures: dict[str, list[int]] = field(default_factory=dict)

    def add_player(self, username: str, player_id: int = 1) -> None:
        self.profiles[username.lower()] = {
            "player_id": player_id,
            "username": username.lower(),
            "joined": 1_600_000_000,
        }

    def add_game(self, username: str, month: str, game: dict[str, Any]) -> None:
        self.months[username.lower()].setdefault(month, []).append(game)

    def fail(self, path: str, *statuses: int) -> None:
        self.failures.setdefault(path, []).extend(statuses)

    def paths(self) -> list[str]:
        return list(self.log)

    def client(self, **kwargs: Any) -> ChessComClient:
        kwargs.setdefault("limiter", NoopLimiter())
        kwargs.setdefault("sleep", lambda _s: None)
        return ChessComClient(
            user_agent="ChessCoach/test (test@example.invalid)",
            base_url="https://api.chess.com/pub",
            transport=httpx.MockTransport(self.handle),
            **kwargs,
        )

    # --- the server ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/pub/")
        self.log.append(path)

        queued = self.failures.get(path)
        if queued:
            return httpx.Response(queued.pop(0), json={"message": "injected"})

        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "player" or parts[1] not in self.profiles:
            return httpx.Response(404, json={"code": 0, "message": "not found"})
        username = parts[1]

        if len(parts) == 2:
            return httpx.Response(200, json=self.profiles[username])

        if parts[2:] == ["games", "archives"]:
            months = sorted(self.months[username])
            urls = [
                f"https://api.chess.com/pub/player/{username}/games/{m.replace('-', '/')}"
                for m in months
            ]
            return httpx.Response(200, json={"archives": urls})

        if len(parts) == 5 and parts[2] == "games":
            month = f"{parts[3]}-{parts[4]}"
            games = self.months[username].get(month)
            if games is None:
                return httpx.Response(404, json={"message": "no such month"})
            body = json.dumps({"games": games}).encode()
            etag = f'W/"{hashlib.sha1(body).hexdigest()}"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            return httpx.Response(
                200,
                content=body,
                headers={
                    "etag": etag,
                    "last-modified": "Sunday, 06-Jul-2025 21:04:08 GMT+0000",
                    "content-type": "application/json",
                },
            )

        return httpx.Response(404, json={"message": "unknown endpoint"})
