"""Trading session, kill-switch and quick-trade API routes.

Every order goes through ``SessionManager.submit_manual_order`` (OrderManager -> RiskService
-> paper executor); the bodies call the engine's ``TraderBackend`` (``app.state.trader``) —
the same operations the MCP tools call — so both transports answer identical envelopes.
Errors use the ``_db.py`` envelope.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hydra.dashboard.routes._db import (
    ServiceErrorRoute,
    request_pool,
    request_trader,
)
from hydra.dashboard.security import audit
from hydra.services.live_gate import live_gate_required, session_trading_mode
from hydra.services.sessions import (
    KillSwitchNotPersistedError,
    QuickTradeRequest,
    SessionStopNotPersistedError,
    StartSessionRequest,
    activate_kill_switch_fail_safe,
    release_kill_switch_fail_safe,
)

router = APIRouter(prefix="/api/trading", tags=["trading"], route_class=ServiceErrorRoute)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    session_id: str
    strategy_id: str
    strategy_name: str | None = None
    trading_mode: str
    status: str
    exchange_id: str
    symbols: list[str]
    timeframe: str
    paper_capital: float | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    error_message: str | None = None
    positions_at_stop: int = 0
    flatten_incomplete: bool = False
    persist_error: str | None = None
    last_bar_at: str | None = None
    entries_paused: str | None = None
    persist_dirty: bool | None = None
    feed_live: bool | None = None
    created_by: str | None = None
    reconcile_state: str | None = None


class ExchangeStopState(BaseModel):
    """The A8-live exchange-side stop state of one position (pinned seam #3).

    ``'Stop at {stop_price} on the exchange'`` only when ``state == 'confirmed'``; danger
    copy ``'No exchange stop for {symbol}'`` on ``'missing'``; ``'unknown'`` shows as such.
    """

    state: Literal["confirmed", "missing", "unknown"]
    kind: str | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    order_list_id: str | None = None
    checked_at: str
    error: str | None = None


class PositionItem(BaseModel):
    symbol: str
    direction: str
    quantity: float
    avg_entry_price: float
    unrealized_pnl: float
    exchange_stop: ExchangeStopState | None = None


class TradeItem(BaseModel, extra="allow"):
    id: str | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: float = 0
    price: float = 0
    fee: float = 0
    pnl: float = 0
    net_pnl: float | None = None
    timestamp: str | None = None
    # R20: true once an ok|mismatch reconcile pass covered this fill; None for paper fills
    reconciled: bool | None = None

    @field_validator("fee", mode="before")
    @classmethod
    def _normalize_fee(cls, v: Any) -> float:
        if isinstance(v, dict):
            return float(v.get("cost", 0))
        return float(v) if v is not None else 0.0


class OpenOrderItem(BaseModel, extra="allow"):
    id: str
    symbol: str
    side: str
    type: str
    amount: float
    price: float | None
    stop_price: float | None
    reason: str
    protective: bool | None


class LiveOrderItem(BaseModel, extra="allow"):
    """One tracked live order row, the pinned seam #2 shape (newest first, cap 100).

    ``status`` is the seam's vocabulary: pending_submit|submitting|new|partially_filled|
    filled|canceled|expired|rejected|unknown; ``exchange_order_id`` is the orderListId
    for an OCO; ``error`` carries a rejection or resolution reason, else null.
    """

    client_order_id: str
    exchange_order_id: str | None = None
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    filled_quantity: float = 0
    average_fill_price: float | None = None
    status: Literal[
        "pending_submit",
        "submitting",
        "new",
        "partially_filled",
        "filled",
        "canceled",
        "expired",
        "rejected",
        "unknown",
    ]
    tag: str = ""
    actor: str = ""
    submitted_at: str | None = None
    terminal_at: str | None = None
    error: str | None = None


class ReconcileStatus(BaseModel):
    """A live session's reconcile state (pinned seam #2); None on a paper session.

    A mismatch carries its findings in ``detail`` verbatim; ``unknown`` means the pass
    could not read the exchange (fail-closed); ``never`` means no pass has run yet.
    """

    state: Literal["ok", "mismatch", "unknown", "never"]
    last_reconciled_at: str | None = None
    detail: str | None = None


class SessionMetrics(BaseModel):
    balance: dict[str, float] = Field(default_factory=dict)
    total_pnl: float = 0
    win_rate: float = 0
    total_trades: int = 0
    open_positions: int = 0
    equity: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    fees: float | None = None
    drawdown: float | None = None


class SessionDetailResponse(BaseModel):
    session_id: str
    strategy_id: str
    strategy_name: str | None = None
    trading_mode: str
    status: str
    exchange_id: str
    symbols: list[str]
    timeframe: str
    paper_capital: float | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    error_message: str | None = None
    positions_at_stop: int = 0
    flatten_incomplete: bool = False
    last_bar_at: str | None = None
    entries_paused: str | None = None
    protection_failures: list[str] = Field(default_factory=list)
    persist_error: str | None = None
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
    positions: list[PositionItem] = Field(default_factory=list)
    trades: list[TradeItem] = Field(default_factory=list)
    open_orders: list[OpenOrderItem] = Field(default_factory=list)
    created_by: str | None = None
    # A7 (live sessions): the tracked order rows + reconcile; paper answers the defaults
    orders: list[LiveOrderItem] = Field(default_factory=list)
    reconcile: ReconcileStatus | None = None


class KillSwitchResponse(BaseModel):
    active: bool
    message: str
    sessions_stopped: int = 0
    positions_remaining: int = 0
    persisted: bool = True
    flatten_failures: list[str] = Field(default_factory=list)


class KillSwitchRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["RELEASE"]
    reason: str = Field(..., min_length=3, max_length=500)


class QuickTradeResponse(BaseModel):
    order_id: str | None
    session_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float | None = None
    status: str
    protection: str
    protective_order_id: str | None = None
    exit_orders_deferred: bool = False
    fill_price: float | None = None
    mark_source: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    name = getattr(principal, "name", None)
    kind = getattr(principal, "kind", None)
    if name:
        return f"{kind}:{name}" if kind else str(name)
    return "anonymous"


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def start_session(body: StartSessionRequest, request: Request) -> dict[str, Any]:
    """Start a paper session. Live is refused here (409 ``live_gate_required``): the
    code-gated ``POST /api/live/session-start`` ceremony is the only live path; only
    Binance SPOT is accepted."""
    if body.trading_mode == "live":
        raise live_gate_required("POST /api/live/session-start")
    d = await request_trader(request).start_session(_actor(request), body)
    audit(
        getattr(request.state, "principal", None),
        "session_start",
        session_id=d["session_id"],
        strategy_id=body.strategy_id,
    )
    return d


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    """Flatten (reduce-only) and stop a session; positions_at_stop reports what is left."""
    try:
        d = await request_trader(request).stop_session(session_id)
    except SessionStopNotPersistedError:
        audit(
            getattr(request.state, "principal", None),
            "session_stop",
            session_id=session_id,
            persisted=False,
        )
        raise
    audit(getattr(request.state, "principal", None), "session_stop", session_id=session_id)
    return d


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    """List all trading sessions (running + recent stopped) with each one's health."""
    return await request_trader(request).list_sessions()


@router.get("/sessions/{session_id}/detail", response_model=SessionDetailResponse)
async def get_session_detail(session_id: str, request: Request) -> dict[str, Any]:
    """Full session detail with account numbers, positions and fills."""
    return await request_trader(request).session_detail(session_id)


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------


@router.post("/kill-switch", response_model=KillSwitchResponse)
async def activate_kill_switch(request: Request) -> dict[str, Any]:
    """Latch the kill switch, then flatten and stop every running or errored session.

    In the split layout an unreachable trader falls back to the DB latch (R7): the
    switch is latched durably, the answer is an honest 503 and the trader applies it
    within its 60 s cleanup tick once it returns.
    """
    try:
        body = await activate_kill_switch_fail_safe(
            request_trader(request), request_pool(request), _actor(request), "kill switch (api)"
        )
    except KillSwitchNotPersistedError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        audit(
            getattr(request.state, "principal", None),
            "kill_switch_on",
            sessions_stopped=detail.get("sessions_stopped"),
            positions_remaining=detail.get("positions_remaining"),
            persisted=detail.get("persisted"),
        )
        raise
    audit(
        getattr(request.state, "principal", None),
        "kill_switch_on",
        sessions_stopped=body["sessions_stopped"],
        positions_remaining=body["positions_remaining"],
        persisted=body["persisted"],
    )
    return body


@router.delete("/kill-switch", response_model=KillSwitchResponse)
async def release_kill_switch(body: KillSwitchRelease, request: Request) -> dict[str, Any]:
    """Release the kill switch (``{"confirm": "RELEASE", "reason": ...}``).

    In the split layout an unreachable trader keeps the switch latched: the honest 503
    says so (R30/A7) instead of implying the release might have landed.
    """
    d = await release_kill_switch_fail_safe(request_trader(request), _actor(request), body.reason)
    audit(getattr(request.state, "principal", None), "kill_switch_off", reason=body.reason)
    return d


# ---------------------------------------------------------------------------
# Quick-trade endpoint
# ---------------------------------------------------------------------------


@router.post("/quick-trade", response_model=QuickTradeResponse)
async def quick_trade(body: QuickTradeRequest, request: Request) -> dict[str, Any]:
    """A risk-checked manual order on the running session ``session_id``.

    SL/TP (or a trailing stop) are placed reduce-only when the entry fills; without them the
    strategy's protective stop (default 5 %) is placed. A SELL only reduces the held
    position. A live session is refused (409 ``live_gate_required``): the code-gated
    ``POST /api/live/order`` ceremony is the only manual live-order path.
    """
    backend = request_trader(request)
    if await session_trading_mode(backend, body.session_id) == "live":
        raise live_gate_required("POST /api/live/order")
    d = await backend.quick_trade(body)
    audit(
        getattr(request.state, "principal", None),
        "manual_order",
        session_id=d["session_id"],
        symbol=body.symbol,
        side=body.side,
        order_type=body.order_type,
        quantity=str(body.quantity),
        order_id=d["order_id"],
    )
    return d
