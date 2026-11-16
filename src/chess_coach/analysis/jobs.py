"""Putting games on the analysis queue.

One job per game, keyed `analyze-game:<games.id>`. Not the Chess.com uuid: a
game between two tracked players is two `games` rows (one per point of view),
and each needs its own moves, so each needs its own job.
"""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from chess_coach.config import get_settings
from chess_coach.logging import get_logger
from chess_coach.models import TIME_CLASSES

log = get_logger(__name__)

JOB_TYPE = "analyze-game"

# Lower runs first. Rapid, blitz and daily before bullet: bullet histories are
# large and low-information, and a new user's first report should not wait
# behind thousands of one-minute games (docs/DESIGN.md).
PRIORITY = {"rapid": 10, "blitz": 10, "daily": 10, "bullet": 20}
assert set(PRIORITY) == set(TIME_CLASSES)

# Built from PRIORITY so the SQL and the Python cannot disagree. The values are
# this module's own constants, not input.
_PRIORITY_SQL = (
    "CASE g.time_class "
    + " ".join(f"WHEN '{tc}' THEN {p}" for tc, p in PRIORITY.items())
    + f" ELSE {max(PRIORITY.values())} END"
)


def dedupe_key(game_id: int) -> str:
    return f"{JOB_TYPE}:{game_id}"


def enqueue_pending_games(
    session: Session, *, max_attempts: int, player_id: int | None = None
) -> int:
    """Enqueue every pending game that has no job yet; return how many were added.

    One set-based statement over `ix_games_pending_analysis`, so it costs the
    same whether it adds nothing or a whole history, and it is safe to run as
    often as you like - which is the point: a crash between "games saved" and
    "jobs enqueued" is repaired by the next run, not lost.
    """
    player_filter = "AND g.player_id = :player_id" if player_id is not None else ""
    result = session.execute(
        text(
            f"""
            INSERT INTO jobs (type, payload, status, priority, attempts, max_attempts,
                              dedupe_key, run_after)
            SELECT :type, jsonb_build_object('game_id', g.id), 'pending', {_PRIORITY_SQL},
                   0, :max_attempts, :key_prefix || g.id, now()
            FROM games g
            WHERE g.analysis_status = 'pending' {player_filter}
            ON CONFLICT (dedupe_key) DO NOTHING
            """
        ),
        # One parameter per use: psycopg binds server-side, and the same $1
        # as a VARCHAR column value and as a || operand is "inconsistent types".
        {
            "type": JOB_TYPE,
            "key_prefix": f"{JOB_TYPE}:",
            "max_attempts": max_attempts,
            "player_id": player_id,
        },
    )
    return result.rowcount


def enqueue_pending(engine: Engine) -> int:
    """`enqueue_pending_games` in its own transaction, for callers outside the
    worker (the sync CLI calls this after syncing)."""
    with Session(engine) as session, session.begin():
        added = enqueue_pending_games(session, max_attempts=get_settings().job_max_attempts)
    log.info("analysis.enqueued", jobs=added)
    return added
