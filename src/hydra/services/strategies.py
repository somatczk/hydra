"""Strategy YAML store and the strategy CRUD, preview and save operations."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from hydra.backtest.data import (
    DataProvenance,
    InvalidMarketDataError,
    NoMarketDataError,
    load_latest_bars,
)
from hydra.core.config import DataConfig
from hydra.core.types import OHLCV, ExchangeId, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.data.errors import DataFetchError
from hydra.data.on_demand import OnDemandFetchService
from hydra.data.storage import MarketDataRepository
from hydra.execution.client_factory import RestHandle, SharedRestClients, validate_venue
from hydra.execution.errors import (
    ConfigReloadRefusedError,
    SessionNotRunningError,
    UnsupportedVenueError,
)
from hydra.services import universe
from hydra.services.errors import (
    DB_ERRORS,
    ServiceError,
    db_errors,
    require_pool,
    service_error,
)
from hydra.strategy.config import StrategyRiskConfig, config_hash
from hydra.strategy.engine import validate_strategy_config, write_strategy_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config directory for strategy YAML files
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "strategies"
_RULE_BASED_CLASS = "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}
_VALID_EXCHANGES = {"binance", "binanceusdm", "bybit", "kraken", "okx"}
_PERP_EXCHANGES = {"binanceusdm"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "Mean Reversion": "RSI-based mean reversion with Bollinger Band confirmation",
    "Breakout Scanner": "Volume-weighted breakout detection across multiple timeframes",
}
_DEFAULT_DESCRIPTION = "Custom strategy"


def _status_from_enabled(enabled: bool) -> str:
    return "Active" if enabled else "Paused"


def _name_to_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "strategy"


def parse_strategy_file(path: Path) -> dict[str, Any] | None:
    """Read a strategy YAML file; None if unreadable or not a mapping."""
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return dict(data) if isinstance(data, dict) else None


def _parse_strategy_yaml(path: Path) -> dict[str, Any] | None:
    """Read a YAML strategy file, returning None if not a RuleBasedStrategy."""
    data = parse_strategy_file(path)
    if data is None:
        return None
    if data.get("strategy_class") != _RULE_BASED_CLASS:
        return None
    return data


def iter_strategy_yamls() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, data)`` for every readable strategy YAML, read from the dir at call time."""
    if not _CONFIG_DIR.is_dir():
        return
    for path in sorted(_CONFIG_DIR.glob("*.yaml")):
        data = parse_strategy_file(path)
        if data is not None:
            yield path, data


def find_strategy_file(strategy_id: str) -> Path | None:
    for path, data in iter_strategy_yamls():
        if data.get("id") == strategy_id:
            return path
    return None


def build_strategy_name_map() -> dict[str, str]:
    """Return {strategy_id: friendly_name} from all YAML configs."""
    return {
        data["id"]: data.get("name", data["id"])
        for _, data in iter_strategy_yamls()
        if "id" in data
    }


def _write_validated(path: Path, data: dict[str, Any]) -> None:
    """Validate strictly and write atomically; an invalid config is a 422 and writes nothing."""
    try:
        write_strategy_config(path, data)
    except (ValueError, TypeError) as exc:
        raise ServiceError(422, f"Invalid strategy config: {exc}") from exc
    except OSError as exc:
        raise ServiceError(500, f"Failed to write: {exc}") from exc


def _strategy_not_found(strategy_id: str) -> ServiceError:
    return service_error(
        404, "strategy_not_found", f"Strategy {strategy_id} not found", "not_found"
    )


def _exchange_block(exchange_id: str, leverage: int) -> dict[str, Any]:
    """The ``exchange`` section of a saved strategy: binanceusdm is the FUTURES venue.

    A perp strategy carries its leverage (1-3x, pydantic-validated here, re-validated at
    session start against the risk_config cap); spot strategies stay 1x.
    """
    if exchange_id in _PERP_EXCHANGES:
        return {
            "exchange_id": exchange_id,
            "market_type": "FUTURES",
            "leverage": leverage,
            "margin_mode": "isolated",
        }
    if leverage != 1:
        raise ServiceError(
            422, f"Leverage applies to binanceusdm (futures) strategies only, got {leverage}x"
        )
    return {"exchange_id": exchange_id, "market_type": "SPOT"}


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------


