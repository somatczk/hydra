"""The model registry operations the REST routes and the MCP tools share.

Every version is addressed by its registry UUID and every filesystem path is
server-composed: a stored ``artifact_file`` is validated before it is joined
onto the models directory, and no request value composes a path. Retrain queues
an ``ml_train`` job whose version lands in Staging. Promote and rollback swap the
Production version that every ML consumer attaches when it starts, recovers or
pins its overlay — paper and live alike — so promotion stays a human action: the
REST routes are the signed-in operator's, and the MCP surface reaches them only
through the two code-gated executors below (a human types the one-time code).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hydra.backtest import jobs
from hydra.backtest.jobs import MlTrainRunRequest
from hydra.core.logging import get_logger
from hydra.core.types import Timeframe
from hydra.ml.registry import ModelRegistry, ModelRegistryError, ModelVersion
from hydra.services import universe
from hydra.services.backtest import cancel_job
from hydra.services.errors import ServiceError, db_errors, require_pool, service_error
from hydra.services.strategies import find_strategy_file, parse_strategy_file
from hydra.strategy.config import StrategyConfig

audit_logger = get_logger("hydra.audit")

MODEL_ACTIONS = ("promote_model", "rollback_model")
"""The two gated approval actions; their executors are the MCP surface's only stage writes."""

_SAFE_ARTIFACT_FILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.onnx$")
_DRIFT_STATUSES: frozenset[str] = frozenset({"low", "moderate", "high"})
STAGE_LABELS = {"staging": "Staging", "production": "Production", "archived": "Archived"}

DriftStatus = Literal["unknown", "low", "moderate", "high"]


class MlRetrainRequest(BaseModel):
    """What a retrain trains on; the model name comes from the path."""

    strategy_id: str
    symbol: str
    timeframe: Timeframe | None = None
    horizon: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_HORIZON)
    window_1h: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_1H)
    window_4h: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_4H)
    window_1d: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_1D)


def model_not_found(model_id: str) -> ServiceError:
    return service_error(404, "model_not_found", f"Model {model_id} not found", "not_found")


def validated_name(name: str) -> str:
    """A registry name: plain identifier characters only, never a path component."""
    if not jobs.SAFE_MODEL_NAME.match(name) or ".." in name:
        raise service_error(400, "invalid_model_name", "Invalid model name", "invalid")
    return name


def model_uuid(model_id: str) -> uuid.UUID:
    """The only address a version answers to: its registry UUID."""
    try:
        return uuid.UUID(model_id)
    except ValueError as exc:
        raise service_error(400, "invalid_model_id", "Invalid model ID", "invalid") from exc


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def artifact_size(version: ModelVersion, models_dir: Path) -> int:
    """The artifact's byte size when it is present on this node's models mount; else 0."""
    name = version.artifact_file
    if not _SAFE_ARTIFACT_FILE.match(name) or ".." in name:
        return 0
    try:
        return (models_dir / name).stat().st_size
    except OSError:
        return 0


def drift_fields(version: ModelVersion) -> tuple[float | None, DriftStatus]:
    """A drift label is only trusted when a measured drift value backs it."""
    drift = version.drift
    if not isinstance(drift, dict) or drift.get("max_psi") is None:
        return None, "unknown"
    status = drift.get("status", drift.get("drift_status"))
    label: DriftStatus = status if status in _DRIFT_STATUSES else "unknown"
    try:
        return float(drift["max_psi"]), label
    except (TypeError, ValueError):
        return None, "unknown"


def version_response(version: ModelVersion, models_dir: Path) -> dict[str, Any]:
    drift, drift_status = drift_fields(version)
    return {
        "id": str(version.id),
        "name": version.name,
        "version": version.version,
        "stage": stage_label(version.stage),
        "model_type": version.model_type,
        "metrics": version.metrics,
        "feature_spec": version.feature_spec,
        "drift": drift,
        "drift_status": drift_status,
        "artifact_file": version.artifact_file,
        "artifact_sha256": version.artifact_sha256,
        "trained_at": version.trained_at.isoformat(),
        "created_by": version.created_by,
        "training_job_id": version.training_job_id,
        "archived_at": version.archived_at.isoformat() if version.archived_at else None,
        "file_size": artifact_size(version, models_dir),
        "file_name": version.artifact_file,
    }


