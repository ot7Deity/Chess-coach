"""Sync one player's Chess.com history into Postgres.

The first sync loads every month. Every sync after that costs two requests -
the archive list and the current month - because a month that has ended can
never change again and is marked complete once it has been fetched after its
end. The current month is revalidated with its ETag, so an unchanged month is a
0-byte 304.

Each month is committed on its own. A sync that dies halfway leaves the months
it finished marked done, and the next run starts where it stopped.

Correctness does not depend on the lock: games are upserted on
(player_id, chesscom_uuid), so two overlapping syncs would still store each game
once. The per-player advisory lock exists to stop them *spending rate budget*
fetching the same months twice.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, func, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from chess_coach.chesscom import (
    ChessComClient,
    GameParseError,
    normalize_username,
    parse_game,
)
from chess_coach.logging import get_logger
from chess_coach.models import Archive, Game, Player

log = get_logger(__name__)

# How long after a month ends (in UTC) before it is treated as final. Chess.com
# files a game under its end month in US Pacific time, not UTC - measured on
# deitythemenace: 71 of 4,550 games ended up to 7h26m into the next UTC month
# and were filed under the earlier one; 0 were filed outside their Pacific
# month. So a UTC month keeps changing for up to 8 hours after it ends. A day
# covers that with margin and costs one extra 304 at most.
ARCHIVE_GRACE = dt.timedelta(hours=24)

# First key of pg_try_advisory_lock(int, int). Namespaces ingest's locks so they
# cannot collide with the queue's, which will key on job ids.
_LOCK_NAMESPACE = 1

# Rows per INSERT. ~22 bound parameters per game keeps this far below
# Postgres's 65,535-parameter limit while still being one round trip for a
# typical month.
_UPSERT_CHUNK = 500


class SyncInProgress(Exception):
    """Another process is already syncing this player."""


@dataclass
class SyncResult:
    username: str
    months_listed: int = 0
    months_skipped: int = 0  # complete, so never requested
    months_not_modified: int = 0  # requested, 304
    months_fetched: int = 0  # requested, 200
    games_seen: int = 0
    games_new: int = 0
    games_rejected: int = 0
    requests: int = 0


def month_end(month: str) -> dt.datetime:
    """The first instant after `month` ('YYYY-MM'), in UTC."""
    year, mm = (int(part) for part in month.split("-"))
    if mm == 12:
        return dt.datetime(year + 1, 1, 1, tzinfo=dt.UTC)
    return dt.datetime(year, mm + 1, 1, tzinfo=dt.UTC)


def is_final(month: str, fetched_at: dt.datetime) -> bool:
    """Whether a fetch at `fetched_at` saw `month` in its final state."""
    return fetched_at >= month_end(month) + ARCHIVE_GRACE


def sync_player(
    engine: Engine,
    client: ChessComClient,
    username: str,
    *,
    now: dt.datetime | None = None,
) -> SyncResult:
    """Bring one player's games up to date. Creates the player on first use.

    Raises `PlayerNotFound` for an unknown username (and creates no row), and
    `SyncInProgress` if another process holds this player's lock. Any other
    failure marks the player `sync_status='error'` and is re-raised.
    """
    username = normalize_username(username)
    now = now or dt.datetime.now(dt.UTC)
    result = SyncResult(username=username)
    requests_before = client.requests_sent

    player_id = _ensure_player(engine, client, username)

    # A dedicated AUTOCOMMIT connection holds the lock. Session-level advisory
    # locks belong to the connection, and holding a normal connection open for
    # the length of a sync would leave a transaction "idle in transaction" the
    # whole time.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_conn:
        got = lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(:ns, :id)"),
            {"ns": _LOCK_NAMESPACE, "id": player_id},
        )
        if not got:
            raise SyncInProgress(f"{username} is already being synced")
        try:
            _set_status(engine, player_id, "syncing")
            try:
                _sync_months(engine, client, player_id, username, now, result)
            # BaseException, not Exception: Ctrl-C (KeyboardInterrupt) and
            # SystemExit are not Exceptions, and missing them left a player
            # marked 'syncing' forever. A hard kill still can; see
            # INTERVIEW_PREP.md.
            except BaseException as exc:
                _set_status(engine, player_id, "error", error=f"{type(exc).__name__}: {exc}")
                raise
            _set_status(engine, player_id, "complete", synced_at=now)
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:ns, :id)"),
                {"ns": _LOCK_NAMESPACE, "id": player_id},
            )

    result.requests = client.requests_sent - requests_before
    log.info("sync.complete", **vars(result))
    return result


# --- steps ------------------------------------------------------------------


def _ensure_player(engine: Engine, client: ChessComClient, username: str) -> int:
    with Session(engine) as session:
        player_id = session.scalar(select(Player.id).where(Player.username == username))
    if player_id is not None:
        return player_id

    # First sight of this username: ask Chess.com before creating anything, so
    # a typo raises PlayerNotFound instead of leaving a row that can never sync.
    profile = client.profile(username)
    joined = profile.get("joined")
    with Session(engine) as session, session.begin():
        # ON CONFLICT: two first-time syncs of the same player can both get
        # here; the loser just reads the winner's row.
        session.execute(
            insert(Player)
            .values(
                username=username,
                chesscom_player_id=profile.get("player_id"),
                joined_at=dt.datetime.fromtimestamp(joined, tz=dt.UTC) if joined else None,
                sync_status="pending",
            )
            .on_conflict_do_nothing(index_elements=[Player.username])
        )
        return session.scalar(select(Player.id).where(Player.username == username))


def _set_status(
    engine: Engine,
    player_id: int,
    status: str,
    *,
    error: str | None = None,
    synced_at: dt.datetime | None = None,
) -> None:
    values: dict[str, Any] = {"sync_status": status, "sync_error": error}
    if synced_at is not None:
        values["last_synced_at"] = synced_at
    with Session(engine) as session, session.begin():
        session.execute(update(Player).where(Player.id == player_id).values(**values))


def _sync_months(
    engine: Engine,
    client: ChessComClient,
    player_id: int,
    username: str,
    now: dt.datetime,
    result: SyncResult,
) -> None:
    months = client.archive_months(username)
    result.months_listed = len(months)

    with Session(engine) as session:
        known = {
            a.month: (a.etag, a.is_complete)
            for a in session.scalars(select(Archive).where(Archive.player_id == player_id))
        }

    for month in months:
        etag, complete = known.get(month, (None, False))
        if complete:
            result.months_skipped += 1
            continue

        resp = client.month(username, month, etag=etag)
        final = is_final(month, now)

        with Session(engine) as session, session.begin():
            archive_values: dict[str, Any] = {
                "etag": resp.etag,
                "last_modified": resp.last_modified,
                "fetched_at": now,
                "is_complete": final,
            }
            if resp.not_modified:
                result.months_not_modified += 1
            else:
                result.months_fetched += 1
                rejected_before = result.games_rejected
                rows = _parse_month(resp.games, username, player_id, month, result)
                result.games_new += _upsert_games(session, rows)
                archive_values["game_count"] = len(rows)
                if result.games_rejected > rejected_before:
                    # A month with a game we could not parse is never frozen,
                    # and its ETag is dropped so the next sync downloads it in
                    # full rather than getting a 304. Otherwise a parser fix
                    # could never recover the game: the month would be skipped
                    # forever and nothing would record that a game was missing.
                    archive_values["is_complete"] = False
                    archive_values["etag"] = None

            session.execute(
                insert(Archive)
                .values(player_id=player_id, month=month, **archive_values)
                .on_conflict_do_update(
                    constraint="uq_archives_player_month",
                    set_={**archive_values, "updated_at": func.now()},
                )
            )

        log.info(
            "sync.month",
            username=username,
            month=month,
            not_modified=resp.not_modified,
            final=final,
        )


def _parse_month(
    games: list[dict[str, Any]],
    username: str,
    player_id: int,
    month: str,
    result: SyncResult,
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in games:
        result.games_seen += 1
        try:
            row = parse_game(raw, username)
        except GameParseError as exc:
            # One malformed game must not cost the other few hundred in the
            # month, but it is counted and logged with its id, never dropped
            # without a trace.
            result.games_rejected += 1
            log.warning("sync.game_rejected", month=month, uuid=raw.get("uuid"), reason=str(exc))
            continue
        row["player_id"] = player_id
        # Deduplicated by uuid before the INSERT. One `INSERT ... ON CONFLICT
        # DO UPDATE` may not touch the same row twice, so a month listing a
        # game twice would fail the whole month - on every retry, forever.
        if row["chesscom_uuid"] in rows:
            log.warning("sync.game_duplicated", month=month, uuid=row["chesscom_uuid"])
        rows[row["chesscom_uuid"]] = row
    return list(rows.values())


def _upsert_games(session: Session, rows: list[dict[str, Any]]) -> int:
    """Insert new games; return how many were new.

    A game that already exists is never rewritten - least of all its
    `analysis_status`, which would send finished games back to the queue. The
    one field allowed to change is Chess.com's accuracy, which appears after
    someone requests a review. The WHERE clause means re-fetching an unchanged
    month writes zero rows rather than rewriting every game in it.
    """
    new = 0
    games = Game.__table__
    for start in range(0, len(rows), _UPSERT_CHUNK):
        stmt = insert(Game).values(rows[start : start + _UPSERT_CHUNK])
        acc_player = func.coalesce(stmt.excluded.accuracy_player, games.c.accuracy_player)
        acc_opp = func.coalesce(stmt.excluded.accuracy_opponent, games.c.accuracy_opponent)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_games_player_uuid",
            set_={
                "accuracy_player": acc_player,
                "accuracy_opponent": acc_opp,
                "updated_at": func.now(),
            },
            where=or_(
                acc_player.is_distinct_from(games.c.accuracy_player),
                acc_opp.is_distinct_from(games.c.accuracy_opponent),
            ),
            # `xmax = 0` is true only for a row this statement inserted; an
            # updated row carries the id of the transaction that updated it.
        ).returning(literal_column("xmax = 0"))
        new += sum(1 for (inserted,) in session.execute(stmt) if inserted)
    return new
