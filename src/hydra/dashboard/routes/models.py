"""The model registry's HTTP surface: immutable versions over ``ml_models``.

Every artifact is addressed by registry identity — the row's UUID — and every
filesystem path is server-composed: uploads land under generated names, a
training job's ONNX is named for the job id, and stored ``artifact_file`` values
are validated before they are ever joined onto the models directory. No request
value composes a path. The stage lifecycle (staging → production → archived,
with rollback restoring the most recently archived Production) is the registry's
own, and promotion stays a human action.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import Response
from starlette.types import Message

from hydra.backtest import jobs
from hydra.backtest.jobs import MlTrainRunRequest
from hydra.core.types import Timeframe
from hydra.dashboard.deps import current_config
from hydra.dashboard.routes._db import (
    ServiceErrorRoute,
    api_error,
    require_pool,
)
from hydra.dashboard.security import Principal, audit
from hydra.ml.registry import (
    ModelRegistration,
    ModelRegistry,
    ModelRegistryError,
    ModelVersion,
)
from hydra.services import universe
from hydra.services.backtest import cancel_job
from hydra.services.errors import db_errors
from hydra.services.strategies import find_strategy_file, parse_strategy_file
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"], route_class=ServiceErrorRoute)

_SAFE_ARTIFACT_FILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.onnx$")
_DRIFT_STATUSES: frozenset[str] = frozenset({"low", "moderate", "high"})
_STAGE_LABELS = {"staging": "Staging", "production": "Production", "archived": "Archived"}


def _model_not_found(model_id: str) -> HTTPException:
    return api_error(404, "model_not_found", f"Model {model_id} not found", "not_found")


def _validated_name(name: str) -> str:
    """A registry name: plain identifier characters only, never a path component."""
    if not jobs.SAFE_MODEL_NAME.match(name) or ".." in name:
        raise api_error(400, "invalid_model_name", "Invalid model name", "invalid")
    return name


def _registry(request: Request) -> ModelRegistry:
    return ModelRegistry(require_pool(request))


def _models_dir() -> Path:
    return Path(current_config().ml.models_dir)


def _artifact_size(version: ModelVersion) -> int:
    """The artifact's byte size when it is present on this node's models mount; else 0."""
    name = version.artifact_file
    if not _SAFE_ARTIFACT_FILE.match(name) or ".." in name:
        return 0
    try:
        return (_models_dir() / name).stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

DriftStatus = Literal["unknown", "low", "moderate", "high"]


class ModelVersionResponse(BaseModel):
    id: str
    name: str
    version: int
    stage: str  # Staging | Production | Archived
    model_type: str
    metrics: dict[str, Any]
    feature_spec: dict[str, Any]
    drift: float | None = None
    drift_status: DriftStatus = "unknown"
    artifact_file: str
    artifact_sha256: str
    trained_at: str
    created_by: str
    training_job_id: str | None = None
    archived_at: str | None = None
    file_size: int = 0
    file_name: str = ""


class ModelUploadResponse(BaseModel):
    id: str
    name: str
    version: int
    file_name: str
    file_size: int
    stage: str
    message: str


class PromoteResponse(BaseModel):
    id: str
    name: str
    version: int
    stage: str
    message: str


class RollbackResponse(BaseModel):
    id: str
    name: str
    restored_version: int
    stage: str
    message: str


class RetrainResponse(BaseModel):
    task_id: str
    status: str = "queued"


class RetrainStatusResponse(BaseModel):
    task_id: str
    status: str  # queued | running | completed | failed | cancelled
    progress: float = 0.0
    error: str | None = None
    result: dict[str, Any] | None = None
    fetch: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None


class MlRetrainRequest(BaseModel):
    """What a retrain trains on; the model name comes from the path."""

    strategy_id: str
    symbol: str
    timeframe: Timeframe | None = None
    horizon: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_HORIZON)
    window_1h: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_1H)
    window_4h: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_4H)
    window_1d: int | None = Field(default=None, ge=1, le=jobs.MAX_TRAIN_WINDOW_1D)


def _drift_fields(version: ModelVersion) -> tuple[float | None, DriftStatus]:
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


def _version_response(version: ModelVersion) -> dict[str, Any]:
    drift, drift_status = _drift_fields(version)
    return {
        "id": str(version.id),
        "name": version.name,
        "version": version.version,
        "stage": _STAGE_LABELS.get(version.stage, version.stage),
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
        "file_size": _artifact_size(version),
        "file_name": version.artifact_file,
    }


def _model_id_or_400(model_id: str) -> uuid.UUID:
    """The only address a version answers to: its registry UUID."""
    try:
        return uuid.UUID(model_id)
    except ValueError as exc:
        raise api_error(400, "invalid_model_id", "Invalid model ID", "invalid") from exc


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ModelVersionResponse])
async def list_models(request: Request) -> list[dict[str, Any]]:
    """Every registered model version, in name-then-version order."""
    registry = _registry(request)
    async with db_errors("listing ml models"):
        versions = await registry.list()
    return [_version_response(v) for v in versions]


@router.get("/{model_id}", response_model=ModelVersionResponse)
async def get_model(model_id: str, request: Request) -> dict[str, Any]:
    """One model version, addressed by its registry id."""
    registry = _registry(request)
    async with db_errors("reading an ml model"):
        version = await registry.get_by_id(_model_id_or_400(model_id))
    if version is None:
        raise _model_not_found(model_id)
    return _version_response(version)


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


@router.post("/{model_id}/promote", response_model=PromoteResponse)
async def promote_model(model_id: str, request: Request) -> dict[str, Any]:
    """Promote a version to Production; the previous Production is archived and recorded."""
    registry = _registry(request)
    model_uuid = _model_id_or_400(model_id)
    try:
        async with db_errors("promoting an ml model"):
            version = await registry.get_by_id(model_uuid)
            if version is None:
                raise _model_not_found(model_id)
            promoted = await registry.promote(version.name, version.version)
    except ModelRegistryError as exc:
        raise api_error(409, "promote_refused", str(exc), "invalid") from exc
    audit(
        _principal(request),
        "model_promoted",
        model=str(promoted.id),
        name=promoted.name,
        version=promoted.version,
    )
    return {
        "id": str(promoted.id),
        "name": promoted.name,
        "version": promoted.version,
        "stage": _STAGE_LABELS.get(promoted.stage, promoted.stage),
        "message": f"Model '{promoted.name}' v{promoted.version} promoted to Production",
    }


@router.post("/{model_id}/rollback", response_model=RollbackResponse)
async def rollback_model(model_id: str, request: Request) -> dict[str, Any]:
    """Roll the model back to its most recently archived Production version."""
    registry = _registry(request)
    model_uuid = _model_id_or_400(model_id)
    try:
        async with db_errors("rolling back an ml model"):
            version = await registry.get_by_id(model_uuid)
            if version is None:
                raise _model_not_found(model_id)
            restored = await registry.rollback(version.name)
    except ModelRegistryError as exc:
        raise api_error(409, "rollback_refused", str(exc), "invalid") from exc
    audit(
        _principal(request),
        "model_rolled_back",
        model=str(restored.id),
        name=restored.name,
        restored_version=restored.version,
    )
    return {
        "id": str(restored.id),
        "name": restored.name,
        "restored_version": restored.version,
        "stage": _STAGE_LABELS.get(restored.stage, restored.stage),
        "message": f"Model '{restored.name}' rolled back to v{restored.version}",
    }


def _principal(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


def _validation_text(exc: ValidationError) -> str:
    """The pydantic errors as one flat message for the shared envelope."""
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'request'}: {error['msg']}"
        for error in exc.errors()
    )


# ---------------------------------------------------------------------------
# Upload (DB-16)
# ---------------------------------------------------------------------------

MAX_MODEL_BYTES = 200 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024


def _too_large() -> HTTPException:
    return api_error(
        413,
        "model_too_large",
        f"Model files are limited to {MAX_MODEL_BYTES // (1024 * 1024)} MiB",
        "invalid",
        max_bytes=MAX_MODEL_BYTES,
    )


class _BoundedUploadRoute(APIRoute):
    """Counts request body bytes as they arrive; 413 before the multipart parser spools more."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            limit = MAX_MODEL_BYTES + MULTIPART_OVERHEAD_BYTES
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise _too_large()
            receive = request.receive
            received = 0

            async def counting() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _too_large()
                return message

            return await handler(Request(request.scope, counting))

        return bounded


