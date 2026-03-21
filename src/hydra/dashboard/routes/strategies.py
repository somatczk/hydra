"""Consolidated strategy routes: list, detail, update, toggle, delete,
builder indicators/comparators, preview, and save.

Merges the old ``strategy_builder.py`` and ``builder.py`` endpoints into
a single ``/api/strategies/*`` namespace.
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
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from hydra.core.types import OHLCV, Timeframe

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config directory for strategy YAML files
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parents[4] / "config" / "strategies"
_RULE_BASED_CLASS = "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}
_VALID_EXCHANGES = {"binance", "bybit", "kraken", "okx"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "LSTM Momentum": "ML-based momentum strategy using LSTM predictions on 1h BTC/USDT",
    "Mean Reversion": "RSI-based mean reversion with Bollinger Band confirmation",
    "Funding Arbitrage": "Cross-exchange funding rate arbitrage between perpetual swaps",
    "Breakout Scanner": "Volume-weighted breakout detection across multiple timeframes",
}
_DEFAULT_DESCRIPTION = "Custom strategy"


def _pool_from_request(request: Request) -> Any:
    return getattr(request.app.state, "db_pool", None)


def _status_from_enabled(enabled: bool) -> str:
    return "Active" if enabled else "Paused"


def _name_to_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "strategy"


def _parse_any_strategy_yaml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return dict(data) if isinstance(data, dict) else None


def _parse_strategy_yaml(path: Path) -> dict[str, Any] | None:
    """Read a YAML strategy file, returning None if not a RuleBasedStrategy."""
    data = _parse_any_strategy_yaml(path)
    if data is None:
        return None
    if data.get("strategy_class") != _RULE_BASED_CLASS:
        return None
    return data


def _find_strategy_file(strategy_id: str) -> Path | None:
    if not _CONFIG_DIR.is_dir():
        return None
    for path in _CONFIG_DIR.glob("*.yaml"):
        data = _parse_any_strategy_yaml(path)
        if data is not None and data.get("id") == strategy_id:
            return path
    return None


def build_strategy_name_map() -> dict[str, str]:
    """Return {strategy_id: friendly_name} from all YAML configs."""
    name_map: dict[str, str] = {}
    if not _CONFIG_DIR.is_dir():
        return name_map
    for path in _CONFIG_DIR.glob("*.yaml"):
        data = _parse_any_strategy_yaml(path)
        if data is not None and "id" in data:
            name_map[data["id"]] = data.get("name", data["id"])
    return name_map


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------


class StrategyPerformance(BaseModel):
    total_pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0


class StrategyResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    enabled: bool = True
    editable: bool = False
    config_yaml: str = ""
    performance: StrategyPerformance = Field(default_factory=StrategyPerformance)


class ToggleResponse(BaseModel):
    id: str
    enabled: bool


# Builder-related models


class ParamSchema(BaseModel):
    name: str
    type: str
    default: int | float | None = None
    min: int | float | None = None
    max: int | float | None = None


class IndicatorSchema(BaseModel):
    name: str
    category: str
    description: str
    params: list[ParamSchema]


class ComparatorSchema(BaseModel):
    value: str
    label: str
    description: str


class ConditionInput(BaseModel):
    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    comparator: str
    value: float | str


class ConditionGroupInput(BaseModel):
    operator: str = "AND"
    conditions: list[ConditionInput] = Field(default_factory=list)


class RuleSetInput(BaseModel):
    entry_long: ConditionGroupInput | None = None
    exit_long: ConditionGroupInput | None = None
    entry_short: ConditionGroupInput | None = None
    exit_short: ConditionGroupInput | None = None


class TimeframeInput(BaseModel):
    primary: str = "1h"
    confirmation: str | None = None
    entry: str | None = None


class RiskInput(BaseModel):
    stop_loss_method: str = "atr"
    stop_loss_value: float = 2.0
    take_profit_method: str = "atr"
    take_profit_value: float = 3.0
    sizing_method: str = "fixed_fractional"
    sizing_params: dict[str, float] = Field(default_factory=dict)


class PreviewRequest(BaseModel):
    rules: RuleSetInput
    timeframe: str = "1h"
    symbol: str = "BTCUSDT"
    bars_count: int = Field(default=200, ge=50, le=1000)


class SignalOutput(BaseModel):
    timestamp: str
    type: str
    price: float


class MetricsOutput(BaseModel):
    trades: int
    win_rate: float
    pnl: float


class PreviewResponse(BaseModel):
    signals: list[SignalOutput]
    metrics: MetricsOutput


class SaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    exchange_id: str = "binance"
    symbol: str = "BTCUSDT"
    rules: RuleSetInput
    timeframes: TimeframeInput = Field(default_factory=TimeframeInput)
    risk: RiskInput = Field(default_factory=RiskInput)
    enable_immediately: bool = False
    ml_overlay: dict[str, Any] | None = None


class SaveResponse(BaseModel):
    id: str
    name: str
    config_path: str
    message: str


class StrategySummary(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool
    filename: str
    editable: bool = True


class StrategyDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    enabled: bool = True
    editable: bool = False
    exchange_id: str = "binance"
    symbol: str = "BTCUSDT"
    rules: RuleSetInput | None = None
    timeframes: TimeframeInput = Field(default_factory=TimeframeInput)
    risk: RiskInput = Field(default_factory=RiskInput)
    performance: StrategyPerformance = Field(default_factory=StrategyPerformance)
    ml_overlay: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# In-memory placeholder data (fallback when DB is not available)
# ---------------------------------------------------------------------------

_STRATEGIES: dict[str, dict[str, Any]] = {
    "strat-1": {
        "id": "strat-1",
        "name": "LSTM Momentum",
        "description": "ML-based momentum strategy using LSTM predictions on 1h BTC/USDT",
        "status": "Active",
        "enabled": True,
        "editable": False,
        "config_yaml": "strategy:\n  type: lstm_momentum\n  timeframe: 1h\n",
        "performance": {
            "total_pnl": 1240.50,
            "win_rate": 68.7,
            "total_trades": 32,
            "sharpe_ratio": 1.84,
            "max_drawdown": 8.2,
        },
    },
}


def _get_strategy(strategy_id: str) -> dict[str, Any]:
    if strategy_id not in _STRATEGIES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )
    return _STRATEGIES[strategy_id]


def _row_to_strategy(row: Any) -> dict[str, Any]:
    name = row["name"]
    return {
        "id": row["id"],
        "name": name,
        "description": _STRATEGY_DESCRIPTIONS.get(name, _DEFAULT_DESCRIPTION),
        "status": _status_from_enabled(row["enabled"]),
        "enabled": row["enabled"],
        "editable": False,
        "config_yaml": "",
        "performance": {
            "total_pnl": round(float(row["total_pnl"]), 2),
            "win_rate": round(float(row["win_rate"]), 1),
            "total_trades": row["total_trades"],
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        },
    }


# Fixed SQL: use WHERE instead of HAVING for strategy_id filter
_STRATEGIES_QUERY = (
    "SELECT ts.strategy_id AS id, ts.strategy_id AS name, "
    "ts.exchange_id, TRUE AS enabled, "
    "COALESCE(SUM(t.pnl), 0) AS total_pnl, "
    "COUNT(t.id) AS total_trades, "
    "COALESCE("
    "  COUNT(CASE WHEN t.pnl > 0 THEN 1 END)::float "
    "  / NULLIF(COUNT(t.id), 0) * 100, 0"
    ") AS win_rate "
    "FROM trading_sessions ts "
    "LEFT JOIN trades t ON ts.strategy_id = t.strategy_id "
    "GROUP BY ts.strategy_id, ts.exchange_id"
)


# ---------------------------------------------------------------------------
# Builder helper functions
# ---------------------------------------------------------------------------


def _validate_condition_group(group: ConditionGroupInput | None) -> Any:
    if group is None or not group.conditions:
        return None

    from hydra.strategy.condition_schema import (
        Comparator,
        Condition,
        ConditionGroup,
        LogicOperator,
    )

    operator = LogicOperator(group.operator)
    conditions = [
        Condition(
            indicator=cond.indicator,
            params=cond.params,
            comparator=Comparator(cond.comparator),
            value=cond.value,
        )
        for cond in group.conditions
    ]
    return ConditionGroup(operator=operator, conditions=conditions)


def _validate_rules(rules: RuleSetInput) -> Any:
    from hydra.strategy.condition_schema import RuleSet

    return RuleSet(
        entry_long=_validate_condition_group(rules.entry_long),
        exit_long=_validate_condition_group(rules.exit_long),
        entry_short=_validate_condition_group(rules.entry_short),
        exit_short=_validate_condition_group(rules.exit_short),
    )


def _generate_sample_bars(count: int) -> list[OHLCV]:
    import numpy as np

    rng = np.random.default_rng(42)
    base_price = 42000.0
    bars: list[OHLCV] = []
    price = base_price
    for i in range(count):
        change = rng.normal(0, 0.005)
        price = max(price * (1 + change), 100.0)
        intra_vol = abs(rng.normal(0, 0.003))
        open_price = price * (1 + rng.normal(0, 0.001))
        high_price = max(price, open_price) * (1 + intra_vol)
        low_price = min(price, open_price) * (1 - intra_vol)
        volume = abs(rng.normal(1000, 200))
        bars.append(
            OHLCV(
                open=Decimal(str(round(open_price, 2))),
                high=Decimal(str(round(high_price, 2))),
                low=Decimal(str(round(low_price, 2))),
                close=Decimal(str(round(price, 2))),
                volume=Decimal(str(round(volume, 2))),
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
            )
        )
    return bars


def _to_condition_group_input(group_data: dict[str, Any] | None) -> ConditionGroupInput | None:
    if group_data is None:
        return None
    return ConditionGroupInput(
        operator=group_data.get("operator", "AND"),
        conditions=[ConditionInput(**c) for c in group_data.get("conditions", [])],
    )


def _extract_strategy_detail(data: dict[str, Any]) -> dict[str, Any]:
    """Extract structured builder fields from parsed YAML config."""
    params = data.get("parameters", {})
    rules_data = params.get("rules", {})
    tf_data = data.get("timeframes", {})
    sizing = data.get("position_sizing", {})
    exchange = data.get("exchange", {})
    symbols = data.get("symbols", ["BTCUSDT"])

    result: dict[str, Any] = {
        "exchange_id": exchange.get("exchange_id", "binance"),
        "symbol": symbols[0] if symbols else "BTCUSDT",
        "rules": {
            "entry_long": _to_condition_group_input(rules_data.get("entry_long")),
            "exit_long": _to_condition_group_input(rules_data.get("exit_long")),
            "entry_short": _to_condition_group_input(rules_data.get("entry_short")),
            "exit_short": _to_condition_group_input(rules_data.get("exit_short")),
        },
        "timeframes": {
            "primary": tf_data.get("primary", "1h"),
            "confirmation": tf_data.get("confirmation"),
            "entry": tf_data.get("entry"),
        },
        "risk": {
            "stop_loss_method": "atr",
            "stop_loss_value": 2.0,
            "take_profit_method": "atr",
            "take_profit_value": 3.0,
            "sizing_method": sizing.get("method", "fixed_fractional"),
            "sizing_params": {
                "risk_per_trade_pct": sizing.get("risk_per_trade_pct", 1.0),
                "max_position_pct": sizing.get("max_position_pct", 10.0),
            },
        },
    }
    ml_overlay = data.get("ml_overlay")
    if ml_overlay:
        result["ml_overlay"] = ml_overlay
    return result


# ---------------------------------------------------------------------------
# Strategy CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(request: Request) -> list[dict[str, Any]]:
    """List all strategy configs from YAML files, merged with DB performance."""
    # 1. Scan YAML files for strategy definitions
    strategies: dict[str, dict[str, Any]] = {}
    if _CONFIG_DIR.is_dir():
        for path in _CONFIG_DIR.glob("*.yaml"):
            data = _parse_any_strategy_yaml(path)
            if data is None or "id" not in data:
                continue
            sid = data["id"]
            name = data.get("name", sid)
            enabled = data.get("enabled", False)
            strategy_class = data.get("strategy_class", "")
            strategies[sid] = {
                "id": sid,
                "name": name,
                "description": data.get("parameters", {}).get("description")
                or _STRATEGY_DESCRIPTIONS.get(name, _DEFAULT_DESCRIPTION),
                "status": _status_from_enabled(enabled),
                "enabled": enabled,
                "editable": strategy_class == _RULE_BASED_CLASS,
                "config_yaml": "",
                "performance": {
                    "total_pnl": 0.0,
                    "win_rate": 0.0,
                    "total_trades": 0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                },
            }

    # 2. Overlay DB performance stats
    pool = _pool_from_request(request)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT strategy_id, "
                    "COALESCE(SUM(pnl), 0) AS total_pnl, "
                    "COUNT(id) AS total_trades, "
                    "COALESCE("
                    "  COUNT(CASE WHEN pnl > 0 THEN 1 END)::float "
                    "  / NULLIF(COUNT(id), 0) * 100, 0"
                    ") AS win_rate "
                    "FROM trades GROUP BY strategy_id"
                )
            for row in rows:
                sid = row["strategy_id"]
                if sid in strategies:
                    strategies[sid]["performance"] = {
                        **strategies[sid]["performance"],
                        "total_pnl": round(float(row["total_pnl"]), 2),
                        "win_rate": round(float(row["win_rate"]), 1),
                        "total_trades": row["total_trades"],
                    }
        except Exception:
            logger.exception("Failed to fetch strategy performance from DB")

    if not strategies:
        return list(_STRATEGIES.values())

    return list(strategies.values())


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
    # Try YAML first
    path = _find_strategy_file(strategy_id)
    if path is not None:
        data = _parse_any_strategy_yaml(path)
        if data is not None:
            name = data.get("name", strategy_id)
            enabled = data.get("enabled", False)
            strategy_class = data.get("strategy_class", "")
            is_rule_based = strategy_class == _RULE_BASED_CLASS
            description = data.get("parameters", {}).get(
                "description"
            ) or _STRATEGY_DESCRIPTIONS.get(name, _DEFAULT_DESCRIPTION)
            result: dict[str, Any] = {
                "id": strategy_id,
                "name": name,
                "description": description,
                "status": _status_from_enabled(enabled),
                "enabled": enabled,
                "editable": is_rule_based,
                "performance": {
                    "total_pnl": 0.0,
                    "win_rate": 0.0,
                    "total_trades": 0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                },
            }
            if is_rule_based:
                result.update(_extract_strategy_detail(data))
            # Overlay DB performance
            pool = _pool_from_request(request)
            if pool is not None:
                try:
                    async with pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT COALESCE(SUM(pnl), 0) AS total_pnl, "
                            "COUNT(id) AS total_trades, "
                            "COALESCE("
                            "  COUNT(CASE WHEN pnl > 0 THEN 1 END)::float "
                            "  / NULLIF(COUNT(id), 0) * 100, 0"
                            ") AS win_rate "
                            "FROM trades WHERE strategy_id = $1",
                            strategy_id,
                        )
                    if row:
                        result["performance"] = {
                            **result["performance"],
                            "total_pnl": round(float(row["total_pnl"]), 2),
                            "win_rate": round(float(row["win_rate"]), 1),
                            "total_trades": row["total_trades"],
                        }
                except Exception:
                    logger.exception("Failed to fetch performance for %s", strategy_id)
            return result

    # Fallback to DB / in-memory
    pool = _pool_from_request(request)
    if pool is None:
        return _get_strategy(strategy_id)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _STRATEGIES_QUERY + " HAVING ts.strategy_id = $1",
                strategy_id,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy {strategy_id} not found",
            )
        return _row_to_strategy(row)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch strategy %s from DB", strategy_id)
        return _get_strategy(strategy_id)


@router.put("/{strategy_id}", response_model=SaveResponse)
async def update_strategy(strategy_id: str, body: SaveRequest) -> SaveResponse:
    """Update a rule-based strategy config (structured payload -> YAML)."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    data = _parse_strategy_yaml(path)
    if data is None:
        raise HTTPException(status_code=400, detail="Only rule-based strategies can be edited")

    try:
        rule_set = _validate_rules(body.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    if body.exchange_id not in _VALID_EXCHANGES:
        raise HTTPException(status_code=422, detail=f"Invalid exchange: {body.exchange_id}")

    if body.timeframes.primary not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timeframe: {body.timeframes.primary}",
        )

    existing_enabled = data.get("enabled", False)
    existing_required_history = data.get("parameters", {}).get("required_history", 50)

    config_dict: dict[str, Any] = {
        "id": strategy_id,
        "name": body.name,
        "strategy_class": _RULE_BASED_CLASS,
        "enabled": True if body.enable_immediately else existing_enabled,
        "symbols": [body.symbol],
        "exchange": {"exchange_id": body.exchange_id, "market_type": "SPOT"},
        "timeframes": {"primary": body.timeframes.primary},
        "parameters": {
            "rules": rule_set.model_dump(mode="json"),
            "required_history": existing_required_history,
            "description": body.description,
        },
        "position_sizing": {
            "method": body.risk.sizing_method,
            "risk_per_trade_pct": body.risk.sizing_params.get("risk_per_trade_pct", 1.0),
            "max_position_pct": body.risk.sizing_params.get("max_position_pct", 10.0),
        },
    }

    if body.ml_overlay:
        config_dict["ml_overlay"] = body.ml_overlay

    if body.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = body.timeframes.confirmation
    if body.timeframes.entry:
        config_dict["timeframes"]["entry"] = body.timeframes.entry

    try:
        with path.open("w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write: {exc}") from exc

    return SaveResponse(
        id=strategy_id,
        name=body.name,
        config_path=str(path),
        message=f"Strategy '{body.name}' updated successfully",
    )


