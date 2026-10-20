from __future__ import annotations

import asyncio
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
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import Message

from hydra.dashboard.deps import current_config
from hydra.dashboard.routes._db import api_error
from hydra.dashboard.security import Principal, audit

router = APIRouter(prefix="/api/models", tags=["models"])
logger = logging.getLogger(__name__)

# Allowed characters for model IDs to prevent path traversal
_SAFE_MODEL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _model_not_found(model_id: str) -> HTTPException:
    return api_error(404, "model_not_found", f"Model {model_id} not found", "not_found")


def _validate_model_id(model_id: str) -> str:
    """Validate model_id contains no path traversal characters."""
    if not _SAFE_MODEL_ID.match(model_id) or ".." in model_id:
        raise api_error(400, "invalid_model_id", "Invalid model ID", "invalid")
    return model_id


# ---------------------------------------------------------------------------
# Models directory
# ---------------------------------------------------------------------------


def _models_dir() -> Path:
    return Path(current_config().ml.models_dir)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


DriftStatus = Literal["unknown", "low", "moderate", "high"]
_DRIFT_STATUSES: frozenset[str] = frozenset({"low", "moderate", "high"})


class ModelMetrics(BaseModel):
    accuracy: float | None = None
    precision: float | None = None


class ModelResponse(BaseModel):
    id: str
    name: str
    version: str
    stage: str  # Production | Staging | Training | Archived
    metrics: ModelMetrics
    drift: float | None = None
    drift_status: DriftStatus = "unknown"
    last_trained: str = ""
    file_size: int = 0
    file_name: str = ""


class ModelUploadResponse(BaseModel):
    id: str
    name: str
    file_name: str
    file_size: int
    stage: str
    message: str


class PromoteResponse(BaseModel):
    id: str
    stage: str
    message: str


class RollbackResponse(BaseModel):
    id: str
    stage: str
    previous_version: str
    message: str


# ---------------------------------------------------------------------------
# Placeholder data (fallback when models dir is empty / not mounted)
# ---------------------------------------------------------------------------

_MODELS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Sidecar metadata helpers
# ---------------------------------------------------------------------------


def _read_meta(onnx_path: Path) -> dict[str, Any]:
    """Read the .meta.json sidecar for an ONNX file."""
    meta_path = onnx_path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _drift_status(meta: dict[str, Any]) -> str:
    """A drift label is only trusted when a measured drift value backs it."""
    if meta.get("drift") is None:
        return "unknown"
    status = meta.get("drift_status")
    return status if status in _DRIFT_STATUSES else "unknown"


def _write_meta(onnx_path: Path, meta: dict[str, Any]) -> None:
    """Write the .meta.json sidecar for an ONNX file."""
    meta_path = onnx_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------


def _scan_models_dir() -> list[dict[str, Any]]:
    """Scan the models directory for .onnx files and their sidecars."""
    models_path = _models_dir()
    if not models_path.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for onnx_file in sorted(models_path.glob("*.onnx")):
        model_id = onnx_file.stem
        meta = _read_meta(onnx_file)
        stat = onnx_file.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

        metrics_data = meta.get("metrics", {})
        results.append(
            {
                "id": model_id,
                "name": meta.get("name", model_id.replace("_", " ").replace("-", " ").title()),
                "version": meta.get("version", "v1.0.0"),
                "stage": meta.get("stage", "Staging"),
                "metrics": {
                    "accuracy": metrics_data.get("accuracy"),
                    "precision": metrics_data.get("precision"),
                },
                "drift": meta.get("drift"),
                "drift_status": _drift_status(meta),
                "last_trained": meta.get("last_trained", mtime.strftime("%Y-%m-%d %H:%M UTC")),
                "file_size": stat.st_size,
                "file_name": onnx_file.name,
            }
        )
    return results


def _get_model_from_dir(model_id: str) -> tuple[dict[str, Any], Path] | None:
    """Look up a single model by stem name."""
    _validate_model_id(model_id)
    models_path = _models_dir()
    onnx_file = models_path / f"{model_id}.onnx"
    if not onnx_file.exists():
        return None
    meta = _read_meta(onnx_file)
    stat = onnx_file.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    metrics_data = meta.get("metrics", {})
    return {
        "id": model_id,
        "name": meta.get("name", model_id.replace("_", " ").replace("-", " ").title()),
        "version": meta.get("version", "v1.0.0"),
        "stage": meta.get("stage", "Staging"),
        "metrics": {
            "accuracy": metrics_data.get("accuracy"),
            "precision": metrics_data.get("precision"),
        },
        "drift": meta.get("drift"),
        "drift_status": _drift_status(meta),
        "last_trained": meta.get("last_trained", mtime.strftime("%Y-%m-%d %H:%M UTC")),
        "file_size": stat.st_size,
        "file_name": onnx_file.name,
    }, onnx_file


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ModelResponse])
async def list_models() -> list[dict[str, Any]]:
    """List all ML models — scans ONNX dir, falls back to placeholder data."""
    scanned = _scan_models_dir()
    if scanned:
        return scanned
    return list(_MODELS.values())


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(model_id: str) -> dict[str, Any]:
    """Get detail for a single model."""
    result = _get_model_from_dir(model_id)
    if result is not None:
        return result[0]
    # Fallback to placeholder
    if model_id in _MODELS:
        return _MODELS[model_id]
    raise _model_not_found(model_id)


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