def validation_text(exc: ValidationError) -> str:
    """The pydantic errors as one flat message for the shared envelope."""
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'request'}: {error['msg']}"
        for error in exc.errors()
    )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


async def list_models(pool: Any, models_dir: Path) -> list[dict[str, Any]]:
    """Every registered model version, in name-then-version order."""
    registry = ModelRegistry(require_pool(pool))
    async with db_errors("listing ml models"):
        versions = await registry.list()
    return [version_response(v, models_dir) for v in versions]


async def find_model(pool: Any, model_id: str) -> ModelVersion:
    """One version by its registry id: 503 no pool, 400 not a UUID, 404 unknown."""
    registry = ModelRegistry(require_pool(pool))
    async with db_errors("reading an ml model"):
        version = await registry.get_by_id(model_uuid(model_id))
    if version is None:
        raise model_not_found(model_id)
    return version


async def get_model(pool: Any, models_dir: Path, model_id: str) -> dict[str, Any]:
    """One model version, addressed by its registry id."""
    return version_response(await find_model(pool, model_id), models_dir)


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


async def promote_model(pool: Any, model_id: str) -> dict[str, Any]:
    """Promote a version to Production; the previous Production is archived and recorded."""
    registry = ModelRegistry(require_pool(pool))
    model = model_uuid(model_id)
    try:
        async with db_errors("promoting an ml model"):
            version = await registry.get_by_id(model)
            if version is None:
                raise model_not_found(model_id)
            promoted = await registry.promote(version.name, version.version)
    except ModelRegistryError as exc:
        raise service_error(409, "promote_refused", str(exc), "invalid") from exc
    return {
        "id": str(promoted.id),
        "name": promoted.name,
        "version": promoted.version,
        "stage": stage_label(promoted.stage),
        "message": f"Model '{promoted.name}' v{promoted.version} promoted to Production",
    }


async def rollback_model(pool: Any, model_id: str) -> dict[str, Any]:
    """Roll the model back to its most recently archived Production version."""
    registry = ModelRegistry(require_pool(pool))
    model = model_uuid(model_id)
    try:
        async with db_errors("rolling back an ml model"):
            version = await registry.get_by_id(model)
            if version is None:
                raise model_not_found(model_id)
            restored = await registry.rollback(version.name)
    except ModelRegistryError as exc:
        raise service_error(409, "rollback_refused", str(exc), "invalid") from exc
    return {
        "id": str(restored.id),
        "name": restored.name,
        "restored_version": restored.version,
        "stage": stage_label(restored.stage),
        "message": f"Model '{restored.name}' rolled back to v{restored.version}",
    }


async def promote_params(pool: Any, model_id: str, reason: str) -> dict[str, Any]:
    """The gated promote's approval params: the version, resolved and named for the human."""
    version = await find_model(pool, model_id)
    return {
        "model_id": str(version.id),
        "name": version.name,
        "version": version.version,
        "reason": reason,
    }


async def rollback_params(pool: Any, model_id: str, reason: str) -> dict[str, Any]:
    """The gated rollback's approval params, pinning the Production version it replaces.

    Refuses up front (409, like the registry would at execution) when the name has
    no archived version, so no code is issued for a rollback that cannot run.
    """
    version = await find_model(pool, model_id)
    async with db_errors("reading an ml model"):
        versions = await ModelRegistry(pool).list(version.name)
    if not any(v.stage == "archived" for v in versions):
        raise service_error(
            409,
            "rollback_refused",
            f"model '{version.name}' has no archived version to roll back to",
            "invalid",
        )
    return {
        "model_id": str(version.id),
        "name": version.name,
        "production_version": next((v.version for v in versions if v.stage == "production"), None),
        "reason": reason,
    }