def _check_onnx(path: Path) -> dict[str, str]:
    """Load the file with onnxruntime; 422 when it is not a model it can run.

    Returns the artifact's custom metadata map (the feature spec, when the
    uploader exported it through our own trainer).
    """
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(str(path))
        meta = session.get_modelmeta()
    except Exception as exc:
        logger.info("Rejected model upload: %s", exc)
        raise api_error(
            422, "invalid_onnx", "The file is not a valid ONNX model", "invalid"
        ) from exc
    return dict(meta.custom_metadata_map) if meta is not None else {}


def _copy_capped(src: Any, dest: Path) -> int:
    """Copy the parsed upload to *dest*; 413 (and nothing kept) above MAX_MODEL_BYTES."""
    size = 0
    src.seek(0)
    with dest.open("wb") as out:
        while chunk := src.read(_CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_MODEL_BYTES:
                raise _too_large()
            out.write(chunk)
    return size


async def _spool(file: UploadFile, dest: Path) -> int:
    """Copy the upload to *dest* off the event loop."""
    return await asyncio.to_thread(_copy_capped, file.file, dest)


def _uploaded_spec(metadata: dict[str, str]) -> dict[str, Any]:
    """The feature spec an uploaded artifact carries, when it carries one at all."""
    try:
        if "spec_version" in metadata and "feature_names" in metadata:
            spec: dict[str, Any] = {
                "spec_version": int(metadata["spec_version"]),
                "feature_names": json.loads(metadata["feature_names"]),
            }
            if "horizon" in metadata:
                spec["horizon"] = int(metadata["horizon"])
            return spec
    except (TypeError, ValueError):
        logger.info("Uploaded model carried an unreadable feature spec; registering none")
    return {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def upload_model(
    file: UploadFile,
    request: Request,
    sha256: str | None = None,
) -> dict[str, Any]:
    """Upload an ONNX model as the next Staging version of its name.

    Every upload of a name registers a new immutable version, so the old
    ``replace`` flag has nothing to replace. The artifact is written under a
    server-generated name (never the upload's filename); ``sha256`` optionally
    pins the exact bytes, refused on a mismatch.
    """
    if not file.filename or not file.filename.endswith(".onnx"):
        raise api_error(422, "invalid_file", "File must have .onnx extension", "invalid")
    name = _validated_name(Path(file.filename).name.removesuffix(".onnx"))
    principal = _principal(request)
    pool = require_pool(request)

    models_path = _models_dir()
    models_path.mkdir(parents=True, exist_ok=True)
    tmp = models_path / f".upload-{uuid.uuid4().hex}.tmp"
    try:
        size = await _spool(file, tmp)
        if size == 0:
            raise api_error(422, "invalid_file", "Empty file", "invalid")
        metadata = await asyncio.to_thread(_check_onnx, tmp)
        actual = await asyncio.to_thread(_sha256, tmp)
        if sha256 is not None and sha256.lower() != actual:
            raise api_error(
                422,
                "sha256_mismatch",
                "The uploaded bytes do not hash to the provided sha256",
                "invalid",
                provided_sha256=sha256,
                actual_sha256=actual,
            )
        # the final name is the registry's own: server-generated, addressed by the row
        dest = models_path / f"{uuid.uuid4().hex}.onnx"
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)

    try:
        async with db_errors("registering an uploaded model"):
            version = await ModelRegistry(pool).register(
                ModelRegistration(
                    name=name,
                    model_type="onnx",
                    metrics={},
                    feature_spec=_uploaded_spec(metadata),
                    artifact_path=dest,
                    artifact_sha256=actual,
                    trained_at=datetime.now(UTC),
                    created_by=f"upload:{principal.name if principal else 'anonymous'}",
                )
            )
    except ModelRegistryError as exc:
        dest.unlink(missing_ok=True)
        raise api_error(409, "model_register_refused", str(exc), "invalid") from exc
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    audit(principal, "model_uploaded", model=str(version.id), name=name, size=size)

    return {
        "id": str(version.id),
        "name": version.name,
        "version": version.version,
        "file_name": version.artifact_file,
        "file_size": size,
        "stage": _STAGE_LABELS.get(version.stage, version.stage),
        "message": f"Model '{name}' uploaded as version {version.version}",
    }


router.add_api_route(
    "/upload",
    upload_model,
    methods=["POST"],
    response_model=ModelUploadResponse,
    status_code=201,
    route_class_override=_BoundedUploadRoute,
)


# ---------------------------------------------------------------------------
# Retrain: the ml_train job (the 501 is gone)
# ---------------------------------------------------------------------------


def _strategy_config(strategy_id: str) -> StrategyConfig:
    """Resolve a strategy YAML to its config: 404 unknown, 422 unusable."""
    path = find_strategy_file(strategy_id)
    if path is None:
        raise api_error(404, "strategy_not_found", f"Strategy {strategy_id} not found", "not_found")
    data = parse_strategy_file(path)
    if data is None:
        raise api_error(
            422, "invalid_strategy", f"Strategy {strategy_id}: not a YAML mapping", "invalid"
        )
    try:
        return StrategyConfig(**data)
    except (ValidationError, TypeError) as exc:
        raise api_error(
            422, "invalid_strategy", f"Strategy {strategy_id}: invalid config: {exc}", "invalid"
        ) from exc


@router.post("/{name}/retrain", response_model=RetrainResponse, status_code=202)
async def retrain_model(name: str, body: MlRetrainRequest, request: Request) -> dict[str, Any]:
    """Queue an ml_train job: a challenger version of ``name`` trained on stored bars.

    The symbol must be a member of the current top-100 universe; the strategy
    must exist. The job runs bounded (window/horizon caps, the fetch budget,
    the queue's 6 h timeout) and registers its version in Staging — promotion
    stays a human action.
    """
    model_name = _validated_name(name)
    config = _strategy_config(body.strategy_id)
    try:
        job_request = MlTrainRunRequest(name=model_name, **body.model_dump(exclude_none=True))
    except ValidationError as exc:
        raise api_error(422, "invalid_train_request", _validation_text(exc), "invalid") from exc
    pool = require_pool(request)
    await universe.validate_symbol(pool, body.symbol)
    async with db_errors("queueing the ml_train job"):
        task_id = await jobs.enqueue(
            pool,
            "ml_train",
            job_request.model_dump(mode="json"),
            config.model_dump(mode="json"),
            {},
        )
    audit(
        _principal(request),
        "model_retrain_submitted",
        name=model_name,
        task_id=task_id,
        symbol=body.symbol,
        strategy_id=body.strategy_id,
    )
    return {"task_id": task_id, "status": "queued"}


@router.get("/retrain/{task_id}", response_model=RetrainStatusResponse)
async def get_retrain_status(task_id: str, request: Request) -> dict[str, Any]:
    """The progress of a queued, running or finished ml_train job."""
    pool = require_pool(request)
    async with db_errors("reading an ml_train job"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM backtest_jobs WHERE id = $1 AND kind = 'ml_train'", task_id
        )
    if row is None:
        raise api_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
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


@router.post("/retrain/{task_id}/cancel", response_model=RetrainResponse)
async def cancel_retrain(task_id: str, request: Request) -> dict[str, Any]:
    """Cancel a queued or running retrain; 404 unknown, 409 already terminal."""
    row = await cancel_job(require_pool(request), task_id)
    audit(_principal(request), "model_retrain_cancelled", task_id=task_id, status=row["status"])
    return {"task_id": task_id, "status": row["status"]}
