"""Make a game unique per player, not globally

`games.chesscom_uuid` was unique on its own. A row is one game from one tracked
player's point of view, so a game between two tracked players needs two rows;
the global constraint made the second player's copy collide with the first's.
The idempotency key becomes (player_id, chesscom_uuid).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 declared the constraint unnamed, so this is the name Postgres gave it.
    op.drop_constraint("games_chesscom_uuid_key", "games", type_="unique")
    op.create_unique_constraint("uq_games_player_uuid", "games", ["player_id", "chesscom_uuid"])


def downgrade() -> None:
    # Fails, deliberately, if two tracked players share a game: restoring the
    # global constraint would mean deleting one player's copy, and choosing
    # which is not something a downgrade should do silently.
    op.drop_constraint("uq_games_player_uuid", "games", type_="unique")
    op.create_unique_constraint("games_chesscom_uuid_key", "games", ["chesscom_uuid"])
