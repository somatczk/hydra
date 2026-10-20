"""Webhook endpoint for external trading signals (TradingView-style JSON).

The route sits behind the API auth middleware, and the body must also carry the configured
``webhook_secret``: with no secret configured the webhook is disabled (503), a wrong one is 401.
Entries need an explicit ``quantity``; exits default to closing the open position.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from decimal import Decimal
from typing import Any, Self

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

from hydra.dashboard.routes._db import api_error, unauthenticated

router = APIRouter(prefix="/api/signals", tags=["signals"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class WebhookSignal(BaseModel):
    """Incoming webhook signal (TradingView format)."""

    symbol: str = "BTCUSDT"
    side: str = Field(..., pattern="^(buy|sell)$")
    action: str = Field("entry", pattern="^(entry|exit)$")
    strategy_id: str = Field(..., min_length=1)
    secret: str = ""
    quantity: Decimal | None = Field(None, gt=0, max_digits=18, decimal_places=8)

    @model_validator(mode="after")
    def _entry_needs_quantity(self) -> Self:
        if self.action == "entry" and self.quantity is None:
            raise ValueError("quantity is required for entry signals")
        return self


class WebhookResponse(BaseModel):
    status: str
    signal_id: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_webhook_secret(request: Request) -> str:
    """Read webhook_secret from system config; empty when none is configured."""
    cfg = getattr(request.app.state, "system_config", None) or {}
    secret = cfg.get("webhook_secret")
    return secret if isinstance(secret, str) else ""


def _require_webhook_secret(request: Request, presented: str) -> None:
    """Fail closed: no configured secret disables the webhook; compare in constant time."""
    expected = _get_webhook_secret(request)
    if not expected:
        raise api_error(
            503,
            "webhook_disabled",
            "Signal webhook is disabled: set a webhook secret in Settings",
            "unavailable",
        )
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise unauthenticated("invalid_webhook_secret", "Invalid webhook secret")


def _get_session_manager(request: Request) -> Any:
    return getattr(request.app.state, "session_manager", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/webhook", response_model=WebhookResponse, status_code=200)
async def receive_webhook(body: WebhookSignal, request: Request) -> dict[str, Any]:
    """Receive a trading signal via webhook (TradingView, custom, etc).

    The signal goes only to the running session(s) of ``strategy_id``; there is no symbol fan-out.
    """
    _require_webhook_secret(request, body.secret)

    signal_id = f"sig-{uuid.uuid4().hex[:8]}"

    mgr = _get_session_manager(request)
    if mgr is None:
        raise api_error(503, "engine_not_ready", "Trading engine not initialized", "unavailable")

    sessions = mgr.list_sessions()
    running = [s for s in sessions if s.status == "running"]

    targets = [s for s in running if s.strategy_id == body.strategy_id]
    if not targets:
        raise api_error(
            404,
            "session_not_found",
            f"No running session for strategy_id '{body.strategy_id}'",
            "not_found",
        )

    executed = 0
    failed = 0
    for session in targets:
        executor = session._executor
        if executor is None:
            continue

        try:
            if body.action == "exit":
                positions = await executor.fetch_positions(body.symbol)
                if not positions:
                    logger.info(
                        "Webhook signal: no position to exit for %s in session %s",
                        body.symbol,
                        session.session_id,
                    )
                    continue
                pos = positions[0]
                qty = (
                    body.quantity
                    if body.quantity is not None
                    else Decimal(str(pos.get("contracts", pos.get("quantity", 0))))
                )
                close_side = "SELL" if pos.get("side", "").lower() in ("long", "buy") else "BUY"
                await executor.create_order(
                    symbol=body.symbol, side=close_side, order_type="MARKET", quantity=qty
                )
            else:
                await executor.create_order(
                    symbol=body.symbol,
                    side="BUY" if body.side == "buy" else "SELL",
                    order_type="MARKET",
                    quantity=body.quantity,
                )

            executed += 1
            logger.info(
                "Webhook signal %s executed: %s %s %s on session %s",
                signal_id,
                body.action,
                body.side,
                body.symbol,
                session.session_id,
            )
        except Exception:
            failed += 1
            logger.exception(
                "Failed to execute webhook signal %s on session %s",
                signal_id,
                session.session_id,
            )

    if executed == 0:
        if failed:
            raise api_error(
                502,
                "execution_failed",
                f"Signal {signal_id} failed on every session of '{body.strategy_id}'",
                "unavailable",
            )
        if body.action == "exit":
            raise api_error(
                409, "no_position", f"No open {body.symbol} position to exit", "no_data"
            )
        raise api_error(
            503, "executor_not_ready", "No session executor is ready for this signal", "unavailable"
        )

    return {
        "status": "accepted",
        "signal_id": signal_id,
        "message": f"Signal executed on {executed} session(s)",
    }