class StrategyPerformance(BaseModel):
    total_pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None


class StrategyResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    enabled: bool = True
    editable: bool = False
    config_yaml: str = ""
    performance: StrategyPerformance | None = None
    performance_error: str | None = None


class ToggleResponse(BaseModel):
    id: str
    enabled: bool


class ConditionInput(BaseModel):
    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    comparator: str
    value: float | str
    param_key: str | None = None
    value_param_key: str | None = None
    value_ref_overrides: dict[str, str] | None = None


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


def _risk_block(risk: RiskInput) -> dict[str, Any]:
    """The YAML ``risk:`` block for builder SL/TP; a trailing stop drops the take-profit."""
    trailing = risk.stop_loss_method == "trailing_stop"
    block = {
        "stop_loss_method": risk.stop_loss_method,
        "stop_loss_value": risk.stop_loss_value,
        "take_profit_method": "none" if trailing else risk.take_profit_method,
        "take_profit_value": 0.0 if trailing else risk.take_profit_value,
    }
    try:
        return StrategyRiskConfig(**block).model_dump(mode="json")
    except ValidationError as exc:
        msgs = "; ".join(str(e.get("msg", "")) for e in exc.errors())
        raise ServiceError(422, f"Invalid risk settings: {msgs}") from exc


class PreviewRequest(BaseModel):
    rules: RuleSetInput
    timeframe: Timeframe = Timeframe.H1
    exchange_id: ExchangeId = "binance"
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


class PreviewProvenance(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    bar_count: int
    first_ts: str
    last_ts: str
    gap_count: int
    data_source: str


class PreviewResponse(BaseModel):
    signals: list[SignalOutput]
    metrics: MetricsOutput
    provenance: PreviewProvenance


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
    schedule: dict[str, Any] | None = Field(
        None,
        description="entry hours (UTC) and weekdays (Monday=0); on update None keeps the "
        "stored schedule and {} clears it",
    )
    leverage: int = Field(1, ge=1, le=3, description="perp strategies only (1-3x, isolated)")


class SaveResponse(BaseModel):
    id: str
    name: str
    config_path: str
    message: str


class ReloadResponse(BaseModel):
    session_id: str
    from_hash: str | None
    to_hash: str
    applies_at: str


class RunningSession(BaseModel):
    session_id: str
    config_hash: str | None
    stale: bool


class StrategyDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    enabled: bool = True
    editable: bool = False
    exchange_id: str = "binance"
    symbol: str = "BTCUSDT"
    leverage: int = 1
    rules: RuleSetInput | None = None
    timeframes: TimeframeInput = Field(default_factory=TimeframeInput)
    risk: RiskInput = Field(default_factory=RiskInput)
    performance: StrategyPerformance | None = None
    performance_error: str | None = None
    ml_overlay: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    config_hash: str | None = None
    file_changed: bool = False
    running_sessions: list[RunningSession] = Field(default_factory=list)


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
            param_key=cond.param_key,
            value_param_key=cond.value_param_key,
            value_ref_overrides=cond.value_ref_overrides,
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


def _to_condition_group_input(group_data: dict[str, Any] | None) -> ConditionGroupInput | None:
    if group_data is None:
        return None
    return ConditionGroupInput(
        operator=group_data.get("operator", "AND"),
        conditions=[ConditionInput(**c) for c in group_data.get("conditions", [])],
    )


def _stored_risk(raw: Any) -> dict[str, Any]:
    """SL/TP as enforced: the YAML ``risk:`` block, or the 5 % default stop when absent."""
    try:
        cfg = StrategyRiskConfig(**raw) if isinstance(raw, dict) else StrategyRiskConfig()
    except ValidationError:
        cfg = StrategyRiskConfig()
    return {
        "stop_loss_method": cfg.stop_loss_method,
        "stop_loss_value": cfg.stop_loss_value,
        "take_profit_method": cfg.take_profit_method,
        "take_profit_value": cfg.take_profit_value,
    }


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
        # P4-F: pass the stored perp leverage through so an edited strategy round-trips it
        "leverage": exchange.get("leverage", 1),
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
            **_stored_risk(data.get("risk")),
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
# Strategy CRUD
# ---------------------------------------------------------------------------


async def list_strategies(pool: Any) -> list[dict[str, Any]]:
    """List all strategy configs from YAML files, merged with DB performance."""
    # 1. Scan YAML files for strategy definitions
    strategies: dict[str, dict[str, Any]] = {}
    for _, data in iter_strategy_yamls():
        if "id" not in data:
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
        }

    if strategies:
        rows, perf_error = await _overlay_performance(pool, _PERFORMANCE_SQL)
        perf_by_id = {row["strategy_id"]: _performance_from_row(row) for row in rows}
        for sid, entry in strategies.items():
            entry["performance_error"] = perf_error
            if perf_error is None:
                entry["performance"] = perf_by_id.get(sid, _performance_from_row(None))

    return list(strategies.values())


