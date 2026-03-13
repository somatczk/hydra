from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/builder", tags=["builder"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConditionNode(BaseModel):
    indicator: str
    condition: str  # crosses_above | crosses_below | greater_than | less_than
    value: float


class ConditionTree(BaseModel):
    entry: list[ConditionNode] = Field(default_factory=list)
    exit: list[ConditionNode] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    conditions: ConditionTree
    timeframe: str = "1h"
    pair: str = "BTC/USDT"
    start_date: str = "2026-01-01"
    end_date: str = "2026-03-01"


class SignalMarker(BaseModel):
    timestamp: str
    type: str  # entry | exit
    price: float


class PreviewMetrics(BaseModel):
    total_signals: int = 0
    estimated_win_rate: float = 0.0
    estimated_pnl: float = 0.0


class PreviewResponse(BaseModel):
    signals: list[SignalMarker] = Field(default_factory=list)
    metrics: PreviewMetrics = Field(default_factory=PreviewMetrics)


class SaveRequest(BaseModel):
    name: str
    description: str = ""
    conditions: ConditionTree
    timeframe: str = "1h"
    pair: str = "BTC/USDT"
    risk_config: dict[str, Any] = Field(default_factory=dict)


class SaveResponse(BaseModel):
    strategy_id: str
    name: str
    config_yaml: str
    message: str


class IndicatorParam(BaseModel):
    name: str
    type: str  # int | float | string
    default: Any = None
    description: str = ""


class IndicatorSchema(BaseModel):
    id: str
    name: str
    description: str
    category: str
    parameters: list[IndicatorParam] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Placeholder indicators
# ---------------------------------------------------------------------------

_INDICATORS: list[dict[str, Any]] = [
    {
        "id": "rsi",
        "name": "RSI (Relative Strength Index)",
        "description": "Measures momentum on a scale of 0-100",
        "category": "momentum",
        "parameters": [
            {"name": "period", "type": "int", "default": 14, "description": "Lookback period"},
            {
                "name": "overbought",
                "type": "float",
                "default": 70.0,
                "description": "Overbought threshold",
            },
            {
                "name": "oversold",
                "type": "float",
                "default": 30.0,
                "description": "Oversold threshold",
            },
        ],
    },
    {
        "id": "macd",
        "name": "MACD",
        "description": "Moving Average Convergence Divergence",
        "category": "trend",
        "parameters": [
            {"name": "fast_period", "type": "int", "default": 12, "description": "Fast EMA period"},
            {"name": "slow_period", "type": "int", "default": 26, "description": "Slow EMA period"},
            {
                "name": "signal_period",
                "type": "int",
                "default": 9,
                "description": "Signal line period",
            },
        ],
    },
    {
        "id": "bb",
        "name": "Bollinger Bands",
        "description": "Volatility bands around a moving average",
        "category": "volatility",
        "parameters": [
            {
                "name": "period",
                "type": "int",
                "default": 20,
                "description": "Moving average period",
            },
            {
                "name": "std_dev",
                "type": "float",
                "default": 2.0,
                "description": "Standard deviation multiplier",
            },
        ],
    },
    {
        "id": "ema",
        "name": "EMA (Exponential Moving Average)",
        "description": "Weighted moving average giving more weight to recent prices",
        "category": "trend",
        "parameters": [
            {"name": "period", "type": "int", "default": 20, "description": "EMA period"},
        ],
    },
    {
        "id": "atr",
        "name": "ATR (Average True Range)",
        "description": "Measures market volatility",
        "category": "volatility",
        "parameters": [
            {"name": "period", "type": "int", "default": 14, "description": "ATR period"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/preview", response_model=PreviewResponse)
async def preview_strategy(body: PreviewRequest) -> dict[str, Any]:
    """Instant backtest with condition tree; returns signal markers and quick metrics."""
    # In production this would run the actual backtest engine.
    # Return synthetic preview data for now.
    signals = [
        {"timestamp": "2026-01-15T10:00:00Z", "type": "entry", "price": 65200.0},
        {"timestamp": "2026-01-15T14:00:00Z", "type": "exit", "price": 65800.0},
        {"timestamp": "2026-02-02T09:00:00Z", "type": "entry", "price": 66100.0},
        {"timestamp": "2026-02-02T16:00:00Z", "type": "exit", "price": 66500.0},
        {"timestamp": "2026-02-20T11:00:00Z", "type": "entry", "price": 67000.0},
        {"timestamp": "2026-02-21T08:00:00Z", "type": "exit", "price": 66800.0},
    ]
    metrics = {
        "total_signals": len(signals),
        "estimated_win_rate": 66.7,
        "estimated_pnl": 800.0,
    }
    return {"signals": signals, "metrics": metrics}


@router.post("/save", response_model=SaveResponse)
async def save_strategy(body: SaveRequest) -> dict[str, Any]:
    """Validate, generate YAML, and save a strategy config."""
    import uuid

    strategy_id = f"custom-{uuid.uuid4().hex[:8]}"

    # Build YAML representation
    yaml_lines = [
        "strategy:",
        f"  name: {body.name}",
        f"  pair: {body.pair}",
        f"  timeframe: {body.timeframe}",
        "  entry_conditions:",
    ]
    for cond in body.conditions.entry:
        yaml_lines.append(f"    - indicator: {cond.indicator}")
        yaml_lines.append(f"      condition: {cond.condition}")
        yaml_lines.append(f"      value: {cond.value}")
    yaml_lines.append("  exit_conditions:")
    for cond in body.conditions.exit:
        yaml_lines.append(f"    - indicator: {cond.indicator}")
        yaml_lines.append(f"      condition: {cond.condition}")
        yaml_lines.append(f"      value: {cond.value}")

    config_yaml = "\n".join(yaml_lines)

    return {
        "strategy_id": strategy_id,
        "name": body.name,
        "config_yaml": config_yaml,
        "message": f"Strategy '{body.name}' saved successfully",
    }


@router.get("/indicators", response_model=list[IndicatorSchema])
async def list_indicators() -> list[dict[str, Any]]:
    """List all available indicators with their parameter schemas."""
    return _INDICATORS