def _check_onnx(path: Path) -> None:
    """Load the file with onnxruntime; 422 when it is not a model it can run."""
    try:
        import onnxruntime as ort

        ort.InferenceSession(str(path))
    except Exception as exc:
        logger.info("Rejected model upload: %s", exc)
        raise api_error(
            422, "invalid_onnx", "The file is not a valid ONNX model", "invalid"
        ) from exc


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


def _guard_destination(dest: Path, model_id: str, replace: bool) -> None:
    """409 when *dest* is a Production model, or exists and ``replace`` is false."""
    if not dest.exists():
        return
    if _read_meta(dest).get("stage") == "Production":
        raise api_error(
            409,
            "model_in_production",
            f"Model '{model_id}' is in Production; demote or roll it back before replacing it",
            "invalid",
        )
    if not replace:
        raise api_error(
            409,
            "model_exists",
            f"Model '{model_id}' already exists; upload again with replace=true to replace it",
            "invalid",
        )


async def upload_model(file: UploadFile, request: Request, replace: bool = False) -> dict[str, Any]:
    """Upload an ONNX model as Staging; an existing model is replaced only with ``replace=true``.

    A Production model is never replaced by an upload: demote or roll it back first.
    """
    if not file.filename or not file.filename.endswith(".onnx"):
        raise api_error(422, "invalid_file", "File must have .onnx extension", "invalid")

    # Sanitize filename — use only the basename to prevent path traversal
    safe_filename = Path(file.filename).name
    model_id = _validate_model_id(safe_filename.removesuffix(".onnx"))

    models_path = _models_dir()
    dest = models_path / safe_filename
    _guard_destination(dest, model_id, replace)

    models_path.mkdir(parents=True, exist_ok=True)
    tmp = models_path / f".{safe_filename}.{uuid.uuid4().hex}.upload"
    name = model_id.replace("_", " ").replace("-", " ").title()
    try:
        size = await _spool(file, tmp)
        if size == 0:
            raise api_error(422, "invalid_file", "Empty file", "invalid")
        await asyncio.to_thread(_check_onnx, tmp)
        # no await from here to the sidecar write: a promote cannot interleave
        _guard_destination(dest, model_id, replace)
        os.replace(tmp, dest)
        _write_meta(
            dest,
            {
                "name": name,
                "version": "v1.0.0",
                "stage": "Staging",
                "metrics": {},
                "last_trained": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        )
    finally:
        tmp.unlink(missing_ok=True)

    principal = getattr(request.state, "principal", None)
    audit(
        principal if isinstance(principal, Principal) else None,
        "model_uploaded",
        model_id=model_id,
        size=size,
        replaced=replace,
    )

    return {
        "id": model_id,
        "name": name,
        "file_name": file.filename,
        "file_size": size,
        "stage": "Staging",
        "message": f"Model '{model_id}' uploaded",
    }


router.add_api_route(
    "/upload",
    upload_model,
    methods=["POST"],
    response_model=ModelUploadResponse,
    status_code=201,
    route_class_override=_BoundedUploadRoute,
)


@router.post("/{model_id}/promote", response_model=PromoteResponse)
async def promote_model(model_id: str) -> dict[str, Any]:
    """Promote a model to production stage."""
    result = _get_model_from_dir(model_id)
    if result is not None:
        model_data, onnx_path = result
        previous_stage = model_data["stage"]
        # Demote any other Production model to Archived
        for other in _models_dir().glob("*.onnx"):
            if other.stem != model_id:
                other_meta = _read_meta(other)
                if other_meta.get("stage") == "Production":
                    other_meta["stage"] = "Archived"
                    _write_meta(other, other_meta)
        # Promote this model
        meta = _read_meta(onnx_path)
        meta["stage"] = "Production"
        _write_meta(onnx_path, meta)
        return {
            "id": model_id,
            "stage": "Production",
            "message": f"Model promoted from {previous_stage} to Production",
        }
    # Fallback to placeholder
    if model_id not in _MODELS:
        raise _model_not_found(model_id)
    model = _MODELS[model_id]
    previous_stage = model["stage"]
    model["stage"] = "Production"
    return {
        "id": model_id,
        "stage": "Production",
        "message": f"Model promoted from {previous_stage} to Production",
    }


@router.post("/{model_id}/rollback", response_model=RollbackResponse)
async def rollback_model(model_id: str) -> dict[str, Any]:
    """Rollback a model to staging."""
    result = _get_model_from_dir(model_id)
    if result is not None:
        model_data, onnx_path = result
        current_version = model_data["version"]
        meta = _read_meta(onnx_path)
        meta["stage"] = "Staging"
        _write_meta(onnx_path, meta)
        return {
            "id": model_id,
            "stage": "Staging",
            "previous_version": current_version,
            "message": f"Model {model_id} rolled back from {current_version}",
        }
    # Fallback to placeholder
    if model_id not in _MODELS:
        raise _model_not_found(model_id)
    model = _MODELS[model_id]
    current_version = model["version"]
    model["stage"] = "Staging"
    return {
        "id": model_id,
        "stage": "Staging",
        "previous_version": current_version,
        "message": f"Model {model_id} rolled back from {current_version}",
    }


@router.post("/{model_id}/retrain", status_code=501)
async def retrain_model(model_id: str) -> None:
    """Retraining is not implemented; no job is started."""
    raise api_error(
        501,
        "not_implemented",
        "Model retraining is not implemented",
        "not_implemented",
        model_id=model_id,
    )
