"""Turn one Chess.com game JSON into a `games` row, from one player's side.

Pure functions, no I/O. Written against real archive responses rather than the
docs; the corrections that came out of that are in INTERVIEW_PREP.md.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import chess
import chess.pgn

from chess_coach.models import TIME_CLASSES

# Player-side `result` codes that mean the game was drawn. Every decisive game
# marks the winner with exactly 'win', and the loser with the reason they lost
# ('resigned', 'timeout', 'checkmated', 'abandoned', ...). So win and loss are
# decided by where 'win' appears, which keeps working if Chess.com adds a new
# way to lose; only a draw has to be recognised by name.
DRAW_CODES = frozenset(
    {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
)

_ECO = re.compile(r"^[A-E][0-9]{2}$")
# Where an opening URL slug stops naming the opening and starts listing moves:
# 'French-Defense-Kings-Indian-Attack...6.Bg2-Nge7' -> at '...6.'
_SLUG_MOVES = re.compile(r"\.{0,3}\d+\.")


class GameParseError(ValueError):
    """A game that cannot be stored. Logged and skipped, never silently dropped."""


def _side(raw: dict[str, Any], color: str) -> dict[str, Any]:
    side = raw.get(color)
    if not isinstance(side, dict) or not side.get("username") or not side.get("result"):
        raise GameParseError(f"missing or incomplete '{color}' side")
    return side


def _opening_name(url: str | None) -> str | None:
    """'.../openings/Closed-Sicilian-Defense-Grand-Prix' -> 'Closed Sicilian Defense Grand Prix'.

    Chess.com only provides the opening name as a URL slug. Apostrophes are lost
    ('Kings Indian'), so this is a display label; the ECO code is the key.
    """
    if not url:
        return None
    slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    match = _SLUG_MOVES.search(slug)
    if match:
        slug = slug[: match.start()]
    name = " ".join(part for part in slug.split("-") if part)
    return name or None


def _start_fen(headers: chess.pgn.Headers, raw: dict[str, Any]) -> str | None:
    """The starting position, or None for the normal one.

    The PGN FEN header wins over the JSON `initial_setup` field: for chess960
    the header uses Shredder-FEN castling ('HBhb') while `initial_setup` says
    'KQkq', and only the former tells the analysis worker which rooks can
    castle.
    """
    fen = headers.get("FEN") if headers.get("SetUp") == "1" else None
    fen = fen or raw.get("initial_setup")
    if not fen or fen == chess.STARTING_FEN:
        return None
    return fen


def parse_game(raw: dict[str, Any], username: str) -> dict[str, Any]:
    """Column values for one `games` row, minus `player_id`.

    `username` must already be normalised (lowercase). Every returned dict has
    the same keys, which a multi-row INSERT requires.
    """
    uuid = raw.get("uuid")
    if not uuid:
        raise GameParseError("no uuid")

    white, black = _side(raw, "white"), _side(raw, "black")
    if white["username"].lower() == username:
        color, me, opp = "white", white, black
    elif black["username"].lower() == username:
        color, me, opp = "black", black, white
    else:
        raise GameParseError(f"{username!r} played neither side")

    # Termination is the loser's code for a decisive game, so 'win' +
    # 'timeout' reads as "opponent flagged" and 'loss' + 'timeout' as "I did".
    if me["result"] == "win":
        result, termination = "win", opp["result"]
    elif opp["result"] == "win":
        result, termination = "loss", me["result"]
    elif me["result"] in DRAW_CODES:
        result, termination = "draw", me["result"]
    else:
        raise GameParseError(f"unrecognised result pair {me['result']!r}/{opp['result']!r}")

    time_class = raw.get("time_class")
    if time_class not in TIME_CLASSES:
        raise GameParseError(f"unknown time_class {time_class!r}")

    pgn = raw.get("pgn")
    if not pgn:
        raise GameParseError("no pgn")
    headers = chess.pgn.read_headers(io.StringIO(pgn))
    if headers is None:
        raise GameParseError("unreadable pgn headers")

    end_time = raw.get("end_time")
    if not isinstance(end_time, int):
        raise GameParseError("no end_time")

    rules = raw.get("rules") or "chess"
    start_fen = _start_fen(headers, raw)

    # An opening code only means something from the normal starting position.
    # Chess.com still attaches one to chess960 games ('A20' for a shuffled back
    # rank), and letting those into "results by opening" would be noise.
    if rules == "chess" and start_fen is None:
        eco = headers.get("ECO")
        opening_eco = eco if eco and _ECO.match(eco) else None
        opening_name = _opening_name(headers.get("ECOUrl") or raw.get("eco"))
    else:
        opening_eco = opening_name = None

    accuracies = raw.get("accuracies") or {}
    opp_color = "black" if color == "white" else "white"

    if rules == "chess":
        analysis_status, skip_reason = "pending", None
    else:
        analysis_status, skip_reason = "skipped", f"unsupported rules: {rules}"

    return {
        "chesscom_uuid": uuid,
        "url": raw.get("url"),
        "color": color,
        "result": result,
        "termination": termination[:32],
        "time_class": time_class,
        "time_control": str(raw.get("time_control", ""))[:16],
        "rated": bool(raw.get("rated", True)),
        "rules": rules[:16],
        "opening_eco": opening_eco,
        "opening_name": opening_name,
        "pgn": pgn,
        "start_fen": start_fen,
        "played_at": dt.datetime.fromtimestamp(end_time, tz=dt.UTC),
        "player_rating": me.get("rating"),
        "opponent_rating": opp.get("rating"),
        "opponent_username": opp["username"][:64],
        "accuracy_player": accuracies.get(color),
        "accuracy_opponent": accuracies.get(opp_color),
        "analysis_status": analysis_status,
        "analysis_skip_reason": skip_reason,
    }
