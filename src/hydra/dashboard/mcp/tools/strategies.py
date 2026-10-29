"""Strategy tools (list, get, save, update, delete, toggle, reload, import, export).

Nine tools registered on the shared server instance, each a thin call into the
service layer (the same functions the REST routes call). Inputs mirror the service
request models' fields and the models are constructed inside the tool, so pydantic
revalidates and no schema drift is possible. Every ``ServiceError`` surfaces as a
``ToolError`` carrying the full error envelope verbatim — a 409 ``restart_required``
from ``strategy_reload`` keeps its ``changed_fields`` list. Every mutating tool
writes a ``hydra.audit`` line as the MCP principal. All tools call the service layer
except ``strategy_export``, which reads the strategy's YAML text directly (no service
export function exists; the REST export route reads the same file).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.services import strategies as strategy_svc
from hydra.services.errors import ServiceError, service_error
from hydra.services.strategies import (
    RiskInput,
    RuleSetInput,
    SaveRequest,
    TimeframeInput,
)

_DEFAULT_TIMEFRAMES = TimeframeInput()
_DEFAULT_RISK = RiskInput()


def _save_request(
    name: str,
    rules: RuleSetInput,
    description: str,
    exchange_id: str,
    symbol: str,
    timeframes: TimeframeInput,
    risk: RiskInput,
    enable_immediately: bool,
    ml_overlay: dict[str, Any] | None,
    schedule: dict[str, Any] | None,
    parameters: dict[str, int | float] | None,
    leverage: int,
) -> SaveRequest:
    """The service's save payload, built from the tool's mirrored fields."""
    return SaveRequest(
        name=name,
        description=description,
        exchange_id=exchange_id,
        symbol=symbol,
        rules=rules,
        timeframes=timeframes,
        risk=risk,
        enable_immediately=enable_immediately,
        ml_overlay=ml_overlay,
        schedule=schedule,
        parameters=parameters,
        leverage=leverage,
    )


@server.tool(name="strategy_list", structured_output=False)
async def strategy_list() -> str:
    """List every strategy config (YAML) with its enabled state and paper performance."""
    try:
        return _deps.json_text(await strategy_svc.list_strategies(_deps.db_pool()))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="strategy_get", structured_output=False)
async def strategy_get(strategy_id: str) -> str:
    """Read one strategy in full: rules, timeframes, risk, symbols, config hash, running
    sessions and whether the file changed under them, plus paper performance."""
    try:
        return _deps.json_text(await strategy_svc.get_strategy(_deps.db_pool(), strategy_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="strategy_save", structured_output=False)
async def strategy_save(
    name: str,
    rules: RuleSetInput,
    description: str = "",
    exchange_id: str = "binance",
    symbol: str = "BTCUSDT",
    timeframes: TimeframeInput = _DEFAULT_TIMEFRAMES,
    risk: RiskInput = _DEFAULT_RISK,
    enable_immediately: bool = False,
    ml_overlay: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    parameters: dict[str, int | float] | None = None,
    leverage: int = 1,
) -> str:
    """Validate and save a new strategy as a YAML config file; returns its new id.

    ``rules`` is the builder's rule set (entry/exit long/short condition groups);
    ``enable_immediately`` also marks it enabled for paper sessions. ``schedule``
    (``active_hours_utc`` 0-23, ``active_days`` 0-6 with Monday=0) limits when entries
    may open, checked on the primary bar's open time; exits always run. ``parameters``
    sets top-level tunables by name; only names the rules bind are accepted (see
    ``strategy_params``) and the indicator ranges apply. ``symbol``
    must be a member of the current top-100 universe (``universe_list``): a
    non-member is a 422 ``symbol_not_in_universe`` naming the ranking's
    ``ranked_at`` (running sessions and stored strategies are unaffected).
    ``leverage`` (1-3, isolated margin) applies to ``binanceusdm`` perp strategies
    only; any other venue with ``leverage != 1`` is a 422.
    """
    try:
        body = _save_request(
            name,
            rules,
            description,
            exchange_id,
            symbol,
            timeframes,
            risk,
            enable_immediately,
            ml_overlay,
            schedule,
            parameters,
            leverage,
        )
        result = await strategy_svc.save_strategy(_deps.db_pool(), body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(_deps.MCP_PRINCIPAL, "strategy_save", strategy_id=result.id, name=result.name)
    return _deps.json_text(result.model_dump(mode="json"))


@server.tool(name="strategy_update", structured_output=False)
async def strategy_update(
    strategy_id: str,
    name: str,
    rules: RuleSetInput,
    description: str = "",
    exchange_id: str = "binance",
    symbol: str = "BTCUSDT",
    timeframes: TimeframeInput = _DEFAULT_TIMEFRAMES,
    risk: RiskInput = _DEFAULT_RISK,
    enable_immediately: bool = False,
    ml_overlay: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    parameters: dict[str, int | float] | None = None,
    leverage: int = 1,
) -> str:
    """Update an existing rule-based strategy's YAML config; only rule-based strategies
    can be edited (400 otherwise). Omitting ``schedule`` keeps the stored one; ``{}``
    clears it. Omitting ``parameters`` keeps the stored tunables of names the rules still
    bind; a dict replaces them (``{}`` clears) and may only name bound parameters.
    ``symbol`` must be a member of the current top-100 universe (``universe_list``): a
    non-member is a 422 ``symbol_not_in_universe`` naming the ranking's ``ranked_at`` (the
    running sessions and the stored file are unaffected). ``leverage`` (1-3) applies to
    ``binanceusdm`` perp strategies only; like every other field it is replaced, so an
    update that omits it writes 1x."""
    try:
        body = _save_request(
            name,
            rules,
            description,
            exchange_id,
            symbol,
            timeframes,
            risk,
            enable_immediately,
            ml_overlay,
            schedule,
            parameters,
            leverage,
        )
        result = await strategy_svc.update_strategy(_deps.db_pool(), strategy_id, body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(_deps.MCP_PRINCIPAL, "strategy_update", strategy_id=strategy_id, name=result.name)
    return _deps.json_text(result.model_dump(mode="json"))


@server.tool(name="strategy_delete", structured_output=False)
async def strategy_delete(strategy_id: str) -> str:
    """Delete a saved strategy's YAML file. Running sessions keep their snapshot."""
    try:
        strategy_svc.delete_strategy(strategy_id)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    audit(_deps.MCP_PRINCIPAL, "strategy_delete", strategy_id=strategy_id)
    return _deps.json_text({"id": strategy_id, "status": "deleted"})


