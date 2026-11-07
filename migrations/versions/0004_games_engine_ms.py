"""Record the engine time each game's analysis cost, independent of the cache

Engine time was only recorded in `positions.compute_ms`, which is written by
the cache. With the cache off there was nowhere it landed, so "engine seconds
with cache vs without" could not be computed from the database: the "without"
side had no numbers. `games.engine_ms` is written by the worker when a game
completes - the sum over positions it actually sent to the engine - whatever
cache is or is not in front of it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("games", sa.Column("engine_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "engine_ms")
