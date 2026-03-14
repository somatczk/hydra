"""FastAPI routes for the no-code visual strategy builder.

Provides endpoints for listing indicators, previewing signals via quick
backtest, and saving strategy configurations to YAML files.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
    RuleSet,
)
from hydra.strategy.config import ExchangeStrategyConfig, StrategyConfig, TimeframeConfig
from hydra.strategy.indicator_registry import get_all_indicators

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/builder", tags=["builder"])

# Default config directory for strategy YAML files
_CONFIG_DIR = Path(__file__).resolve().parents[4] / "config" / "strategies"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ParamSchema(BaseModel):
    """Schema for a single indicator parameter."""

    name: str
    type: str
    default: int | float | None = None
    min: int | float | None = None
    max: int | float | None = None


class IndicatorSchema(BaseModel):
    """Schema for a single indicator."""

    name: str
    category: str
    description: str
    params: list[ParamSchema]


class ComparatorSchema(BaseModel):
    """Schema for a single comparator."""

    value: str
    label: str
    description: str


class ConditionInput(BaseModel):
    """A single condition in the request body."""

    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    comparator: str
    value: float | str


class ConditionGroupInput(BaseModel):
    """A group of conditions with a logical operator."""

    operator: str = "AND"
    conditions: list[ConditionInput] = Field(default_factory=list)


class RuleSetInput(BaseModel):
    """Full rule set for preview/save requests."""

    entry_long: ConditionGroupInput | None = None
    exit_long: ConditionGroupInput | None = None
    entry_short: ConditionGroupInput | None = None
    exit_short: ConditionGroupInput | None = None


class TimeframeInput(BaseModel):
    """Timeframe configuration."""

    primary: str = "1h"
    confirmation: str | None = None
    entry: str | None = None


class RiskInput(BaseModel):
    """Risk configuration for strategy saving."""

    stop_loss_method: str = "atr"
    stop_loss_value: float = 2.0
    take_profit_method: str = "atr"
    take_profit_value: float = 3.0
    sizing_method: str = "fixed_fractional"
    sizing_params: dict[str, float] = Field(default_factory=dict)


class PreviewRequest(BaseModel):
    """Request body for the preview endpoint."""

    rules: RuleSetInput
    timeframe: str = "1h"
    symbol: str = "BTCUSDT"
    bars_count: int = Field(default=200, ge=50, le=1000)


class SignalOutput(BaseModel):
    """A single signal in the preview response."""

    timestamp: str
    type: str
    price: float


class MetricsOutput(BaseModel):
    """Quick metrics summary."""

    trades: int
    win_rate: float
    pnl: float


class PreviewResponse(BaseModel):
    """Response from the preview endpoint."""

    signals: list[SignalOutput]
    metrics: MetricsOutput


class SaveRequest(BaseModel):
    """Request body for saving a strategy."""

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    exchange_id: str = "binance"
    symbol: str = "BTCUSDT"
    rules: RuleSetInput
    timeframes: TimeframeInput = Field(default_factory=TimeframeInput)
    risk: RiskInput = Field(default_factory=RiskInput)
    enable_immediately: bool = False


class SaveResponse(BaseModel):
    """Response from the save endpoint."""

    id: str
    name: str
    config_path: str
    message: str


class StrategySummary(BaseModel):
    """Summary of a saved strategy."""

    id: str
    name: str
    description: str
    enabled: bool
    filename: str
    editable: bool = True


class StrategyDetail(BaseModel):
    """Full detail of a saved rule-based strategy for editing."""

    id: str
    name: str
    description: str
    enabled: bool
    exchange_id: str
    symbol: str
    rules: RuleSetInput
    timeframes: TimeframeInput
    risk: RiskInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RULE_BASED_CLASS = "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}
_VALID_EXCHANGES = {"binance", "bybit", "kraken", "okx"}


def _validate_condition_group(group: ConditionGroupInput | None) -> ConditionGroup | None:
    """Validate and convert a ConditionGroupInput to a ConditionGroup."""
    if group is None or not group.conditions:
        return None

    operator = LogicOperator(group.operator)
    conditions: list[Condition] = []
    for cond in group.conditions:
        comparator = Comparator(cond.comparator)
        conditions.append(
            Condition(
                indicator=cond.indicator,
                params=cond.params,
                comparator=comparator,
                value=cond.value,
            )
        )
    return ConditionGroup(operator=operator, conditions=conditions)


def _validate_rules(rules: RuleSetInput) -> RuleSet:
    """Validate and convert a RuleSetInput to a RuleSet."""
    return RuleSet(
        entry_long=_validate_condition_group(rules.entry_long),
        exit_long=_validate_condition_group(rules.exit_long),
        entry_short=_validate_condition_group(rules.entry_short),
        exit_short=_validate_condition_group(rules.exit_short),
    )


def _name_to_id(name: str) -> str:
    """Convert a strategy name to a valid identifier."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    if not slug:
        slug = "strategy"
    return slug


