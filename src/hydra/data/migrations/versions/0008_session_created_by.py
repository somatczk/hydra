"""Attribute every trading session to the principal that started it.

Revision ID: 0008_session_created_by
Revises: 0007_session_config_snapshot
Create Date: 2026-10-26

Additive and idempotent. Rows from before this revision have NULL (unknown attribution);
the column is written on every session start ('user:<name>' from the REST routes,
'service:mcp' from the MCP tools) and never changes afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_session_created_by"
down_revision: str | None = "0007_session_config_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE trading_sessions ADD COLUMN IF NOT EXISTS created_by TEXT",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in ("ALTER TABLE trading_sessions DROP COLUMN IF EXISTS created_by",):
        op.execute(statement)
