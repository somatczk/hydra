"""When stored exchange credentials last passed the read-only key check.

Revision ID: 0004_credentials_validated_at
Revises: 0003_risk_state
Create Date: 2026-10-20

Additive and idempotent. Rows stored before this revision were never checked, so they stay NULL
("never validated") until the keys are connected again.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_credentials_validated_at"
down_revision: str | None = "0003_risk_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE exchange_credentials ADD COLUMN IF NOT EXISTS validated_at TIMESTAMPTZ",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from a pg_dump instead")