_PERFORMANCE_SQL = (
    "SELECT strategy_id, "
    "COALESCE(SUM(pnl), 0) AS total_pnl, "
    "COUNT(id) AS total_trades, "
    "COALESCE("
    "  COUNT(CASE WHEN pnl > 0 THEN 1 END)::float "
    "  / NULLIF(COUNT(id), 0) * 100, 0"
    ") AS win_rate "
    "FROM trades GROUP BY strategy_id"
)
_PERFORMANCE_UNAVAILABLE = "Performance unavailable: database is not available"


def _performance_from_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {"total_pnl": 0.0, "win_rate": 0.0, "total_trades": 0}
    return {
        "total_pnl": round(float(row["total_pnl"]), 2),
        "win_rate": round(float(row["win_rate"]), 1),
        "total_trades": row["total_trades"],
    }


async def _overlay_performance(pool: Any, query: str, *args: Any) -> tuple[list[Any], str | None]:
    """Trade-stat rows for the performance overlay, or why the database could not answer."""
    if pool is None:
        return [], _PERFORMANCE_UNAVAILABLE
    try:
        async with pool.acquire() as conn:
            return list(await conn.fetch(query, *args)), None
    except DB_ERRORS:
        logger.exception("Database error while loading strategy performance")
        return [], "Performance unavailable: database error"


_ONE_PERFORMANCE_SQL = (
    "SELECT COALESCE(SUM(pnl), 0) AS total_pnl, "
    "COUNT(id) AS total_trades, "
    "COALESCE("
    "  COUNT(CASE WHEN pnl > 0 THEN 1 END)::float "
    "  / NULLIF(COUNT(id), 0) * 100, 0"
    ") AS win_rate "
    "FROM trades WHERE strategy_id = $1"
)


async def get_strategy(pool: Any, strategy_id: str) -> dict[str, Any]:
    """Get a single strategy detail with structured builder fields."""
    # Try YAML first
    path = find_strategy_file(strategy_id)
    if path is not None:
        data = parse_strategy_file(path)
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
                "schedule": data.get("schedule"),
            }
            if is_rule_based:
                result.update(_extract_strategy_detail(data))
            result.update(await _session_drift(data, strategy_id, pool))
            rows, perf_error = await _overlay_performance(pool, _ONE_PERFORMANCE_SQL, strategy_id)
            result["performance_error"] = perf_error
            if perf_error is None:
                result["performance"] = _performance_from_row(rows[0] if rows else None)
            return result

    # No YAML: the strategy may only exist in trading-session history, which needs the DB
    pool = require_pool(pool)
    async with db_errors("loading the strategy"), pool.acquire() as conn:
        row = await conn.fetchrow(
            _STRATEGIES_QUERY + " HAVING ts.strategy_id = $1",
            strategy_id,
        )
    if row is None:
        raise _strategy_not_found(strategy_id)
    result = _row_to_strategy(row)
    # no YAML for this strategy: a snapshot-recovered session can still be running on it
    result.update(await _session_drift(None, strategy_id, pool))
    return result


