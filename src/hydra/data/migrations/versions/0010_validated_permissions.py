"""Exchange credential permission record: exchange_credentials.validated_permissions.

Revision ID: 0010_validated_permissions
Revises: 0009_live_orders
Create Date: 2026-10-28

Additive and idempotent. ``validated_permissions`` is the connect-time record of the
Binance ``apiRestrictions`` read (mainnet connects only): ``{enable_spot_and_margin_trading,
enable_withdrawals, ip_restrict}``. NULL means the keys were checked before P3-C or on
testnet — not live-capable — and is the flag source for the credential-delete gate. The
live-arming executor re-fetches the restrictions live at arm time (R8); this column is
the stored snapshot, never the arming truth.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_validated_permissions"
down_revision: str | None = "0009_live_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE exchange_credentials ADD COLUMN IF NOT EXISTS validated_permissions JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE exchange_credentials DROP COLUMN IF EXISTS validated_permissions")
