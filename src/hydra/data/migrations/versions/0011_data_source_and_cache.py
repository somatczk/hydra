"""Bar attribution and the data-cache ledger: ts.ohlcv_1m.source + data_cache_entries.

Revision ID: 0011_data_source_and_cache
Revises: 0010_validated_permissions
Create Date: 2026-10-28

Additive and idempotent. ``source`` separates user-curated bars ('feed': written by the
feeds, manual backfills and warm-up fills) from auto-fetched ones ('auto'): every
pre-existing row takes the default 'feed', so the ~4.8M prod rows become structurally
un-deletable (the sweeper's predicate is ``source = 'auto'``) without rewriting them --
the ADD COLUMN is catalog-only on PG16. ``data_cache_entries`` is the ledger of cached
datasets (one row per exchange x symbol x timeframe) the sweeper and the inventory
surfaces read; upsert_closed_bars keeps it in the same transaction as the bars.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_data_source_and_cache"
down_revision: str | None = "0010_validated_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ts.ohlcv_1m ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'feed'"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.data_cache_entries (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            first_ts TIMESTAMPTZ NOT NULL,
            last_ts TIMESTAMPTZ NOT NULL,
            rows BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (exchange, symbol, timeframe)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.data_cache_entries")
    op.execute("ALTER TABLE ts.ohlcv_1m DROP COLUMN IF EXISTS source")
