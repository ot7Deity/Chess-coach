"""SQLAlchemy models.

Six tables, as in docs/DESIGN.md, with the corrections recorded in
INTERVIEW_PREP.md - most importantly that `positions` is keyed on
(position, engine version, depth) rather than position alone.

Status and category columns are `VARCHAR` with `CHECK` constraints rather than
Postgres `ENUM` types. Adding a value to a Postgres enum is a migration that
cannot run inside a transaction with other DDL on older versions, while
widening a CHECK constraint is ordinary. The constraint still stops a typo'd
status from reaching the table, which is the point.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- shared vocabularies ----------------------------------------------------

SYNC_STATUSES = ("pending", "syncing", "complete", "error")
ANALYSIS_STATUSES = ("pending", "running", "complete", "skipped", "error")
JOB_STATUSES = ("pending", "running", "done", "failed", "dead")
COLORS = ("white", "black")
RESULTS = ("win", "loss", "draw")
TIME_CLASSES = ("bullet", "blitz", "rapid", "daily")
CLASSIFICATIONS = ("best", "good", "inaccuracy", "mistake", "blunder")
PHASES = ("opening", "middlegame", "endgame")


def _check(column: str, allowed: tuple[str, ...], name: str) -> CheckConstraint:
    values = ", ".join(f"'{v}'" for v in allowed)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --- players ----------------------------------------------------------------


class Player(Base, TimestampMixin):
    """A Chess.com account being tracked."""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Chess.com usernames are case-insensitive; stored lowercased so that
    # "DeityTheMenace" and "deitythemenace" cannot become two rows that then
    # both sync the same archives.
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    chesscom_player_id: Mapped[int | None] = mapped_column(BigInteger)
    joined_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    sync_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    archives: Mapped[list[Archive]] = relationship(back_populates="player")
    games: Mapped[list[Game]] = relationship(back_populates="player")

    __table_args__ = (_check("sync_status", SYNC_STATUSES, "ck_players_sync_status"),)


# --- archives ---------------------------------------------------------------


class Archive(Base, TimestampMixin):
    """One month of one player's games, and the ETag that lets us skip it.

    This table is what makes re-syncing cheap: a player can have 100+ monthly
    archives and the API offers no "changed since" filter, so each month's ETag
    is replayed as `If-None-Match` and a 304 means skip.
    """

    __tablename__ = "archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )

    # 'YYYY-MM'. A string rather than a date because it names a whole month and
    # is used to build the API URL verbatim.
    month: Mapped[str] = mapped_column(String(7), nullable=False)

    etag: Mapped[str | None] = mapped_column(String(128))
    last_modified: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    game_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # True once this month can never change again, i.e. any month before the
    # current one. The current month is the only one ever re-fetched.
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    player: Mapped[Player] = relationship(back_populates="archives")

    __table_args__ = (
        UniqueConstraint("player_id", "month", name="uq_archives_player_month"),
        CheckConstraint("month ~ '^[0-9]{4}-[0-9]{2}$'", name="ck_archives_month_format"),
    )


# --- games ------------------------------------------------------------------


class Game(Base, TimestampMixin):
    """One finished game, from the tracked player's point of view."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Half of the idempotency key; the other half is player_id (see
    # uq_games_player_uuid below). Ingestion upserts on the pair, so
    # re-fetching a month - or crashing halfway through one - cannot duplicate
    # games.
    chesscom_uuid: Mapped[str] = mapped_column(String(64), nullable=False)

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str | None] = mapped_column(Text)

    color: Mapped[str] = mapped_column(String(5), nullable=False)
    result: Mapped[str] = mapped_column(String(5), nullable=False)

    # Chess.com's raw reason ('resigned', 'timeout', 'checkmated', 'repetition',
    # 'insufficient', ...). Kept alongside the normalised result because "how do
    # I lose" is a report feature: losing on time is a different problem from
    # being checkmated.
    termination: Mapped[str | None] = mapped_column(String(32))

    time_class: Mapped[str] = mapped_column(String(8), nullable=False)
    # Exact control, e.g. '180+2' or '1/86400' for daily.
    time_control: Mapped[str] = mapped_column(String(16), nullable=False)
    rated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 'chess', 'chess960', 'bughouse', ... Stockfish can only evaluate 'chess',
    # so variants are stored and marked skipped rather than dropped - dropping
    # them would make our game count disagree with Chess.com's and look like an
    # ingestion bug.
    rules: Mapped[str] = mapped_column(String(16), nullable=False, default="chess")

    # From the PGN headers, not the JSON `eco` field, which is a URL.
    opening_eco: Mapped[str | None] = mapped_column(String(8))
    opening_name: Mapped[str | None] = mapped_column(Text)

    pgn: Mapped[str] = mapped_column(Text, nullable=False)

    # Set only for games with PGN SetUp/FEN headers (a non-standard starting
    # position). NULL means the normal start, so replay begins there.
    start_fen: Mapped[str | None] = mapped_column(String(128))

    played_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    player_rating: Mapped[int | None] = mapped_column(Integer)
    opponent_rating: Mapped[int | None] = mapped_column(Integer)
    opponent_username: Mapped[str | None] = mapped_column(String(64))

    # Chess.com's own accuracy numbers, when present. Not used for reporting -
    # they are kept as a cross-check that our own Stockfish pipeline produces
    # sane results.
    accuracy_player: Mapped[float | None] = mapped_column(Float)
    accuracy_opponent: Mapped[float | None] = mapped_column(Float)

    ply_count: Mapped[int | None] = mapped_column(SmallInteger)

    analysis_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    analysis_skip_reason: Mapped[str | None] = mapped_column(Text)
    analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    player: Mapped[Player] = relationship(back_populates="games")
    moves: Mapped[list[Move]] = relationship(back_populates="game", cascade="all, delete-orphan")

    __table_args__ = (
        # Unique per player, not globally. A row is a game *from one player's
        # point of view* - its color, result and ratings are all relative - so
        # when two tracked players meet, that one Chess.com game is two rows.
        # With chesscom_uuid unique on its own, the second player's sync would
        # collide with the first's and the game would silently be missing from
        # their history. See INTERVIEW_PREP.md.
        UniqueConstraint("player_id", "chesscom_uuid", name="uq_games_player_uuid"),
        _check("color", COLORS, "ck_games_color"),
        _check("result", RESULTS, "ck_games_result"),
        _check("time_class", TIME_CLASSES, "ck_games_time_class"),
        _check("analysis_status", ANALYSIS_STATUSES, "ck_games_analysis_status"),
        # Every report query filters or groups by time class, because a 15%
        # blunder rate means something completely different in bullet than in
        # rapid. Reports are never mixed across time controls.
        Index("ix_games_player_class_played", "player_id", "time_class", "played_at"),
        # Finding work to do: only pending rows are of interest, so the index
        # covers only those and stays small as completed games accumulate.
        Index(
            "ix_games_pending_analysis",
            "player_id",
            postgresql_where=(analysis_status == "pending"),
        ),
    )