async def _session_drift(
    data: dict[str, Any] | None, strategy_id: str, pool: Any
) -> dict[str, Any]:
    """The file's hash (None without a file) and, per running session, whether it differs.

    Running sessions are read from the database (``session_store.load_running``), so the
    drift read works without a session manager — including while the engine is down.
    """
    from hydra.execution import session_store

    file_hash: str | None = None
    if data is not None:
        try:
            file_hash = config_hash(validate_strategy_config(data))
        except (ValueError, TypeError):
            file_hash = None
    rows: list[Any] = []
    if pool is not None:
        try:
            rows = await session_store.load_running(pool)
        except DB_ERRORS:
            # like the performance overlay: a DB stall degrades this field, not the read
            logger.exception("Running sessions unreadable for the strategy drift")
    sessions = [
        {
            "session_id": str(row["id"]),
            "config_hash": row.get("config_hash"),
            "stale": row.get("config_hash") != file_hash,
        }
        for row in rows
        if row.get("strategy_id") == strategy_id
    ]
    return {
        "config_hash": file_hash,
        "file_changed": any(s["stale"] for s in sessions),
        "running_sessions": sessions,
    }


async def reload_strategy(manager: Any, strategy_id: str) -> dict[str, Any]:
    """Queue a parameters-only reload on the strategy's running session (next closed bar)."""
    if manager is None:
        raise service_error(404, "session_not_running", "No session manager", "not_found")
    try:
        result = await manager.reload_config(strategy_id)
    except SessionNotRunningError as exc:
        raise service_error(
            404, "session_not_running", f"No running session for {strategy_id}", "not_found"
        ) from exc
    except (KeyError, FileNotFoundError) as exc:
        # the session keeps running on its snapshot, but there is no file to reload from
        raise service_error(404, "strategy_not_found", str(exc).strip("'\""), "not_found") from exc
    except ConfigReloadRefusedError as exc:
        raise service_error(
            409, exc.code, str(exc), "invalid", changed_fields=exc.changed_fields
        ) from exc
    except (ValueError, TypeError) as exc:
        raise ServiceError(422, f"Invalid strategy config: {exc}") from exc
    return {
        "session_id": result.session_id,
        "from_hash": result.from_hash,
        "to_hash": result.to_hash,
        "applies_at": "next_closed_bar",
    }