@server.tool(name="strategy_toggle", structured_output=False)
async def strategy_toggle(strategy_id: str) -> str:
    """Flip a strategy's enabled flag; returns the new state."""
    try:
        result = strategy_svc.toggle_strategy(strategy_id)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "strategy_toggle",
        strategy_id=strategy_id,
        enabled=result["enabled"],
    )
    return _deps.json_text(result)


@server.tool(name="strategy_reload", structured_output=False)
async def strategy_reload(strategy_id: str) -> str:
    """Apply the strategy file's changed parameters to its running paper session at the
    next closed bar.

    Changes to rules, risk, sizing, symbols, timeframes or the class cannot be hot-
    reloaded: the call fails 409 ``restart_required`` with the ``changed_fields``
    list and nothing is applied.
    """
    try:
        result = await _deps.trader().reload_strategy(strategy_id)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "strategy_reload",
        strategy_id=strategy_id,
        session_id=result["session_id"],
    )
    return _deps.json_text(result)


@server.tool(name="strategy_import", structured_output=False)
async def strategy_import(filename: str, yaml_content: str) -> str:
    """Import a strategy from YAML text; ``filename`` must end in .yaml or .yml. The
    imported config gets a fresh unique id; returns the new id."""
    try:
        result = strategy_svc.import_strategy(filename, yaml_content.encode("utf-8"))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    audit(_deps.MCP_PRINCIPAL, "strategy_import", strategy_id=result.id, name=result.name)
    return _deps.json_text(result.model_dump(mode="json"))


def _export_not_found(strategy_id: str) -> ServiceError:
    return service_error(
        404,
        "strategy_not_found",
        f"Strategy {strategy_id} not found",
        "not_found",
        strategy_id=strategy_id,
    )


@server.tool(name="strategy_export", structured_output=False)
async def strategy_export(strategy_id: str) -> str:
    """Read a strategy's YAML config file and return its full text."""
    path = strategy_svc.find_strategy_file(strategy_id)
    if path is None:
        raise _deps.tool_error(_export_not_found(strategy_id))
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        # the file can vanish between the finder's scan and this read
        raise _deps.tool_error(_export_not_found(strategy_id)) from exc
    except OSError as exc:
        raise _deps.tool_error(
            service_error(
                503,
                "strategy_file_unreadable",
                f"Strategy {strategy_id} file is unreadable: {exc}",
                "unavailable",
                strategy_id=strategy_id,
            )
        ) from exc