async def execute_promote(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """The ``promote_model`` executor: runs only after a human typed the code.

    Re-resolves the version by id at execution time; promoting the version that is
    already Production is a clean no-op (the registry's own idempotence).
    """
    result = await promote_model(getattr(deps, "db_pool", None), str(params.get("model_id") or ""))
    audit_logger.info(
        "audit",
        action="model_promoted",
        actor=actor,
        model=result["id"],
        name=result["name"],
        version=result["version"],
        reason=params.get("reason"),
    )
    return result


async def execute_rollback(deps: Any, params: dict[str, Any], actor: str) -> dict[str, Any]:
    """The ``rollback_model`` executor: runs only after a human typed the code.

    The approval pinned the Production version it replaces; a promote or rollback
    that landed in between makes "the most recently archived version" a different
    target than the one approved, so the executor refuses 409 and changes nothing.
    """
    pool = require_pool(getattr(deps, "db_pool", None))
    name = str(params.get("name") or "")
    async with db_errors("reading an ml model"):
        current = await ModelRegistry(pool).get_production(name)
    pinned = params.get("production_version")
    now = None if current is None else current.version
    if now != pinned:
        raise service_error(
            409,
            "model_state_changed",
            f"The Production version of '{name}' changed since the rollback was requested "
            f"(v{pinned} then, v{now} now); nothing was rolled back, request it again",
            "invalid",
            requested_production_version=pinned,
            production_version=now,
        )
    result = await rollback_model(pool, str(params.get("model_id") or ""))
    audit_logger.info(
        "audit",
        action="model_rolled_back",
        actor=actor,
        model=result["id"],
        name=result["name"],
        restored_version=result["restored_version"],
        reason=params.get("reason"),
    )
    return result


# ---------------------------------------------------------------------------
# Retrain: the ml_train job
# ---------------------------------------------------------------------------


def strategy_config(strategy_id: str) -> StrategyConfig:
    """Resolve a strategy YAML to its config: 404 unknown, 422 unusable."""
    path = find_strategy_file(strategy_id)
    if path is None:
        raise service_error(
            404, "strategy_not_found", f"Strategy {strategy_id} not found", "not_found"
        )
    data = parse_strategy_file(path)
    if data is None:
        raise service_error(
            422, "invalid_strategy", f"Strategy {strategy_id}: not a YAML mapping", "invalid"
        )
    try:
        return StrategyConfig(**data)
    except (ValidationError, TypeError) as exc:
        raise service_error(
            422, "invalid_strategy", f"Strategy {strategy_id}: invalid config: {exc}", "invalid"
        ) from exc


async def submit_retrain(pool: Any, name: str, body: MlRetrainRequest) -> dict[str, Any]:
    """Queue an ml_train job: a challenger version of ``name`` trained on stored bars.

    The symbol must be a member of the current top-100 universe; the strategy
    must exist. The job runs bounded (window/horizon caps, the fetch budget,
    the queue's 6 h timeout) and registers its version in Staging — promotion
    stays a human action.
    """
    model_name = validated_name(name)
    config = strategy_config(body.strategy_id)
    try:
        job_request = MlTrainRunRequest(name=model_name, **body.model_dump(exclude_none=True))
    except ValidationError as exc:
        raise service_error(422, "invalid_train_request", validation_text(exc), "invalid") from exc
    pool = require_pool(pool)
    await universe.validate_symbol(pool, body.symbol)
    async with db_errors("queueing the ml_train job"):
        task_id = await jobs.enqueue(
            pool,
            "ml_train",
            job_request.model_dump(mode="json"),
            config.model_dump(mode="json"),
            {},
        )
    return {"task_id": task_id, "status": "queued"}


async def retrain_status(pool: Any, task_id: str) -> dict[str, Any]:
    """The progress of a queued, running or finished ml_train job."""
    pool = require_pool(pool)
    async with db_errors("reading an ml_train job"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM backtest_jobs WHERE id = $1 AND kind = 'ml_train'", task_id
        )
    if row is None:
        raise service_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
    detail = jobs.json_value(row["detail"]) or {}
    result = jobs.json_value(row["result"]) if row["result"] is not None else None
    return {
        "task_id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "error": row["error"],
        "result": result,
        "fetch": detail.get("fetch"),
        "provenance": detail.get("provenance"),
        "metrics": detail.get("metrics"),
    }


async def cancel_retrain(pool: Any, task_id: str) -> dict[str, Any]:
    """Cancel a queued or running retrain; 404 unknown or not an ml_train job, 409 terminal."""
    pool = require_pool(pool)
    async with db_errors("reading the ml_train job"):
        row = await jobs.get(pool, task_id, kind="ml_train")
    if row is None:
        raise service_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
    cancelled = await cancel_job(pool, task_id)
    return {"task_id": task_id, "status": cancelled["status"]}