async def update_strategy(pool: Any, strategy_id: str, body: SaveRequest) -> SaveResponse:
    """Update a rule-based strategy config (structured payload -> YAML).

    The symbol must be a member of the current top-100 universe (422
    ``symbol_not_in_universe`` otherwise); running sessions and the stored file
    are never retro-invalidated.
    """
    path = find_strategy_file(strategy_id)
    if path is None:
        raise ServiceError(404, f"Strategy '{strategy_id}' not found")

    data = _parse_strategy_yaml(path)
    if data is None:
        raise ServiceError(400, "Only rule-based strategies can be edited")

    try:
        rule_set = _validate_rules(body.rules)
    except (ValueError, KeyError) as exc:
        raise ServiceError(422, f"Invalid rules: {exc}") from exc

    if body.exchange_id not in _VALID_EXCHANGES:
        raise ServiceError(422, f"Invalid exchange: {body.exchange_id}")

    if body.timeframes.primary not in _VALID_TIMEFRAMES:
        raise ServiceError(422, f"Invalid timeframe: {body.timeframes.primary}")

    await universe.validate_symbol(pool, body.symbol)

    existing_enabled = data.get("enabled", False)
    existing_required_history = data.get("parameters", {}).get("required_history", 50)

    config_dict: dict[str, Any] = {
        "id": strategy_id,
        "name": body.name,
        "strategy_class": _RULE_BASED_CLASS,
        "enabled": True if body.enable_immediately else existing_enabled,
        "symbols": [body.symbol],
        "exchange": _exchange_block(body.exchange_id, body.leverage),
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
        "risk": _risk_block(body.risk),
    }

    if body.ml_overlay:
        config_dict["ml_overlay"] = body.ml_overlay
    # the builder never sends a schedule: keep the stored one rather than silently drop it
    schedule = data.get("schedule") if body.schedule is None else body.schedule
    if schedule:
        config_dict["schedule"] = schedule

    if body.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = body.timeframes.confirmation
    if body.timeframes.entry:
        config_dict["timeframes"]["entry"] = body.timeframes.entry

    _write_validated(path, config_dict)

    return SaveResponse(
        id=strategy_id,
        name=body.name,
        config_path=str(path),
        message=f"Strategy '{body.name}' updated successfully",
    )


async def get_strategy_performance(pool: Any, strategy_id: str) -> dict[str, Any]:
    """Get performance metrics for a strategy."""
    if find_strategy_file(strategy_id) is None:
        raise _strategy_not_found(strategy_id)
    pool = require_pool(pool)
    async with db_errors("loading strategy performance"), pool.acquire() as conn:
        row = await conn.fetchrow(_ONE_PERFORMANCE_SQL, strategy_id)
    return _performance_from_row(row)


def delete_strategy(strategy_id: str) -> None:
    """Delete a saved strategy."""
    path = find_strategy_file(strategy_id)
    if path is None:
        raise ServiceError(404, f"Strategy '{strategy_id}' not found")
    try:
        path.unlink()
    except OSError as exc:
        raise ServiceError(500, f"Failed to delete: {exc}") from exc


