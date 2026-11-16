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


class PlayerProgress(BaseModel):
    username: str
    sync_status: SyncStatus
    sync_error: str | None
    last_synced_at: str | None
    analyzed_last_hour: int
    time_classes: list[TimeClassProgress]
