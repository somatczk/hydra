from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/api/models", tags=["models"])


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
# Placeholder data
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
    },
}


def _get_model(model_id: str) -> dict[str, Any]:
    if model_id not in _MODELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found",
        )
    return _MODELS[model_id]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ModelResponse])
async def list_models() -> list[dict[str, Any]]:
    """List all ML models with name, version, stage, metrics, and drift."""
    return list(_MODELS.values())


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(model_id: str) -> dict[str, Any]:
    """Get detail for a single model."""
    return _get_model(model_id)


@router.post("/{model_id}/promote", response_model=PromoteResponse)
async def promote_model(model_id: str) -> dict[str, Any]:
    """Promote a model to production stage."""
    model = _get_model(model_id)
    previous_stage = model["stage"]
    model["stage"] = "Production"
    return {
        "id": model_id,
        "stage": "Production",
        "message": f"Model promoted from {previous_stage} to Production",
    }


@router.post("/{model_id}/rollback", response_model=RollbackResponse)
async def rollback_model(model_id: str) -> dict[str, Any]:
    """Rollback a model to the previous version."""
    model = _get_model(model_id)
    current_version = model["version"]
    model["stage"] = "Staging"
    return {
        "id": model_id,
        "stage": "Staging",
        "previous_version": current_version,
        "message": f"Model {model_id} rolled back from {current_version}",
    }


@router.post("/retrain", response_model=RetrainResponse)
async def retrain_models() -> dict[str, str]:
    """Trigger retraining of ML models."""
    import uuid

    task_id = f"retrain-{uuid.uuid4().hex[:8]}"
    return {
        "task_id": task_id,
        "message": "Retraining job started",
    }
