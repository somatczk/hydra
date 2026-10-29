"""The DB-backed model registry: immutable versions over the ``ml_models`` table.

One row per model version; register assigns the next version per name and
verifies the artifact's sha256, promote stages a version as Production and
archives the previous Production (recording the demotion time), and rollback
re-promotes the most recently archived Production of the same name — a rollback
never leaves the name without a Production. The pool is injected: ``hydra.ml``
never owns one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hydra.core.logging import get_logger

logger = get_logger(__name__)

_REGISTER_SQL = """
    INSERT INTO ml_models (id, name, version, model_type, metrics, feature_spec,
                           artifact_file, artifact_sha256, trained_at, created_by,
                           training_job_id)
    SELECT $1::uuid, $2, COALESCE(MAX(version), 0) + 1, $3, $4::jsonb, $5::jsonb,
           $6, $7, $8, $9, $10
    FROM ml_models WHERE name = $2
    RETURNING id, name, version, stage, model_type, metrics, feature_spec, drift,
              artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
              archived_at
"""

_LIST_ALL_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models ORDER BY name, version
"""

_LIST_NAME_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE name = $1 ORDER BY version
"""

_GET_LATEST_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE name = $1 ORDER BY version DESC LIMIT 1
"""

_GET_VERSION_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE name = $1 AND version = $2
"""

_GET_BY_ID_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE id = $1
"""

_GET_BY_TRAINING_JOB_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE training_job_id = $1
"""

_DISCARD_TRAINING_JOB_SQL = """
    DELETE FROM ml_models WHERE training_job_id = $1 AND stage = 'staging'
    RETURNING id, name, version, stage, model_type, metrics, feature_spec, drift,
              artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
              archived_at
"""

TRAINING_JOB_UNIQUE_INDEX = "ml_models_training_job_id_key"
"""The 0013 partial unique index: one registered version per training job."""

_GET_PRODUCTION_SQL = """
    SELECT id, name, version, stage, model_type, metrics, feature_spec, drift,
           artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
           archived_at
    FROM ml_models WHERE name = $1 AND stage = 'production'
"""

_DEMOTE_OTHER_PRODUCTION_SQL = """
    UPDATE ml_models SET stage = 'archived', archived_at = now()
    WHERE name = $1 AND stage = 'production' AND version <> $2
    RETURNING version
"""

_PROMOTE_SQL = """
    UPDATE ml_models SET stage = 'production', archived_at = NULL
    WHERE name = $1 AND version = $2
    RETURNING id, name, version, stage, model_type, metrics, feature_spec, drift,
              artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
              archived_at
"""

_RESTORE_LATEST_ARCHIVED_SQL = """
    UPDATE ml_models SET stage = 'production', archived_at = NULL
    WHERE id = (SELECT id FROM ml_models WHERE name = $1 AND stage = 'archived'
                ORDER BY archived_at DESC, version DESC LIMIT 1)
    RETURNING id, name, version, stage, model_type, metrics, feature_spec, drift,
              artifact_file, artifact_sha256, trained_at, created_by, training_job_id,
              archived_at
"""

_ARCHIVE_CURRENT_PRODUCTION_SQL = """
    UPDATE ml_models SET stage = 'archived', archived_at = now()
    WHERE name = $1 AND stage = 'production' AND version <> $2
"""


class ModelRegistryError(Exception):
    """A registry refusal: unknown model, artifact hash mismatch or a duplicate version."""


@dataclass(frozen=True)
class ModelVersion:
    """One immutable model version; only the stage lifecycle field is mutable."""

    id: uuid.UUID
    name: str
    version: int
    stage: str
    model_type: str
    metrics: dict[str, Any]
    feature_spec: dict[str, Any]
    drift: dict[str, Any] | None
    artifact_file: str
    artifact_sha256: str
    trained_at: datetime
    created_by: str
    training_job_id: str | None
    archived_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "version": self.version,
            "stage": self.stage,
            "model_type": self.model_type,
            "metrics": self.metrics,
            "feature_spec": self.feature_spec,
            "drift": self.drift,
            "artifact_file": self.artifact_file,
            "artifact_sha256": self.artifact_sha256,
            "trained_at": self.trained_at.isoformat(),
            "created_by": self.created_by,
            "training_job_id": self.training_job_id,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }


@dataclass(frozen=True)
class ModelRegistration:
    """One new version: what was trained, where its artifact is, who trained it."""

    name: str
    model_type: str
    metrics: dict[str, Any]
    feature_spec: dict[str, Any]
    artifact_path: Path
    artifact_sha256: str
    trained_at: datetime
    created_by: str
    training_job_id: str | None = None


def _json_value(value: Any) -> Any:
    """asyncpg returns jsonb columns as JSON text; dicts and records pass through."""
    return json.loads(value) if isinstance(value, str) else value


def _row_to_version(row: Any) -> ModelVersion:
    return ModelVersion(
        id=row["id"],
        name=row["name"],
        version=int(row["version"]),
        stage=row["stage"],
        model_type=row["model_type"],
        metrics=_json_value(row["metrics"]),
        feature_spec=_json_value(row["feature_spec"]),
        drift=_json_value(row["drift"]) if row["drift"] is not None else None,
        artifact_file=row["artifact_file"],
        artifact_sha256=row["artifact_sha256"],
        trained_at=row["trained_at"],
        created_by=row["created_by"],
        training_job_id=row["training_job_id"],
        archived_at=row["archived_at"],
    )


def _sha256_file(path: Path) -> str:
    """The artifact's hex sha256 — the hash the registry pins and parity checks trust."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        msg = f"cannot read model artifact {path}: {exc!r}"
        raise ModelRegistryError(msg) from exc
    return digest.hexdigest()


