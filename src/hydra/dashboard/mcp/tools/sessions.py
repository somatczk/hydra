"""Session tools (list, detail, start, stop, quick_trade).

Five tools registered on the shared server instance, each a thin call into the engine's
TraderBackend (the same operations the REST routes call). The request models are
constructed inside the tool so pydantic revalidates and no schema drift is possible;
money arrives as strings (``quantity``, ``price``, ``paper_capital``) and is coerced to
Decimal by the model. Sessions are paper-only here — there is no ``trading_mode`` input
and live is refused server-side at session start. Every ``ServiceError`` surfaces as a
``ToolError`` carrying the full error envelope verbatim, and every mutating tool writes a
``hydra.audit`` line as the MCP principal.
"""

from __future__ import annotations

from pydantic import ValidationError

from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.services.approvals import APPROVAL_ACTOR
from hydra.services.errors import ServiceError
from hydra.services.sessions import (
    QuickTradeRequest,
    SessionStopNotPersistedError,
    StartSessionRequest,
)


@server.tool(name="session_list", structured_output=False)
async def session_list() -> str:
    """List every trading session (running + recent stopped) with status, health fields
    (last bar, feed, entries paused) and who started it (``created_by``)."""
    try:
        return _deps.json_text(await _deps.trader().list_sessions())
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="session_detail", structured_output=False)
async def session_detail(session_id: str) -> str:
    """Read one session in full: account numbers, metrics, positions, fills, resting
    orders and who started it."""
    try:
        return _deps.json_text(await _deps.trader().session_detail(session_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="session_start", structured_output=False)
async def session_start(strategy_id: str, paper_capital: str | None = None) -> str:
    """Start a paper session for a strategy.

    ``paper_capital`` is a decimal string allocated from the paper account; omit it to
    use whatever is available. Live trading does not exist here: sessions are paper-only
    and live is refused server-side. Returns the session (status 'running').
    """
    try:
        body = StartSessionRequest(
            strategy_id=strategy_id, trading_mode="paper", paper_capital=paper_capital
        )
        result = await _deps.trader().start_session(APPROVAL_ACTOR, body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "session_start",
        session_id=result["session_id"],
        strategy_id=strategy_id,
    )
    return _deps.json_text(result)


@server.tool(name="session_stop", structured_output=False)
async def session_stop(session_id: str) -> str:
    """Flatten (reduce-only) and stop a session. ``positions_at_stop`` reports anything
    that could not be closed."""
    try:
        result = await _deps.trader().stop_session(session_id)
    except ServiceError as exc:
        if isinstance(exc, SessionStopNotPersistedError):
            audit(_deps.MCP_PRINCIPAL, "session_stop", session_id=session_id, persisted=False)
        raise _deps.tool_error(exc) from exc
    audit(_deps.MCP_PRINCIPAL, "session_stop", session_id=session_id)
    return _deps.json_text(result)


@server.tool(name="quick_trade", structured_output=False)
async def quick_trade(
    session_id: str,
    side: str,
    quantity: str,
    symbol: str = "BTCUSDT",
    order_type: str = "MARKET",
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    trailing_stop_pct: str | None = None,
) -> str:
    """Place a risk-checked manual paper order on a running session.

    ``quantity``/``price``/``take_profit``/``stop_loss``/``trailing_stop_pct`` are decimal
    strings. SL/TP (or a trailing stop) are placed reduce-only when the entry fills;
    without them the strategy's protective stop is placed. A SELL only reduces the held
    position. The order is refused by the same pre-trade checks the REST route applies.
    """
    try:
        body = QuickTradeRequest(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            trailing_stop_pct=trailing_stop_pct,
            session_id=session_id,
        )
        result = await _deps.trader().quick_trade(body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "manual_order",
        session_id=result["session_id"],
        symbol=body.symbol,
        side=body.side,
        order_type=body.order_type,
        quantity=str(body.quantity),
        order_id=result["order_id"],
    )
    return _deps.json_text(result)
