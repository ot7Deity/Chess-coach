"""Live acceptance test: sync the real deitythemenace account and check that
what lands in Postgres agrees with independent sources of truth.

    RUN_LIVE=1 uv run pytest tests/review/test_live_deitythemenace.py -s

Skipped unless RUN_LIVE=1. It sends about 60 requests at 1 request/second
through the real RedisTokenBucket on the default key, so run it rarely.

The bucket lives in the *ingester's* Redis database (LIVE_RATELIMIT_REDIS_URL,
default db 0), not the test database: Redis databases are separate keyspaces,
so a bucket in db 4 would not share the real budget with a running ingester.
Only the bucket key is touched there; nothing else in this suite uses db 0.

The sources of truth, none of which go through parse.py:

* Chess.com's own `stats` endpoint: rated win/loss/draw per time class.
* The raw archive JSON the sync downloaded, captured on the way past: total
  game count and uuid uniqueness.
* Each game's own PGN headers (Result, White/Black, ECO, EndDate/EndTime,
  WhiteElo/BlackElo), parsed independently with python-chess.
"""

from __future__ import annotations

import datetime as dt
import io
import os
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

import chess
import chess.pgn
import pytest
import redis
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from chess_coach.chesscom import ChessComClient, MonthResponse
from chess_coach.config import Settings
from chess_coach.ingest.sync import ARCHIVE_GRACE, month_end, sync_player
from chess_coach.models import Archive, Game
from chess_coach.ratelimit import RedisTokenBucket

USERNAME = "deitythemenace"
CONTACT = "https://github.com/ot7Deity/Chess-coach"
TIME_CLASSES = ("bullet", "blitz", "rapid", "daily")
RATELIMIT_REDIS_URL = os.environ.get("LIVE_RATELIMIT_REDIS_URL", "redis://localhost:6379/0")

pytestmark = [
    pytest.mark.live,
    pytest.mark.postgres,
    pytest.mark.redis,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1"),
]


