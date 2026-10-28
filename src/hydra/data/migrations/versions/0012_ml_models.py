"""The ML model registry: the ``ml_models`` table.

Revision ID: 0012_ml_models
Revises: 0011_data_source_and_cache
Create Date: 2026-10-28

Additive and idempotent. One row per immutable model version: ``stage`` is the
only mutable lifecycle field (``staging`` -> ``production`` -> ``archived``;
demotions record their time on ``archived_at`` so a rollback can re-promote the
most recently archived Production), while metrics, ``feature_spec`` (including
the reference quantiles and the horizon), the artifact file + sha256,
``trained_at``, ``created_by`` and ``training_job_id`` are write-once.
``UNIQUE (name, version)`` makes versions immutable; ``training_job_id``
references ``backtest_jobs(id)`` (a TEXT id — the jobs table's PK is TEXT, so a
BIGINT foreign key could not be created). No backfill: the models directory is
upload-only and holds no versions worth registering.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_ml_models"
down_revision: str | None = "0011_data_source_and_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ml_models (
        id UUID PRIMARY KEY,
        name TEXT NOT NULL,
        version INTEGER NOT NULL,
        stage TEXT NOT NULL DEFAULT 'staging'
            CHECK (stage IN ('staging', 'production', 'archived')),
        model_type TEXT NOT NULL,
        metrics JSONB NOT NULL DEFAULT '{}',
        feature_spec JSONB NOT NULL,
        drift JSONB,
        artifact_file TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        trained_at TIMESTAMPTZ NOT NULL,
        created_by TEXT NOT NULL,
        training_job_id TEXT REFERENCES backtest_jobs(id),
        archived_at TIMESTAMPTZ,
        UNIQUE (name, version)
    )
    """,
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ml_models")
