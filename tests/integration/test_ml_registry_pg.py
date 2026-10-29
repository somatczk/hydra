"""The DB-backed model registry on real TimescaleDB (P5-A).

Needs HYDRA_DB_* (see tests/infra.py): every test runs against the real
``ml_models`` table, so the promote/rollback semantics, the immutable
(name, version) uniqueness and the artifact sha256 refusal are proven on
committed rows, never against a mock pool.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from hydra.data.migrations.runner import upgrade_to_head
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.registry import (
    ModelRegistration,
    ModelRegistry,
    ModelRegistryError,
)

pytestmark = pytest.mark.integration

TRAINED_AT = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture()
def artifact(tmp_path: Path) -> Path:
    path = tmp_path / "alpha-v1.onnx"
    path.write_bytes(b"model-bytes")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registration(
    artifact: Path,
    *,
    name: str = "alpha",
    sha256: str | None = None,
    created_by: str = "job:task-abc12345",
    training_job_id: str | None = None,
    trained_at: datetime = TRAINED_AT,
) -> ModelRegistration:
    return ModelRegistration(
        name=name,
        model_type="xgboost",
        metrics={"oos_accuracy": 0.51, "oos_base_rate": 0.5, "folds": 5},
        feature_spec=DEFAULT_FEATURE_SPEC.to_dict(),
        artifact_path=artifact,
        artifact_sha256=sha256 or _sha256(artifact),
        trained_at=trained_at,
        created_by=created_by,
        training_job_id=training_job_id,
    )


async def _register_three(registry: ModelRegistry, artifact: Path) -> None:
    """Three versions of 'alpha', one per minute apart so ordering is observable."""
    for i in range(3):
        path = artifact.parent / f"alpha-v{i + 1}.onnx"
        path.write_bytes(f"model-bytes-{i + 1}".encode())
        await asyncio.sleep(0.01)
        await registry.register(_registration(path))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    async def test_register_assigns_versions_and_stores_everything(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        v1 = await registry.register(_registration(artifact))
        assert (v1.name, v1.version, v1.stage) == ("alpha", 1, "staging")
        assert v1.model_type == "xgboost"
        assert v1.metrics["oos_accuracy"] == 0.51
        assert v1.feature_spec["spec_version"] == DEFAULT_FEATURE_SPEC.spec_version
        assert v1.feature_spec["reference_quantiles"] is None
        assert v1.drift is None
        assert v1.artifact_file == artifact.name
        assert v1.artifact_sha256 == _sha256(artifact)
        assert v1.trained_at == TRAINED_AT
        assert v1.created_by == "job:task-abc12345"
        assert v1.training_job_id is None
        assert v1.archived_at is None

        v2 = await registry.register(_registration(artifact))
        assert v2.version == 2

    async def test_register_with_a_real_training_job_id(self, pool: Any, artifact: Path) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
                "VALUES ($1, 'ml_train', '{}', '{}')",
                "task-abc12345",
            )
        v1 = await ModelRegistry(pool).register(
            _registration(artifact, training_job_id="task-abc12345")
        )
        assert v1.training_job_id == "task-abc12345"

    async def test_a_rerun_registering_the_same_training_job_serves_its_version(
        self, pool: Any, artifact: Path
    ) -> None:
        """One training job, one version: a re-run's register is idempotent.

        The requeue story: a worker that died between the register commit and
        the terminal jobs.finish write re-runs the job; the re-run must serve
        the committed version, never register a second one.
        """
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
                "VALUES ($1, 'ml_train', '{}', '{}')",
                "task-abc12345",
            )
        registry = ModelRegistry(pool)
        v1 = await registry.register(_registration(artifact, training_job_id="task-abc12345"))

        rerun = await registry.register(_registration(artifact, training_job_id="task-abc12345"))

        assert rerun.id == v1.id  # the SAME version, never a second one
        assert rerun.version == 1
        assert [v.version for v in await registry.list("alpha")] == [1]  # count unchanged

    async def test_uploads_without_a_training_job_id_still_register_versions(
        self, pool: Any, artifact: Path
    ) -> None:
        """The upload path is outside the one-version-per-job index: distinct versions."""
        registry = ModelRegistry(pool)
        assert (await registry.register(_registration(artifact))).version == 1
        assert (await registry.register(_registration(artifact))).version == 2
        assert [v.version for v in await registry.list("alpha")] == [1, 2]

    async def test_the_training_job_discard_drops_only_the_staging_version(
        self, pool: Any, artifact: Path
    ) -> None:
        """A cancelled job's half-claimed Staging version goes; a promoted one stays."""
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO backtest_jobs (id, kind, request, strategy_config) "
                "VALUES ($1, 'ml_train', '{}', '{}')",
                "task-abc12345",
            )
        registry = ModelRegistry(pool)
        v1 = await registry.register(_registration(artifact, training_job_id="task-abc12345"))
        assert await registry.discard_training_job_version("task-abc12345") == v1
        assert await registry.list("alpha") == []
        assert await registry.discard_training_job_version("task-abc12345") is None

        # a version a human promoted is not the job's to discard
        v2 = await registry.register(_registration(artifact, training_job_id="task-abc12345"))
        await registry.promote("alpha", v2.version)
        assert await registry.discard_training_job_version("task-abc12345") is None
        assert (await registry.get_production("alpha")) is not None

    async def test_sha256_mismatch_is_refused_and_stores_nothing(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        with pytest.raises(ModelRegistryError, match="sha256 mismatch"):
            await registry.register(_registration(artifact, sha256="0" * 64))
        assert await registry.list("alpha") == []

    async def test_missing_artifact_is_refused(self, pool: Any, tmp_path: Path) -> None:
        missing = tmp_path / "nope.onnx"
        with pytest.raises(ModelRegistryError, match="cannot read model artifact"):
            await ModelRegistry(pool).register(_registration(missing, sha256="0" * 64))
        assert await ModelRegistry(pool).list("alpha") == []


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------


class TestGetList:
    async def test_get_latest_and_by_version(self, pool: Any, artifact: Path) -> None:
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)
        latest = await registry.get("alpha")
        assert latest is not None and latest.version == 3
        v1 = await registry.get("alpha", 1)
        assert v1 is not None and v1.version == 1
        assert await registry.get("alpha", 99) is None
        assert await registry.get("other") is None

    async def test_list_orders_by_name_and_version(self, pool: Any, artifact: Path) -> None:
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)
        other = artifact.parent / "beta-v1.onnx"
        other.write_bytes(b"beta")
        await registry.register(_registration(other, name="beta"))
        names = [(m.name, m.version) for m in await registry.list()]
        assert names == [("alpha", 1), ("alpha", 2), ("alpha", 3), ("beta", 1)]
        assert [m.version for m in await registry.list("alpha")] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


