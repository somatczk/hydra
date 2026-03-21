"""Strategy template library: curated, importable strategy configurations."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/templates", tags=["templates"])
logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[4] / "config" / "templates"
_STRATEGIES_DIR = Path(__file__).resolve().parents[4] / "config" / "strategies"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TemplateMeta(BaseModel):
    id: str
    name: str
    description: str
    type: str  # "rule_based" | "dca" | "grid"
    risk_level: str  # "conservative" | "moderate" | "aggressive"
    expected_sharpe: float | None = None
    expected_max_drawdown: float | None = None
    recommended_capital: float | None = None
    symbols: list[str] = ["BTCUSDT"]
    timeframe: str = "1h"


class ImportResponse(BaseModel):
    id: str
    name: str
    config_path: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scan_templates() -> list[dict[str, Any]]:
    """Scan the templates directory for YAML configs with _template_meta."""
    if not _TEMPLATES_DIR.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        try:
            with path.open() as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("_template_meta", {})
        results.append(
            {
                "id": path.stem,
                "name": data.get("name", path.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "rule_based"),
                "risk_level": meta.get("risk_level", "moderate"),
                "expected_sharpe": meta.get("expected_sharpe"),
                "expected_max_drawdown": meta.get("expected_max_drawdown"),
                "recommended_capital": meta.get("recommended_capital"),
                "symbols": data.get("symbols", ["BTCUSDT"]),
                "timeframe": data.get("timeframes", {}).get("primary", "1h"),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TemplateMeta])
async def list_templates() -> list[dict[str, Any]]:
    """List all available strategy templates."""
    return _scan_templates()


@router.get("/{template_id}", response_model=TemplateMeta)
async def get_template(template_id: str) -> dict[str, Any]:
    """Get metadata for a single template."""
    for t in _scan_templates():
        if t["id"] == template_id:
            return t
    raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")


@router.post("/{template_id}/import", response_model=ImportResponse, status_code=201)
async def import_template(template_id: str) -> dict[str, Any]:
    """Import a template into the user's strategies directory."""
    template_path = _TEMPLATES_DIR / f"{template_id}.yaml"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")

    try:
        with template_path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Invalid template format")

    # Remove template metadata, generate unique ID
    data.pop("_template_meta", None)
    suffix = uuid.uuid4().hex[:6]
    strategy_id = f"{template_id}_{suffix}"
    data["id"] = strategy_id
    data["enabled"] = False

    _STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _STRATEGIES_DIR / f"{strategy_id}.yaml"
    try:
        with dest.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "id": strategy_id,
        "name": data.get("name", template_id),
        "config_path": str(dest),
        "message": f"Template '{template_id}' imported as '{strategy_id}'",
    }
