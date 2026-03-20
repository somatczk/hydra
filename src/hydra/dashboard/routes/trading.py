"""Trading session and kill-switch API routes."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from hydra.dashboard.routes.strategies import build_strategy_name_map

router = APIRouter(prefix="/api/trading", tags=["trading"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StartSessionRequest(BaseModel):
    strategy_id: str
    trading_mode: str = "paper"  # 'paper' | 'live'
    paper_capital: float | None = Field(None, ge=0)


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


class PositionItem(BaseModel):
    symbol: str
    direction: str
    quantity: float
    avg_entry_price: float
    unrealized_pnl: float


class TradeItem(BaseModel, extra="allow"):
    id: str | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: float = 0
    price: float = 0
    fee: float = 0
    pnl: float = 0
    timestamp: str | None = None


class SessionMetrics(BaseModel):
    balance: dict[str, float] = Field(default_factory=dict)
    total_pnl: float = 0
    win_rate: float = 0
    total_trades: int = 0
    open_positions: int = 0


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
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
    positions: list[PositionItem] = Field(default_factory=list)
    trades: list[TradeItem] = Field(default_factory=list)


class KillSwitchResponse(BaseModel):
    active: bool
    message: str
    sessions_stopped: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_manager(request: Request) -> Any:
    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized",
        )
    return mgr


def _session_to_dict(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "strategy_id": session.strategy_id,
        "trading_mode": session.trading_mode,
        "status": session.status,
        "exchange_id": session.exchange_id,
        "symbols": session.symbols,
        "timeframe": session.timeframe,
        "paper_capital": float(session.paper_capital) if session.paper_capital else None,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
        "error_message": session.error_message,
    }


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def start_session(body: StartSessionRequest, request: Request) -> dict[str, Any]:
    """Start a new trading session for a strategy."""
    mgr = _get_session_manager(request)

    if body.trading_mode not in ("paper", "live"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="trading_mode must be 'paper' or 'live'",
        )

    capital = Decimal(str(body.paper_capital)) if body.paper_capital else None

    # Load exchange credentials for live sessions
    credentials = None
    if body.trading_mode == "live":
        creds_store = getattr(request.app.state, "exchange_credentials", {})
        # Strategy config determines the exchange; pass all available credentials
        credentials = creds_store

    try:
        session_id = await mgr.start_session(
            strategy_id=body.strategy_id,
            trading_mode=body.trading_mode,
            paper_capital=capital,
            credentials=credentials,
        )
    except RuntimeError as exc:
        # Kill switch active
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    session = mgr.get_session(session_id)
    d = _session_to_dict(session)
    name_map = build_strategy_name_map()
    d["strategy_name"] = name_map.get(session.strategy_id)
    return d


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    """Stop a running trading session."""
    mgr = _get_session_manager(request)

    try:
        await mgr.stop_session(session_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    session = mgr.get_session(session_id)
    d = _session_to_dict(session)
    name_map = build_strategy_name_map()
    d["strategy_name"] = name_map.get(session.strategy_id)
    return d


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    """List all trading sessions (running + recent stopped)."""
    mgr = _get_session_manager(request)
    name_map = build_strategy_name_map()
    results = []
    for s in mgr.list_sessions():
        d = _session_to_dict(s)
        d["strategy_name"] = name_map.get(s.strategy_id)
        results.append(d)
    return results


@router.get("/sessions/{session_id}/detail", response_model=SessionDetailResponse)
async def get_session_detail(session_id: str, request: Request) -> dict[str, Any]:
    """Full session detail with runtime metrics, positions and trades."""
    mgr = _get_session_manager(request)

    try:
        detail = await mgr.get_session_detail(session_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    name_map = build_strategy_name_map()
    detail["strategy_name"] = name_map.get(detail.get("strategy_id", ""))
    return detail


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------


@router.post("/kill-switch", response_model=KillSwitchResponse)
async def activate_kill_switch(request: Request) -> dict[str, Any]:
    """Emergency: halt ALL trading sessions and set the kill switch flag."""
    mgr = _get_session_manager(request)
    running_count = sum(1 for s in mgr.list_sessions() if s.status == "running")
    await mgr.stop_all()
    return {
        "active": True,
        "message": "Kill switch activated — all trading halted",
        "sessions_stopped": running_count,
    }


@router.delete("/kill-switch", response_model=KillSwitchResponse)
async def release_kill_switch(request: Request) -> dict[str, Any]:
    """Release the kill switch so new sessions can start."""
    mgr = _get_session_manager(request)
    await mgr.release_kill_switch()
    return {
        "active": False,
        "message": "Kill switch released — trading can resume",
        "sessions_stopped": 0,
    }