def _generate_sample_bars(count: int) -> list[OHLCV]:
    """Generate synthetic OHLCV bars for preview backtesting.

    Creates a random-walk price series with realistic-looking candles.
    """
    import numpy as np

    rng = np.random.default_rng(42)
    base_price = 42000.0
    bars: list[OHLCV] = []

    price = base_price
    for i in range(count):
        change = rng.normal(0, 0.005)
        price = price * (1 + change)
        price = max(price, 100.0)

        intra_vol = abs(rng.normal(0, 0.003))
        open_price = price * (1 + rng.normal(0, 0.001))
        high_price = max(price, open_price) * (1 + intra_vol)
        low_price = min(price, open_price) * (1 - intra_vol)
        close_price = price
        volume = abs(rng.normal(1000, 200))

        bar = OHLCV(
            open=Decimal(str(round(open_price, 2))),
            high=Decimal(str(round(high_price, 2))),
            low=Decimal(str(round(low_price, 2))),
            close=Decimal(str(round(close_price, 2))),
            volume=Decimal(str(round(volume, 2))),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
        )
        bars.append(bar)

    return bars


def _parse_any_strategy_yaml(path: Path) -> dict[str, Any] | None:
    """Read any YAML strategy file."""
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _parse_strategy_yaml(path: Path) -> dict[str, Any] | None:
    """Read a YAML strategy file, returning None if not a RuleBasedStrategy."""
    data = _parse_any_strategy_yaml(path)
    if data is None:
        return None
    if data.get("strategy_class") != _RULE_BASED_CLASS:
        return None
    return data


def _find_strategy_file(strategy_id: str) -> Path | None:
    """Scan config directory for a strategy YAML with matching id."""
    if not _CONFIG_DIR.is_dir():
        return None
    for path in _CONFIG_DIR.glob("*.yaml"):
        data = _parse_any_strategy_yaml(path)
        if data is not None and data.get("id") == strategy_id:
            return path
    return None


