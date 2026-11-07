"""Give jobs.status, priority, attempts and max_attempts database defaults

They had Python-side defaults only, which apply to rows inserted through the
ORM and nothing else. A job enqueued by plain SQL - a benchmark harness, a
psql session, a future INSERT ... SELECT - had to name every one of them or
fail on NOT NULL. The queue is the one table other tools are expected to
write to directly, so its defaults belong in the database.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULTS = {
    "status": sa.text("'pending'"),
    "priority": sa.text("100"),
    "attempts": sa.text("0"),
    "max_attempts": sa.text("5"),
}


def upgrade() -> None:
    for column, default in _DEFAULTS.items():
        op.alter_column("jobs", column, server_default=default)


def downgrade() -> None:
    for column in _DEFAULTS:
        op.alter_column("jobs", column, server_default=None)
