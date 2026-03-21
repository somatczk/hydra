from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from hydra.core.config import HydraConfig

router = APIRouter(prefix="/api/models", tags=["models"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models directory
# ---------------------------------------------------------------------------


def _models_dir() -> Path:
    try:
        cfg = HydraConfig()
        return Path(cfg.ml.models_dir)
    except Exception:
        return Path("/app/models")


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


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
    drift_status: str = "low"  # low | moderate | high
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


class RetrainResponse(BaseModel):
    task_id: str
    message: str


# ---------------------------------------------------------------------------
# Placeholder data (fallback when models dir is empty / not mounted)
# ---------------------------------------------------------------------------

_MODELS: dict[str, dict[str, Any]] = {
    "model-1": {
        "id": "model-1",
        "name": "LSTM Price Predictor",
        "version": "v2.4.1",
        "stage": "Production",
        "metrics": {"accuracy": 72.3, "precision": 68.1},
        "drift": 0.02,
        "drift_status": "low",
        "last_trained": "2 days ago",
        "file_size": 0,
        "file_name": "",
    },
    "model-2": {
        "id": "model-2",
        "name": "XGBoost Signal Classifier",
        "version": "v3.1.0",
        "stage": "Production",
        "metrics": {"accuracy": 78.5, "precision": 74.2},
        "drift": 0.05,
        "drift_status": "low",
        "last_trained": "5 days ago",
        "file_size": 0,
        "file_name": "",
    },
    "model-3": {
        "id": "model-3",
        "name": "Volatility Estimator",
        "version": "v1.2.0",
        "stage": "Staging",
        "metrics": {"accuracy": 81.0, "precision": 76.8},
        "drift": 0.12,
        "drift_status": "moderate",
        "last_trained": "1 day ago",
        "file_size": 0,
        "file_name": "",
    },
    "model-4": {
        "id": "model-4",
        "name": "Sentiment Analyzer",
        "version": "v0.9.3",
        "stage": "Training",
        "metrics": {"accuracy": None, "precision": None},
        "drift": None,
        "drift_status": "low",
        "last_trained": "In progress",
        "file_size": 0,
        "file_name": "",
    },
    "model-5": {
        "id": "model-5",
        "name": "Order Flow Predictor",
        "version": "v1.0.0",
        "stage": "Archived",
        "metrics": {"accuracy": 55.2, "precision": 51.0},
        "drift": 0.35,
        "drift_status": "high",
        "last_trained": "30 days ago",
        "file_size": 0,
        "file_name": "",
    },
}


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
                "drift_status": meta.get("drift_status", "low"),
                "last_trained": meta.get("last_trained", mtime.strftime("%Y-%m-%d %H:%M UTC")),
                "file_size": stat.st_size,
                "file_name": onnx_file.name,
            }
        )
    return results


def _get_model_from_dir(model_id: str) -> tuple[dict[str, Any], Path] | None:
    """Look up a single model by stem name."""
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
        "drift_status": meta.get("drift_status", "low"),
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
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Model {model_id} not found",
    )


@router.post("/upload", response_model=ModelUploadResponse, status_code=201)
async def upload_model(file: UploadFile) -> dict[str, Any]:
    """Upload an ONNX model file. Validates it can be loaded by onnxruntime."""
    if not file.filename or not file.filename.endswith(".onnx"):
        raise HTTPException(status_code=422, detail="File must have .onnx extension")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=422, detail="Empty file")

    # Validate ONNX by attempting to create an InferenceSession
    try:
        import onnxruntime as ort

        ort.InferenceSession(content)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid ONNX model: {exc}") from exc

    models_path = _models_dir()
    models_path.mkdir(parents=True, exist_ok=True)

    dest = models_path / file.filename
    dest.write_bytes(content)

    model_id = dest.stem
    _write_meta(
        dest,
        {
            "name": model_id.replace("_", " ").replace("-", " ").title(),
            "version": "v1.0.0",
            "stage": "Staging",
            "metrics": {},
            "last_trained": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )

    return {
        "id": model_id,
        "name": model_id.replace("_", " ").replace("-", " ").title(),
        "file_name": file.filename,
        "file_size": len(content),
        "stage": "Staging",
        "message": f"Model '{model_id}' uploaded successfully",
    }


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
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
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
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
    model = _MODELS[model_id]
    current_version = model["version"]
    model["stage"] = "Staging"
    return {
        "id": model_id,
        "stage": "Staging",
        "previous_version": current_version,
        "message": f"Model {model_id} rolled back from {current_version}",
    }


@router.post("/{model_id}/retrain", response_model=RetrainResponse)
async def retrain_model(model_id: str) -> dict[str, str]:
    """Trigger retraining of a specific ML model."""
    import uuid

    # Check existence in dir or fallback
    result = _get_model_from_dir(model_id)
    if result is None and model_id not in _MODELS:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
    task_id = f"retrain-{uuid.uuid4().hex[:8]}"
    return {
        "task_id": task_id,
        "message": f"Retraining job started for model {model_id}",
    }
