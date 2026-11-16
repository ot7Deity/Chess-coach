"""Response and request shapes, field for field from docs/api-contract.md.

Timestamps are strings, not datetimes, on purpose: the contract fixes the
format (UTC, second precision, a literal `Z`), and Pydantic's own datetime
serialisation would emit `+00:00` and microseconds.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, field_validator

from chess_coach.chesscom import normalize_username

TimeClass = Literal["bullet", "blitz", "rapid", "daily"]
SyncStatus = Literal["pending", "syncing", "complete", "error"]


def iso_z(value: dt.datetime | None) -> str | None:
    """UTC ISO 8601 with a Z suffix and whole seconds: '2026-11-06T02:14:05Z'."""
    if value is None:
        return None
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AddPlayerRequest(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def _normalise(cls, v: str) -> str:
        # The same rule the Chess.com client applies before putting a name in a
        # URL; a ValueError here becomes FastAPI's 422.
        return normalize_username(v)


class TimeClassProgress(BaseModel):
    time_class: TimeClass
    games: int
    analyzed: int
    pending: int
    failed: int
    skipped: int
    current_rating: int | None


class Summary(BaseModel):
    games: int
    wins: int
    draws: int
    losses: int
    win_rate_pct: float | None
    current_rating: int | None
    blunders_per_game: float | None


class RatingPoint(BaseModel):
    date: str  # the UTC day, 'YYYY-MM-DD'
    rating: int
    games: int


class OpeningResult(BaseModel):
    opening: str
    color: Literal["white", "black"]
    games: int
    wins: int
    draws: int
    losses: int
    win_rate_pct: float
    rating_change: int


class PhaseBlunders(BaseModel):
    phase: Literal["opening", "middlegame", "endgame"]
    player_moves: int
    blunders: int


class ClockBucket(BaseModel):
    moves: int
    accuracy_pct: float | None
    blunders: int


class TimePressure(BaseModel):
    threshold_seconds: int
    low_clock_games: int
    low_clock: ClockBucket
    normal_clock: ClockBucket


class Report(BaseModel):
    username: str
    time_class: TimeClass
    generated_at: str
    games: int
    analyzed_games: int
    summary: Summary
    rating_trend: list[RatingPoint]
    openings: list[OpeningResult]
    blunders_by_phase: list[PhaseBlunders] | None
    time_pressure: TimePressure | None


class PlayerProgress(BaseModel):
    username: str
    sync_status: SyncStatus
    sync_error: str | None
    last_synced_at: str | None
    analyzed_last_hour: int
    time_classes: list[TimeClassProgress]