# --- moves ------------------------------------------------------------------


class Move(Base):
    """One ply of one game, with the engine's verdict on it."""

    __tablename__ = "moves"

    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    # Half-move number from 1. Composite PK with game_id, so re-analysing a game
    # upserts its moves instead of inserting a second copy.
    ply: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    # Links this ply to a `positions` row. Stored as the hash rather than a
    # foreign key because a position's evaluation is keyed on engine version and
    # depth too, so the target row depends on which analysis run is being read.
    fen_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    san: Mapped[str] = mapped_column(String(8), nullable=False)
    uci: Mapped[str] = mapped_column(String(6), nullable=False)

    # Clock reading after the move, from the PGN's {[%clk ...]} comment.
    clock_seconds: Mapped[float | None] = mapped_column(Float)
    # Seconds spent on this move, derived from consecutive clock readings and the
    # increment. This is what the time-pressure report actually uses.
    move_time_seconds: Mapped[float | None] = mapped_column(Float)

    # Evaluation before and after the move, in centipawns from the mover's point
    # of view. NULL when the position is a forced mate, in which case the
    # mate_* columns carry the distance. Raw values are stored so classification
    # thresholds can change without re-running the engine.
    eval_cp_before: Mapped[int | None] = mapped_column(Integer)
    eval_cp_after: Mapped[int | None] = mapped_column(Integer)
    mate_in_before: Mapped[int | None] = mapped_column(SmallInteger)
    mate_in_after: Mapped[int | None] = mapped_column(SmallInteger)

    classification: Mapped[str | None] = mapped_column(String(12))
    phase: Mapped[str | None] = mapped_column(String(12))

    # Whether this ply was played by the tracked player. Opponent moves are
    # evaluated anyway - one engine call per position yields both the "after" of
    # one ply and the "before" of the next - but only the player's own moves
    # count towards their blunder rate.
    is_player_move: Mapped[bool] = mapped_column(Boolean, nullable=False)

    game: Mapped[Game] = relationship(back_populates="moves")

    __table_args__ = (
        _check("classification", CLASSIFICATIONS, "ck_moves_classification"),
        _check("phase", PHASES, "ck_moves_phase"),
        Index("ix_moves_classification", "classification"),
    )