class ModelRegistry:
    """Registry over ``ml_models``; the pool is injected (ml never owns one)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def register(self, entry: ModelRegistration) -> ModelVersion:
        """Insert the next immutable version of ``name``; refuse an artifact hash mismatch.

        The version is assigned atomically (max + 1 per name); a concurrent
        duplicate loses on the ``UNIQUE (name, version)`` constraint. The one
        idempotent case is a training job's re-run: a register whose
        ``training_job_id`` already has a version (the 0013 partial unique
        index) returns that committed version instead of raising, so the
        requeue story holds — one training job, one registered version.
        """
        actual = _sha256_file(entry.artifact_path)
        if actual != entry.artifact_sha256:
            msg = (
                f"artifact sha256 mismatch for {entry.artifact_path.name}: "
                f"expected {entry.artifact_sha256}, file hashes {actual}"
            )
            raise ModelRegistryError(msg)

        import asyncpg

        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    _REGISTER_SQL,
                    uuid.uuid4(),
                    entry.name,
                    entry.model_type,
                    json.dumps(entry.metrics),
                    json.dumps(entry.feature_spec),
                    entry.artifact_path.name,
                    entry.artifact_sha256,
                    entry.trained_at,
                    entry.created_by,
                    entry.training_job_id,
                )
            except asyncpg.UniqueViolationError as exc:
                if (
                    entry.training_job_id is not None
                    and exc.constraint_name == TRAINING_JOB_UNIQUE_INDEX
                ):
                    existing = await conn.fetchrow(_GET_BY_TRAINING_JOB_SQL, entry.training_job_id)
                    if existing is not None:
                        version = _row_to_version(existing)
                        logger.info(
                            "ml model already registered for this training job; "
                            "serving the committed version",
                            name=version.name,
                            version=version.version,
                            training_job_id=entry.training_job_id,
                        )
                        return version
                msg = f"model '{entry.name}' already has the next version; retry the register"
                raise ModelRegistryError(msg) from exc
        if row is None:
            msg = "register produced no row"
            raise ModelRegistryError(msg)
        version = _row_to_version(row)
        logger.info(
            "ml model registered",
            name=version.name,
            version=version.version,
            artifact=version.artifact_file,
            created_by=version.created_by,
        )
        return version

    async def list(self, name: str | None = None) -> list[ModelVersion]:
        """Every version, optionally of one name, in name-then-version order."""
        async with self._pool.acquire() as conn:
            if name is None:
                rows = await conn.fetch(_LIST_ALL_SQL)
            else:
                rows = await conn.fetch(_LIST_NAME_SQL, name)
        return [_row_to_version(r) for r in rows]

    async def get(self, name: str, version: int | None = None) -> ModelVersion | None:
        """One version, or the latest registered version of the name; None when absent."""
        async with self._pool.acquire() as conn:
            if version is None:
                row = await conn.fetchrow(_GET_LATEST_SQL, name)
            else:
                row = await conn.fetchrow(_GET_VERSION_SQL, name, version)
        return None if row is None else _row_to_version(row)

    async def get_by_id(self, model_id: uuid.UUID) -> ModelVersion | None:
        """One version by its registry id — the identity the HTTP surface addresses."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_GET_BY_ID_SQL, model_id)
        return None if row is None else _row_to_version(row)

    async def get_by_training_job(self, training_job_id: str) -> ModelVersion | None:
        """The version a training job registered; None when it registered none."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_GET_BY_TRAINING_JOB_SQL, training_job_id)
        return None if row is None else _row_to_version(row)

    async def discard_training_job_version(self, training_job_id: str) -> ModelVersion | None:
        """Drop the job's half-claimed Staging version; the deleted row, or None.

        A cancelled or timed-out job leaves no lingering Staging version. A
        version a human already promoted or archived is not the job's to
        discard, so it stays.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_DISCARD_TRAINING_JOB_SQL, training_job_id)
        if row is None:
            return None
        version = _row_to_version(row)
        logger.info(
            "ml model version discarded with its job",
            name=version.name,
            version=version.version,
            training_job_id=training_job_id,
        )
        return version

    async def get_production(self, name: str) -> ModelVersion | None:
        """The version currently in Production for the name; None when there is none."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_GET_PRODUCTION_SQL, name)
        return None if row is None else _row_to_version(row)

    async def promote(self, name: str, version: int) -> ModelVersion:
        """Stage the version as Production; the previous Production is archived and recorded.

        The refusal for an unknown version raises inside the transaction, so the
        demote of the current Production rolls back with it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            demoted = await conn.fetchrow(_DEMOTE_OTHER_PRODUCTION_SQL, name, version)
            row = await conn.fetchrow(_PROMOTE_SQL, name, version)
            if row is None:
                msg = f"model '{name}' has no version {version}"
                raise ModelRegistryError(msg)
        if demoted is not None:
            logger.info(
                "ml model demoted from production",
                name=name,
                version=demoted["version"],
                promoted_version=version,
            )
        return _row_to_version(row)

    async def rollback(self, name: str) -> ModelVersion:
        """Re-promote the most recently archived Production of the name.

        The restore candidate is picked before the current Production is
        archived, so a rollback never re-promotes the version it just demoted.
        Refuses (leaving Production untouched) when no archived version exists.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            restored = await conn.fetchrow(_RESTORE_LATEST_ARCHIVED_SQL, name)
            if restored is None:
                msg = f"model '{name}' has no archived version to roll back to"
                raise ModelRegistryError(msg)
            await conn.execute(
                _ARCHIVE_CURRENT_PRODUCTION_SQL,
                name,
                restored["version"],
            )
        logger.info(
            "ml model rolled back",
            name=name,
            restored_version=restored["version"],
        )
        return _row_to_version(restored)