class RecordingClient(ChessComClient):
    """The real client, keeping a copy of every month body it downloads."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.raw_months: dict[str, list[dict[str, Any]]] = {}

    def month(self, username: str, month: str, *, etag: str | None = None) -> MonthResponse:
        resp = super().month(username, month, etag=etag)
        if not resp.not_modified:
            self.raw_months[month] = resp.games
        return resp

    def stats(self, username: str) -> dict[str, Any]:
        return self._get(f"player/{username}/stats").json()


@pytest.fixture
def live_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHESSCOM_CONTACT", CONTACT)
    settings = Settings()
    assert "@" not in settings.user_agent, settings.user_agent
    conn = redis.Redis.from_url(RATELIMIT_REDIS_URL)
    limiter = RedisTokenBucket(conn, rate_per_sec=1.0)  # default key: ratelimit:chesscom
    client = RecordingClient(user_agent=settings.user_agent, limiter=limiter)
    try:
        yield client
    finally:
        client.close()
        conn.close()


def _pgn_utc(headers: chess.pgn.Headers, date_key: str, time_key: str) -> dt.datetime:
    return dt.datetime.strptime(
        f"{headers[date_key]} {headers[time_key]}", "%Y.%m.%d %H:%M:%S"
    ).replace(tzinfo=dt.UTC)


def test_live_sync_of_deitythemenace_comes_out_correct(
    clean_engine: Engine, live_client: RecordingClient
):
    # --- first sync -----------------------------------------------------------
    first = sync_player(clean_engine, live_client, USERNAME)
    stats = live_client.stats(USERNAME)
    raw = live_client.raw_months

    with Session(clean_engine) as s:
        games = list(s.scalars(select(Game)))
        archives = list(s.scalars(select(Archive)))

    report: list[str] = [
        f"first sync: {first}",
        f"archives listed={first.months_listed} complete="
        f"{sum(a.is_complete for a in archives)} games stored={len(games)}",
    ]

    # --- 1. per time class W/L/D vs Chess.com's stats ---------------------------
    ours: Counter[tuple[str, str]] = Counter(
        (g.time_class, g.result) for g in games if g.rules == "chess" and g.rated
    )
    rows, mismatches = [], []
    for tc in TIME_CLASSES:
        record = stats.get(f"chess_{tc}", {}).get("record", {})
        theirs = (record.get("win", 0), record.get("loss", 0), record.get("draw", 0))
        mine = (ours[(tc, "win")], ours[(tc, "loss")], ours[(tc, "draw")])
        rows.append(
            f"| {tc:<7} | {theirs[0]:>5}/{theirs[1]:>5}/{theirs[2]:>4} | "
            f"{mine[0]:>5}/{mine[1]:>5}/{mine[2]:>4} | {'OK' if mine == theirs else 'DIFF'} |"
        )
        if mine != theirs:
            mismatches.append((tc, theirs, mine))
    report += ["| class   | chess.com W/L/D   | ours W/L/D        |      |", *rows]

    # Not asserted: other rules, unrated, by count, for the record.
    others = Counter(
        (g.rules, g.time_class, g.rated) for g in games if g.rules != "chess" or not g.rated
    )
    report.append(f"non-standard or unrated: {dict(others)}")

    # --- 2. uuids vs the raw archive bodies -------------------------------------
    raw_uuids = [g["uuid"] for month in raw.values() for g in month]
    stored_uuids = {g.chesscom_uuid for g in games}
    report.append(
        f"raw games={len(raw_uuids)} distinct raw uuids={len(set(raw_uuids))} "
        f"distinct stored uuids={len(stored_uuids)} rejected={first.games_rejected}"
    )

    # --- 3. archive month vs played_at, i.e. is the UTC-month assumption true --
    by_uuid_month = {g["uuid"]: m for m, month in raw.items() for g in month}
    off_month = []
    for g in games:
        filed = by_uuid_month[g.chesscom_uuid]
        if g.played_at.strftime("%Y-%m") != filed:
            # How far outside the month it was filed under.
            start = month_end(filed) - dt.timedelta(days=1)
            start = start.replace(day=1)
            off = min(abs(g.played_at - start), abs(g.played_at - month_end(filed)))
            off_month.append((g.chesscom_uuid, filed, g.played_at.isoformat(), off))
    later = sum(1 for _, filed, at, _ in off_month if at[:7] > filed)
    worst = max((off for *_, off in off_month), default=dt.timedelta(0))
    # The first live run showed games ending up to ~7 h into the next UTC month
    # filed under the previous one: consistent with Chess.com filing by US
    # Pacific end date. Test that hypothesis against every game.
    pacific = ZoneInfo("America/Los_Angeles")
    not_pacific = [
        (g.chesscom_uuid, by_uuid_month[g.chesscom_uuid], g.played_at.isoformat())
        for g in games
        if g.played_at.astimezone(pacific).strftime("%Y-%m") != by_uuid_month[g.chesscom_uuid]
    ]
    report.append(
        f"games filed outside their UTC end month: {len(off_month)} "
        f"(filed under the earlier month: {later}; furthest from the boundary: {worst})"
    )
    report.append(
        f"games NOT filed under their America/Los_Angeles end month: "
        f"{len(not_pacific)} {not_pacific[:5]}"
    )

    # --- 4. every game against its own PGN headers ------------------------------
    header_problems: list[str] = []
    result_header = {
        ("white", "win"): "1-0",
        ("black", "win"): "0-1",
        ("white", "loss"): "0-1",
        ("black", "loss"): "1-0",
    }
    for g in games:
        h = chess.pgn.read_headers(io.StringIO(g.pgn))
        me_header = h.get("White") if g.color == "white" else h.get("Black")
        if (me_header or "").lower() != USERNAME:
            header_problems.append(
                f"{g.chesscom_uuid}: {g.color} but W/B={h.get('White')}/{h.get('Black')}"
            )
        expected = result_header.get((g.color, g.result), "1/2-1/2")
        if h.get("Result") != expected:
            header_problems.append(
                f"{g.chesscom_uuid}: {g.color} {g.result} but Result={h.get('Result')}"
            )
        if h.get("Timezone") == "UTC" and "EndDate" in h and "EndTime" in h:
            if _pgn_utc(h, "EndDate", "EndTime") != g.played_at:
                header_problems.append(
                    f"{g.chesscom_uuid}: {g.played_at} vs End {h['EndDate']} {h['EndTime']}"
                )
        else:
            header_problems.append(
                f"{g.chesscom_uuid}: no UTC EndDate/EndTime (Timezone={h.get('Timezone')})"
            )
        if g.rules == "chess" and g.start_fen is None:
            eco = h.get("ECO")
            if g.opening_eco != (eco if eco and len(eco) == 3 else None):
                header_problems.append(f"{g.chesscom_uuid}: eco {g.opening_eco} but header {eco}")
        elo_me = h.get("WhiteElo") if g.color == "white" else h.get("BlackElo")
        if elo_me and g.player_rating is not None and int(elo_me) != g.player_rating:
            header_problems.append(f"{g.chesscom_uuid}: rating {g.player_rating} but Elo {elo_me}")
        if g.start_fen is not None and h.get("FEN") not in (None, g.start_fen):
            header_problems.append(f"{g.chesscom_uuid}: start_fen differs from FEN header")
    std_with_fen = sum(1 for g in games if g.rules == "chess" and g.start_fen is not None)
    report.append(f"PGN header disagreements: {len(header_problems)} {header_problems[:5]}")
    report.append(f"standard-rules games with a custom start_fen: {std_with_fen}")
    report.append(f"analysis_status: {dict(Counter(g.analysis_status for g in games))}")

    # --- 5. second sync -------------------------------------------------------
    second = sync_player(clean_engine, live_client, USERNAME)
    incomplete = sum(1 for a in archives if not a.is_complete)
    report.append(f"second sync: {second}")
    report.append(f"months incomplete after first sync: {incomplete}")

    print("\n" + "\n".join(report))

    # --- assertions, after the report is printed ------------------------------
    assert mismatches == [], mismatches
    assert len(raw_uuids) == len(set(raw_uuids)), "a uuid appears twice in the archives"
    assert len(stored_uuids) == len(raw_uuids), (len(stored_uuids), len(raw_uuids))
    assert first.games_rejected == 0
    assert header_problems == []
    assert all(off <= ARCHIVE_GRACE for *_, off in off_month), off_month
    assert second.requests == 1 + incomplete
    assert second.requests == 2
    assert second.games_new == 0