def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Toggle enabled flag of a strategy."""
    # Try YAML file first
    path = find_strategy_file(strategy_id)
    if path is not None:
        data = parse_strategy_file(path)
        if data is not None:
            data["enabled"] = not data.get("enabled", False)
            _write_validated(path, data)
            return {"id": strategy_id, "enabled": data["enabled"]}

    raise _strategy_not_found(strategy_id)


# ---------------------------------------------------------------------------
# Builder: preview, save, import
# ---------------------------------------------------------------------------


class _PreviewFetchClients:
    """The engine process's REST clients behind the preview fetch engine.

    One refcounted pool for the process lifetime (the worker's pattern): the first
    preview fetch constructs the exchange client — ``load_markets`` is paid once per
    process, not per preview — and the handles are held until the process exits.
    """

    def __init__(self) -> None:
        self._pool = SharedRestClients()
        self._handles: dict[ExchangeId, RestHandle] = {}

    async def client(self, exchange_id: ExchangeId) -> Any:
        handle = self._handles.get(exchange_id)
        if handle is None:
            handle = await self._pool.handle(exchange_id)
            # Two concurrent cold fetches both acquire; the loser must not leak its reference.
            existing = self._handles.setdefault(exchange_id, handle)
            if existing is not handle:
                await handle.release()
                handle = existing
        return handle.client


_PREVIEW_ENGINE: OnDemandFetchService | None = None


def preview_fetch_engine(cfg: DataConfig) -> OnDemandFetchService | None:
    """The engine process's one preview fetch engine; ``None`` while the kill switch is on.

    Constructed exactly once per process (the one-instance contract: one weight budget,
    one set of per-dataset write locks). The check-and-set is synchronous, so under
    asyncio no two previews can ever build an engine each. The worker process builds
    its own engine in ``hydra.backtest.worker`` — the two never share one.
    """
    global _PREVIEW_ENGINE
    if not cfg.auto_fetch_enabled:
        return None
    if _PREVIEW_ENGINE is None:
        clients = _PreviewFetchClients()
        _PREVIEW_ENGINE = OnDemandFetchService.from_config(cfg, clients.client)
    return _PREVIEW_ENGINE


async def _fetched_latest_bars(
    repo: MarketDataRepository, body: PreviewRequest, fetch_engine: OnDemandFetchService
) -> tuple[list[OHLCV], DataProvenance]:
    """Fetch the preview's latest-bars window on demand, then re-load the stored rows.

    The window is exactly ``bars_count`` bars (one page) ending now; the bars are
    re-loaded from the store, so provenance keeps ``data_source='db'``. A fetch past
    patience is the retryable 503 ``data_fetch_failed``; a market the exchange
    genuinely has none of keeps today's 422 ``no_market_data``.
    """
    now = datetime.now(UTC)
    step = timedelta(seconds=timeframe_seconds(body.timeframe))
    try:
        await fetch_engine.ensure_available(
            repo,
            body.exchange_id,
            body.symbol,
            body.timeframe,
            now - body.bars_count * step,
            now,
        )
    except DataFetchError as exc:
        raise ServiceError(503, exc.to_detail()) from exc
    except NoMarketDataError as exc:
        raise ServiceError(422, exc.to_detail()) from exc
    try:
        async with db_errors("re-loading market data for preview"):
            return await load_latest_bars(
                repo, body.exchange_id, body.symbol, body.timeframe, body.bars_count
            )
    except NoMarketDataError as exc:
        raise ServiceError(422, exc.to_detail()) from exc
    except InvalidMarketDataError as exc:
        raise service_error(422, "invalid_market_data", str(exc), "invalid") from exc


async def preview_signals(
    pool: Any, body: PreviewRequest, *, fetch_engine: OnDemandFetchService | None = None
) -> PreviewResponse:
    """Run a quick backtest on the latest stored bars and return signals and metrics.

    A market with no stored bars is fetched first — the latest ``bars_count`` bars,
    one page — and re-loaded from the store. ``metrics.win_rate`` is a fraction
    (0-1), unlike the percentage in backtest results.
    """
    try:
        rule_set = _validate_rules(body.rules)
    except (ValueError, KeyError) as exc:
        raise service_error(422, "invalid_rules", f"Invalid rules: {exc}", "invalid") from exc
    try:
        validate_venue(body.exchange_id)
    except UnsupportedVenueError as exc:
        raise service_error(422, exc.code, str(exc), "invalid") from exc

    pool = require_pool(pool)
    repo = MarketDataRepository.from_pool(pool)
    try:
        async with db_errors("loading market data for preview"):
            bars, provenance = await load_latest_bars(
                repo, body.exchange_id, body.symbol, body.timeframe, body.bars_count
            )
    except NoMarketDataError as exc:
        if fetch_engine is None:
            raise ServiceError(422, exc.to_detail()) from exc
        bars, provenance = await _fetched_latest_bars(repo, body, fetch_engine)
    except InvalidMarketDataError as exc:
        raise service_error(422, "invalid_market_data", str(exc), "invalid") from exc

    from hydra.backtest.runner import BacktestRunner
    from hydra.strategy.builtin.rule_based import RuleBasedStrategy
    from hydra.strategy.config import ExchangeStrategyConfig, StrategyConfig, TimeframeConfig

    config = StrategyConfig(
        id=f"preview_{uuid.uuid4().hex[:8]}",
        name="Preview Strategy",
        strategy_class=_RULE_BASED_CLASS,
        symbols=[body.symbol],
        exchange=ExchangeStrategyConfig(exchange_id=body.exchange_id),
        timeframes=TimeframeConfig(primary=body.timeframe),
        parameters={"rules": rule_set.model_dump(), "required_history": 50},
    )

    runner = BacktestRunner()
    try:
        result = await runner.run(
            strategy_class=RuleBasedStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol=body.symbol,
            timeframe=body.timeframe,
        )
    except Exception as exc:
        logger.exception("Preview backtest failed")
        raise ServiceError(500, f"Backtest error: {exc}") from exc

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
    return PreviewResponse(
        signals=signals, metrics=metrics, provenance=_preview_provenance(provenance)
    )


def _preview_provenance(provenance: DataProvenance) -> PreviewProvenance:
    data = provenance.to_dict()
    return PreviewProvenance(
        exchange=data["exchange"],
        symbol=data["symbol"],
        timeframe=data["timeframe"],
        bar_count=data["bar_count"],
        first_ts=data["first_ts"],
        last_ts=data["last_ts"],
        gap_count=data["gap_count"],
        data_source=data["data_source"],
    )


async def save_strategy(pool: Any, request: SaveRequest) -> SaveResponse:
    """Validate and save a strategy as a YAML config file.

    The symbol must be a member of the current top-100 universe (422
    ``symbol_not_in_universe`` otherwise); nothing already stored or running
    is ever retro-invalidated.
    """
    try:
        rule_set = _validate_rules(request.rules)
    except (ValueError, KeyError) as exc:
        raise ServiceError(422, f"Invalid rules: {exc}") from exc

    if request.exchange_id not in _VALID_EXCHANGES:
        raise ServiceError(422, f"Invalid exchange: {request.exchange_id}")

    if request.timeframes.primary not in _VALID_TIMEFRAMES:
        raise ServiceError(422, f"Invalid timeframe: {request.timeframes.primary}")

    await universe.validate_symbol(pool, request.symbol)

    strategy_id = _name_to_id(request.name)
    unique_suffix = uuid.uuid4().hex[:6]
    config_filename = f"{strategy_id}_{unique_suffix}.yaml"

    config_dict: dict[str, Any] = {
        "id": f"{strategy_id}_{unique_suffix}",
        "name": request.name,
        "strategy_class": _RULE_BASED_CLASS,
        "enabled": request.enable_immediately,
        "symbols": [request.symbol],
        "exchange": _exchange_block(request.exchange_id, request.leverage),
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
        "risk": _risk_block(request.risk),
    }

    if request.ml_overlay:
        config_dict["ml_overlay"] = request.ml_overlay
    if request.schedule:
        config_dict["schedule"] = request.schedule

    if request.timeframes.confirmation:
        config_dict["timeframes"]["confirmation"] = request.timeframes.confirmation
    if request.timeframes.entry:
        config_dict["timeframes"]["entry"] = request.timeframes.entry

    config_path = _CONFIG_DIR / config_filename
    _write_validated(config_path, config_dict)

    return SaveResponse(
        id=config_dict["id"],
        name=request.name,
        config_path=str(config_path),
        message=f"Strategy '{request.name}' saved successfully",
    )


def check_import_filename(filename: str | None) -> None:
    """Reject an upload that is not a .yaml/.yml file, before its body is read."""
    if not filename or not filename.endswith((".yaml", ".yml")):
        raise ServiceError(422, "File must be .yaml or .yml")


def import_strategy(filename: str | None, content: bytes) -> SaveResponse:
    """Import a strategy from an uploaded YAML file."""
    check_import_filename(filename)

    try:
        data = yaml.safe_load(content.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ServiceError(422, f"Invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ServiceError(422, "YAML must be a dict")

    # Ensure unique ID
    original_name = data.get("name", "Imported Strategy")
    suffix = uuid.uuid4().hex[:6]
    strategy_id = _name_to_id(original_name) + f"_{suffix}"
    data["id"] = strategy_id
    data.setdefault("enabled", False)
    data.setdefault("name", original_name)

    dest = _CONFIG_DIR / f"{strategy_id}.yaml"
    _write_validated(dest, data)

    return SaveResponse(
        id=strategy_id,
        name=original_name,
        config_path=str(dest),
        message=f"Strategy '{original_name}' imported as '{strategy_id}'",
    )