@router.get("/{strategy_id}/performance", response_model=StrategyPerformance)
async def get_strategy_performance(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get performance metrics for a strategy."""
    default_perf = {
        "total_pnl": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "sharpe_ratio": 0.0,
        "max_drawdown": 0.0,
    }

    # Check YAML or in-memory existence
    path = _find_strategy_file(strategy_id)
    if path is None and strategy_id not in _STRATEGIES:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    # If in-memory fallback has performance, use it as base
    if strategy_id in _STRATEGIES:
        default_perf = _STRATEGIES[strategy_id].get("performance", default_perf)

    # Overlay DB performance if available
    pool = _pool_from_request(request)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(pnl), 0) AS total_pnl, "
                    "COUNT(id) AS total_trades, "
                    "COALESCE("
                    "  COUNT(CASE WHEN pnl > 0 THEN 1 END)::float "
                    "  / NULLIF(COUNT(id), 0) * 100, 0"
                    ") AS win_rate "
                    "FROM trades WHERE strategy_id = $1",
                    strategy_id,
                )
            if row and row["total_trades"] > 0:
                return {
                    **default_perf,
                    "total_pnl": round(float(row["total_pnl"]), 2),
                    "win_rate": round(float(row["win_rate"]), 1),
                    "total_trades": row["total_trades"],
                }
        except Exception:
            logger.exception("Failed to fetch performance for %s", strategy_id)

    return default_perf


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: str) -> Response:
    """Delete a saved strategy."""
    path = _find_strategy_file(strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {exc}") from exc
    return Response(status_code=204)


@router.post("/{strategy_id}/toggle", response_model=ToggleResponse)
async def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Toggle enabled flag of a strategy."""
    # Try YAML file first
    path = _find_strategy_file(strategy_id)
    if path is not None:
        data = _parse_any_strategy_yaml(path)
        if data is not None:
            data["enabled"] = not data.get("enabled", False)
            try:
                with path.open("w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return {"id": strategy_id, "enabled": data["enabled"]}

    # Fall back to in-memory
    strat = _get_strategy(strategy_id)
    strat["enabled"] = not strat["enabled"]
    strat["status"] = _status_from_enabled(strat["enabled"])
    return {"id": strategy_id, "enabled": strat["enabled"]}


# ---------------------------------------------------------------------------
# Builder endpoints (merged from strategy_builder.py)
# ---------------------------------------------------------------------------


@router.post("/preview", response_model=PreviewResponse)
async def preview_signals(request: PreviewRequest) -> PreviewResponse:
    """Run a quick backtest on sample data and return signals and metrics."""
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    if request.timeframe not in _VALID_TIMEFRAMES:
        raise HTTPException(status_code=422, detail=f"Invalid timeframe: {request.timeframe}")

    from hydra.backtest.runner import BacktestRunner
    from hydra.strategy.builtin.rule_based import RuleBasedStrategy
    from hydra.strategy.config import ExchangeStrategyConfig, StrategyConfig, TimeframeConfig

    strategy_id = f"preview_{uuid.uuid4().hex[:8]}"
    config = StrategyConfig(
        id=strategy_id,
        name="Preview Strategy",
        strategy_class=_RULE_BASED_CLASS,
        symbols=[request.symbol],
        exchange=ExchangeStrategyConfig(exchange_id="binance"),
        timeframes=TimeframeConfig(primary=Timeframe(request.timeframe)),
        parameters={"rules": rule_set.model_dump(), "required_history": 50},
    )

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
        raise HTTPException(status_code=500, detail=f"Backtest error: {exc}") from exc

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
    """Validate and save a strategy as a YAML config file."""
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc

    if request.exchange_id not in _VALID_EXCHANGES:
        raise HTTPException(status_code=422, detail=f"Invalid exchange: {request.exchange_id}")

    if request.timeframes.primary not in _VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timeframe: {request.timeframes.primary}",
        )

    strategy_id = _name_to_id(request.name)
    unique_suffix = uuid.uuid4().hex[:6]
    config_filename = f"{strategy_id}_{unique_suffix}.yaml"

    config_dict: dict[str, Any] = {
        "id": f"{strategy_id}_{unique_suffix}",
        "name": request.name,
        "strategy_class": _RULE_BASED_CLASS,
        "enabled": request.enable_immediately,
        "symbols": [request.symbol],
        "exchange": {"exchange_id": request.exchange_id, "market_type": "SPOT"},
        "timeframes": {"primary": request.timeframes.primary},
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

    if request.ml_overlay:
        config_dict["ml_overlay"] = request.ml_overlay

    if request.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = request.timeframes.confirmation
    if request.timeframes.entry:
        config_dict["timeframes"]["entry"] = request.timeframes.entry

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _CONFIG_DIR / config_filename
    try:
        with config_path.open("w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write: {exc}") from exc

    return SaveResponse(
        id=config_dict["id"],
        name=request.name,
        config_path=str(config_path),
        message=f"Strategy '{request.name}' saved successfully",
    )