# --- positions --------------------------------------------------------------


class Position(Base):
    """A unique position and one engine's verdict on it, stored once and reused.

    The primary key is (fen_hash, engine_version, depth), not fen_hash alone.
    An evaluation is the output of one engine at one search depth, not a
    property of the position: with a bare position key, a shallow eval written
    during a quick benchmark would be served forever afterwards as if it were a
    full-depth result, silently and with no error anywhere.
    """

    __tablename__ = "positions"

    # SHA-256 of the normalised FEN. 32 raw bytes rather than 64 hex characters
    # keeps this index - the hottest in the system - half the size.
    fen_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    engine_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    depth: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    # The normalised FEN this hash was taken of, kept for debugging and so a
    # position can be replayed without the originating game.
    fen: Mapped[str] = mapped_column(String(128), nullable=False)

    best_move: Mapped[str | None] = mapped_column(String(6))

    # Exactly one of eval_cp / mate_in is set. A forced mate is not a centipawn
    # value, and encoding it as one (say 30000) would corrupt every average.
    eval_cp: Mapped[int | None] = mapped_column(Integer)
    mate_in: Mapped[int | None] = mapped_column(SmallInteger)

    nodes: Mapped[int | None] = mapped_column(BigInteger)
    # Engine wall-clock for this position, which is what the cache saves. Summed,
    # it gives the "engine seconds saved" metric.
    compute_ms: Mapped[int | None] = mapped_column(Integer)

    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(eval_cp IS NOT NULL AND mate_in IS NULL) "
            "OR (eval_cp IS NULL AND mate_in IS NOT NULL)",
            name="ck_positions_eval_xor_mate",
        ),
        CheckConstraint("depth > 0", name="ck_positions_depth_positive"),
    )


# --- jobs -------------------------------------------------------------------


class Job(Base, TimestampMixin):
    """One unit of background work.

    Postgres is the queue. Workers claim rows with
    `SELECT ... FOR UPDATE SKIP LOCKED`, which hands each row to exactly one
    worker and lets the others walk past locked rows instead of blocking.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Lower runs first. Rapid and blitz are analysed before bullet: bullet
    # histories are large and low-information, and a new user's first report
    # should not wait behind thousands of one-minute games.
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)

    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)

    # Not before this time. Retry backoff is implemented by pushing this
    # forward, which keeps the whole retry policy in one column.
    run_after: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Makes enqueueing idempotent as well as execution: 'analyze-game:<games.id>'
    # can be enqueued twice without creating two jobs. Keyed on games.id, not the
    # Chess.com uuid: since migration 0002 one uuid can be two games rows (one
    # per tracked player), and each needs its own analysis.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    __table_args__ = (
        _check("status", JOB_STATUSES, "ck_jobs_status"),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        # The claim query's index. Partial, because the queue is only ever asked
        # for runnable work: completed jobs stay in the table for inspection but
        # must not grow the index that every worker hits in a loop.
        Index(
            "ix_jobs_claimable",
            "priority",
            "run_after",
            postgresql_where=(status == "pending"),
        ),
        # For the reaper, which frees jobs whose worker died holding them.
        Index(
            "ix_jobs_running_locked_at",
            "locked_at",
            postgresql_where=(status == "running"),
        ),
    )
