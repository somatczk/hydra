"""Consolidated strategy routes: list, detail, update, toggle, delete,
builder indicators/comparators, preview, and save.

Merges the old ``strategy_builder.py`` and ``builder.py`` endpoints into
a single ``/api/strategies/*`` namespace.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile

from hydra.dashboard.routes._db import ServiceErrorRoute, request_pool, request_trader
from hydra.services import strategies as svc
from hydra.services.strategies import (
    PreviewRequest,
    PreviewResponse,
    ReloadResponse,
    SaveRequest,
    SaveResponse,
    StrategyDetailResponse,
    StrategyPerformance,
    StrategyResponse,
    ToggleResponse,
)

router = APIRouter(prefix="/api/strategies", tags=["strategies"], route_class=ServiceErrorRoute)


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(request: Request) -> list[dict[str, Any]]:
    """List all strategy configs from YAML files, merged with DB performance."""
    return await svc.list_strategies(request_pool(request))


@router.get("/indicators")
async def list_indicators() -> list[dict[str, Any]]:
    """List all available indicators with their parameter schemas."""
    from hydra.strategy.indicator_registry import get_all_indicators

    indicators = get_all_indicators()
    return [
        {
            "name": ind.name,
            "category": ind.category,
            "description": ind.description,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "default": p.default,
                    "min": p.min,
                    "max": p.max,
                }
                for p in ind.params
            ],
        }
        for ind in indicators
    ]


@router.get("/comparators")
async def list_comparators() -> list[dict[str, str]]:
    """List all available comparators with descriptions."""
    from hydra.strategy.condition_schema import Comparator

    return [
        {
            "value": Comparator.LESS_THAN.value,
            "label": "Less Than",
            "description": "Current indicator value is less than the target value",
        },
        {
            "value": Comparator.GREATER_THAN.value,
            "label": "Greater Than",
            "description": "Current indicator value is greater than the target value",
        },
        {
            "value": Comparator.CROSSES_ABOVE.value,
            "label": "Crosses Above",
            "description": "Indicator crosses from below to above the target value",
        },
        {
            "value": Comparator.CROSSES_BELOW.value,
            "label": "Crosses Below",
            "description": "Indicator crosses from above to below the target value",
        },
        {
            "value": Comparator.EQUALS.value,
            "label": "Equals",
            "description": "Current indicator value equals the target value",
        },
    ]


@router.get("/{strategy_id}", response_model=StrategyDetailResponse)
async def get_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get a single strategy detail with structured builder fields."""
    return await svc.get_strategy(request_pool(request), strategy_id)


@router.put("/{strategy_id}", response_model=SaveResponse)
async def update_strategy(strategy_id: str, body: SaveRequest) -> SaveResponse:
    """Update a rule-based strategy config (structured payload -> YAML)."""
    return svc.update_strategy(strategy_id, body)


@router.get("/{strategy_id}/performance", response_model=StrategyPerformance)
async def get_strategy_performance(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get performance metrics for a strategy."""
    return await svc.get_strategy_performance(request_pool(request), strategy_id)


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: str) -> Response:
    """Delete a saved strategy."""
    svc.delete_strategy(strategy_id)
    return Response(status_code=204)


@router.post("/{strategy_id}/toggle", response_model=ToggleResponse)
async def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Toggle enabled flag of a strategy."""
    return svc.toggle_strategy(strategy_id)


@router.post("/{strategy_id}/reload", response_model=ReloadResponse)
async def reload_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    """Apply the file's changed ``parameters`` to the running session at its next closed bar.

    Changes to rules, risk, sizing, symbols, timeframes or class return 409 (restart required).
    """
    return await request_trader(request).reload_strategy(strategy_id)


@router.post("/preview", response_model=PreviewResponse)
async def preview_signals(body: PreviewRequest, request: Request) -> PreviewResponse:
    """Run a quick backtest on the latest stored bars and return signals and metrics.

    ``metrics.win_rate`` is a fraction (0-1), unlike the percentage in backtest results.
    """
    return await svc.preview_signals(request_pool(request), body)


@router.post("/save", response_model=SaveResponse, status_code=201)
async def save_strategy(request: SaveRequest) -> SaveResponse:
    """Validate and save a strategy as a YAML config file."""
    return svc.save_strategy(request)


@router.get("/{strategy_id}/export")
async def export_strategy(strategy_id: str) -> Response:
    """Export a strategy as a downloadable YAML file."""
    path = svc.find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
    content = path.read_text()
    return Response(
        content=content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@router.post("/import", response_model=SaveResponse, status_code=201)
async def import_strategy(file: UploadFile) -> SaveResponse:
    """Import a strategy from an uploaded YAML file."""
    svc.check_import_filename(file.filename)
    return svc.import_strategy(file.filename, await file.read())
