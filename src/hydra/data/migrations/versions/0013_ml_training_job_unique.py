"""The idempotent-rerun backstop: one registered version per training job.

Revision ID: 0013_ml_training_job_unique
Revises: 0012_ml_models
Create Date: 2026-10-29

Additive and idempotent. ``ml_train`` is the one job kind whose result is a
committed registry row written before the terminal ``backtest_jobs`` update,
so a worker that dies in between (a crash, a SIGTERM, a stale-heartbeat
restart) leaves the row ``running``; the requeue re-runs the job. This partial
UNIQUE index makes the database refuse a second version for the same
``training_job_id`` — the re-run must serve the version it already registered
(ModelRegistry.register fetches it on conflict) instead of minting a new one.
Uploads carry no ``training_job_id`` and stay outside the index. No backfill:
0012 shipped in the same release, so no rows exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_ml_training_job_unique"
down_revision: str | None = "0012_ml_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRAINING_JOB_UNIQUE_INDEX = "ml_models_training_job_id_key"

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ml_models_training_job_id_key
        ON ml_models (training_job_id)
        WHERE training_job_id IS NOT NULL
    """,
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ml_models_training_job_id_key")