def _to_condition_group_input(group_data: dict[str, Any] | None) -> ConditionGroupInput | None:
    """Convert a raw rules dict entry to a ConditionGroupInput."""
    if group_data is None:
        return None
    return ConditionGroupInput(
        operator=group_data.get("operator", "AND"),
        conditions=[ConditionInput(**c) for c in group_data.get("conditions", [])],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/indicators", response_model=list[IndicatorSchema])
async def list_indicators() -> list[dict[str, Any]]:
    """List all available indicators with their parameter schemas."""
    indicators = get_all_indicators()
    result: list[dict[str, Any]] = []
    for ind in indicators:
        result.append(
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
        )
    return result


@router.get("/comparators", response_model=list[ComparatorSchema])
async def list_comparators() -> list[dict[str, str]]:
    """List all available comparators with descriptions."""
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


@router.post("/preview", response_model=PreviewResponse)
async def preview_signals(request: PreviewRequest) -> PreviewResponse:
    """Run a quick backtest on sample data and return signals and metrics."""
    # Validate rules
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    # Validate timeframe
    if request.timeframe not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timeframe: {request.timeframe}",
        )

    # Build strategy config
    strategy_id = f"preview_{uuid.uuid4().hex[:8]}"
    config = StrategyConfig(
        id=strategy_id,
        name="Preview Strategy",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=[request.symbol],
        exchange=ExchangeStrategyConfig(exchange_id="binance"),
        timeframes=TimeframeConfig(primary=Timeframe(request.timeframe)),
        parameters={
            "rules": rule_set.model_dump(),
            "required_history": 50,
        },
    )

    # Generate sample bars and run backtest
    bars = _generate_sample_bars(request.bars_count)
    runner = BacktestRunner()

    try:
        result = await runner.run(
            strategy_class=RuleBasedStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol=request.symbol,
            timeframe=Timeframe(request.timeframe),
        )
    except Exception as exc:
        logger.exception("Preview backtest failed")
        raise HTTPException(
            status_code=500,
            detail=f"Backtest error: {exc}",
        ) from exc

    # Convert trades to signal outputs
    signals: list[SignalOutput] = []
    for trade in result.trades:
        signals.append(
            SignalOutput(
                timestamp=trade.entry_time.isoformat(),
                type="entry_long" if trade.direction == "LONG" else "entry_short",
                price=float(trade.entry_price),
            )
        )
        signals.append(
            SignalOutput(
                timestamp=trade.exit_time.isoformat(),
                type="exit_long" if trade.direction == "LONG" else "exit_short",
                price=float(trade.exit_price),
            )
        )

    total_pnl = sum((float(t.pnl) for t in result.trades), 0.0)

    metrics = MetricsOutput(
        trades=result.total_trades,
        win_rate=float(result.win_rate),
        pnl=round(total_pnl, 2),
    )

    return PreviewResponse(signals=signals, metrics=metrics)


@router.post("/save", response_model=SaveResponse, status_code=201)
async def save_strategy(request: SaveRequest) -> SaveResponse:
    """Validate the condition tree and save as a YAML config file."""
    # Validate rules
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    # Validate exchange
    if request.exchange_id not in _VALID_EXCHANGES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid exchange: {request.exchange_id}. Must be one of {_VALID_EXCHANGES}",
        )

    # Validate timeframes
    if request.timeframes.primary not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid primary timeframe: {request.timeframes.primary}",
        )

    # Generate strategy ID and file path
    strategy_id = _name_to_id(request.name)
    unique_suffix = uuid.uuid4().hex[:6]
    config_filename = f"{strategy_id}_{unique_suffix}.yaml"

    # Build the strategy config dict
    config_dict: dict[str, Any] = {
        "id": f"{strategy_id}_{unique_suffix}",
        "name": request.name,
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "enabled": request.enable_immediately,
        "symbols": [request.symbol],
        "exchange": {
            "exchange_id": request.exchange_id,
            "market_type": "SPOT",
        },
        "timeframes": {
            "primary": request.timeframes.primary,
        },
        "parameters": {
            "rules": rule_set.model_dump(mode="json"),
            "required_history": 50,
            "description": request.description,
        },
        "position_sizing": {
            "method": request.risk.sizing_method,
            "risk_per_trade_pct": request.risk.sizing_params.get("risk_per_trade_pct", 1.0),
            "max_position_pct": request.risk.sizing_params.get("max_position_pct", 10.0),
        },
    }

    # Add optional timeframes
    if request.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = request.timeframes.confirmation
    if request.timeframes.entry:
        config_dict["timeframes"]["entry"] = request.timeframes.entry

    # Ensure the config directory exists
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Write to YAML file
    config_path = _CONFIG_DIR / config_filename
    try:
        with config_path.open("w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write config file: {exc}",
        ) from exc

    return SaveResponse(
        id=config_dict["id"],
        name=request.name,
        config_path=str(config_path),
        message=f"Strategy '{request.name}' saved successfully",
    )


@router.get("/strategies", response_model=list[StrategySummary])
async def list_strategies() -> list[StrategySummary]:
    """List all saved strategies."""
    if not _CONFIG_DIR.is_dir():
        return []
    strategies: list[StrategySummary] = []
    for path in sorted(_CONFIG_DIR.glob("*.yaml")):
        data = _parse_any_strategy_yaml(path)
        if data is None:
            continue
        params = data.get("parameters", {})
        is_rule_based = data.get("strategy_class") == _RULE_BASED_CLASS
        strategies.append(
            StrategySummary(
                id=data.get("id", path.stem),
                name=data.get("name", path.stem),
                description=params.get("description", ""),
                enabled=data.get("enabled", False),
                filename=path.name,
                editable=is_rule_based,
            )
        )
    return strategies