class TestPromoteRollback:
    async def test_promote_demotes_and_records_the_previous_production(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)

        promoted = await registry.promote("alpha", 1)
        assert promoted.stage == "production"
        assert promoted.archived_at is None
        production = await registry.get_production("alpha")
        assert production is not None and production.version == 1

        await registry.promote("alpha", 2)
        v1 = await registry.get("alpha", 1)
        v2 = await registry.get("alpha", 2)
        assert v1 is not None and v1.stage == "archived" and v1.archived_at is not None
        assert v2 is not None and v2.stage == "production" and v2.archived_at is None
        production = await registry.get_production("alpha")
        assert production is not None and production.version == 2

    async def test_promote_unknown_version_is_refused(self, pool: Any, artifact: Path) -> None:
        registry = ModelRegistry(pool)
        await registry.register(_registration(artifact))
        with pytest.raises(ModelRegistryError, match="no version 9"):
            await registry.promote("alpha", 9)
        current = await registry.get_production("alpha")
        assert current is None

    async def test_promote_unknown_version_keeps_the_current_production(
        self, pool: Any, artifact: Path
    ) -> None:
        """The refusal rolls back: a bad promote must not orphan the Production."""
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)
        await registry.promote("alpha", 1)
        await registry.promote("alpha", 2)
        with pytest.raises(ModelRegistryError, match="no version 9"):
            await registry.promote("alpha", 9)
        production = await registry.get_production("alpha")
        assert production is not None and production.version == 2
        v1 = await registry.get("alpha", 1)
        assert v1 is not None and v1.stage == "archived" and v1.archived_at is not None
        assert await registry.get("alpha", 9) is None

    async def test_rollback_repromotes_the_most_recently_archived_production(
        self, pool: Any, artifact: Path
    ) -> None:
        """The restore candidate is the most recently archived, not the highest version."""
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)

        await registry.promote("alpha", 1)
        await asyncio.sleep(0.01)
        await registry.promote("alpha", 2)  # v1 -> archived (1st demotion)
        await asyncio.sleep(0.01)
        await registry.rollback("alpha")  # v2 -> archived (2nd), v1 -> production
        assert (await registry.get_production("alpha")).version == 1

        await asyncio.sleep(0.01)
        await registry.promote("alpha", 3)  # v1 -> archived (3rd, most recent)
        await asyncio.sleep(0.01)
        rolled = await registry.rollback("alpha")
        # v1 was archived most recently, so it wins over v2
        assert rolled.version == 1
        assert (await registry.get_production("alpha")).version == 1
        v3 = await registry.get("alpha", 3)
        assert v3 is not None and v3.stage == "archived"

    async def test_rollback_never_leaves_the_name_without_a_production(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)
        await registry.promote("alpha", 1)
        await registry.promote("alpha", 2)
        await registry.rollback("alpha")
        production = await registry.get_production("alpha")
        assert production is not None and production.version == 1

    async def test_rollback_without_archived_versions_refuses_and_keeps_production(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        await registry.register(_registration(artifact))
        await registry.promote("alpha", 1)
        with pytest.raises(ModelRegistryError, match="no archived version"):
            await registry.rollback("alpha")
        assert (await registry.get_production("alpha")).version == 1

    async def test_rollback_unknown_name_is_refused(self, pool: Any) -> None:
        with pytest.raises(ModelRegistryError, match="no archived version"):
            await ModelRegistry(pool).rollback("nobody")

    async def test_full_lifecycle_leaves_one_production(self, pool: Any, artifact: Path) -> None:
        registry = ModelRegistry(pool)
        await _register_three(registry, artifact)
        for version in (1, 2, 3, 2, 1, 3):
            await registry.promote("alpha", version)
            await asyncio.sleep(0.01)
        production = await registry.get_production("alpha")
        assert production is not None and production.version == 3
        stages = {m.version: m.stage for m in await registry.list("alpha")}
        assert stages == {1: "archived", 2: "archived", 3: "production"}
        # every archived row records when it was demoted
        assert all(
            m.archived_at is not None for m in await registry.list("alpha") if m.stage == "archived"
        )

    async def test_versions_follow_registration_order_not_trained_at(
        self, pool: Any, artifact: Path
    ) -> None:
        registry = ModelRegistry(pool)
        late = _registration(artifact, trained_at=TRAINED_AT + timedelta(days=1))
        early = _registration(artifact, trained_at=TRAINED_AT - timedelta(days=1))
        assert (await registry.register(late)).version == 1
        assert (await registry.register(early)).version == 2