@router.get("/strategies/{strategy_id}", response_model=StrategyDetail)
async def get_strategy(strategy_id: str) -> StrategyDetail:
    """Get full detail of a saved rule-based strategy for editing."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    data = _parse_strategy_yaml(path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    params = data.get("parameters", {})
    rules_data = params.get("rules", {})
    exchange = data.get("exchange", {})
    symbols = data.get("symbols", ["BTCUSDT"])
    timeframes_data = data.get("timeframes", {})
    sizing = data.get("position_sizing", {})

    rules = RuleSetInput(
        entry_long=_to_condition_group_input(rules_data.get("entry_long")),
        exit_long=_to_condition_group_input(rules_data.get("exit_long")),
        entry_short=_to_condition_group_input(rules_data.get("entry_short")),
        exit_short=_to_condition_group_input(rules_data.get("exit_short")),
    )

    return StrategyDetail(
        id=data.get("id", ""),
        name=data.get("name", ""),
        description=params.get("description", ""),
        enabled=data.get("enabled", False),
        exchange_id=exchange.get("exchange_id", "binance"),
        symbol=symbols[0] if symbols else "BTCUSDT",
        rules=rules,
        timeframes=TimeframeInput(
            primary=timeframes_data.get("primary", "1h"),
            confirmation=timeframes_data.get("confirmation"),
            entry=timeframes_data.get("entry"),
        ),
        risk=RiskInput(
            sizing_method=sizing.get("method", "fixed_fractional"),
            sizing_params={
                "risk_per_trade_pct": sizing.get("risk_per_trade_pct", 1.0),
                "max_position_pct": sizing.get("max_position_pct", 10.0),
            },
        ),
    )


@router.put("/strategies/{strategy_id}", response_model=SaveResponse)
async def update_strategy(strategy_id: str, request: SaveRequest) -> SaveResponse:
    """Update an existing rule-based strategy YAML config."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    # Validate rules
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    # Validate exchange
    if request.exchange_id not in _VALID_EXCHANGES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid exchange: {request.exchange_id}. Must be one of {_VALID_EXCHANGES}",
        )

    # Validate timeframes
    if request.timeframes.primary not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid primary timeframe: {request.timeframes.primary}",
        )

    # Build config dict preserving original id
    config_dict: dict[str, Any] = {
        "id": strategy_id,
        "name": request.name,
        "strategy_class": _RULE_BASED_CLASS,
        "enabled": request.enable_immediately,
        "symbols": [request.symbol],
        "exchange": {
            "exchange_id": request.exchange_id,
            "market_type": "SPOT",
        },
        "timeframes": {
            "primary": request.timeframes.primary,
        },
        "parameters": {
            "rules": rule_set.model_dump(mode="json"),
            "required_history": 50,
            "description": request.description,
        },
        "position_sizing": {
            "method": request.risk.sizing_method,
            "risk_per_trade_pct": request.risk.sizing_params.get("risk_per_trade_pct", 1.0),
            "max_position_pct": request.risk.sizing_params.get("max_position_pct", 10.0),
        },
    }

    # Add optional timeframes
    if request.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = request.timeframes.confirmation
    if request.timeframes.entry:
        config_dict["timeframes"]["entry"] = request.timeframes.entry

    try:
        with path.open("w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write config file: {exc}",
        ) from exc

    return SaveResponse(
        id=strategy_id,
        name=request.name,
        config_path=str(path),
        message=f"Strategy '{request.name}' updated successfully",
    )


@router.post("/strategies/{strategy_id}/toggle")
async def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Toggle the enabled flag of a saved strategy."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    data = _parse_any_strategy_yaml(path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    data["enabled"] = not data.get("enabled", False)

    try:
        with path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update: {exc}") from exc

    return {"id": strategy_id, "enabled": data["enabled"]}


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str) -> Response:
    """Delete a saved rule-based strategy."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete strategy file: {exc}",
        ) from exc

    return Response(status_code=204)
