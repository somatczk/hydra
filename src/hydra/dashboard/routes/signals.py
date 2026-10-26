"""Webhook endpoint for external trading signals (TradingView-style JSON).

The route sits behind the API auth middleware, and the body must also carry the configured
``webhook_secret``: with no secret configured the webhook is disabled (503), a wrong one is 401.
Orders go through the trader backend's ``webhook_signal`` (risk-checked, protective stop on
the entry fill; the webhook origin is the live-refusal seam). An entry without ``quantity``
is sized like a strategy entry; exits are reduce-only and default to the whole position.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from hydra.dashboard.routes._db import ServiceErrorRoute, api_error, request_trader, unauthenticated
from hydra.dashboard.security import audit
from hydra.services.errors import ServiceError

router = APIRouter(prefix="/api/signals", tags=["signals"], route_class=ServiceErrorRoute)
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

    try:
        executed = await request_trader(request).webhook_signal(
            symbol=body.symbol,
            side=body.side,
            action=body.action,
            quantity=body.quantity,
            strategy_id=body.strategy_id,
        )
    except ServiceError:
        raise
    except Exception as exc:
        logger.exception("Webhook signal %s failed", signal_id)
        raise api_error(
            502,
            "execution_failed",
            f"Signal {signal_id} failed on the session of '{body.strategy_id}'",
            "unavailable",
        ) from exc
    order_count = 1 if executed["order_id"] is not None else 0
    audit(
        getattr(request.state, "principal", None),
        "manual_order",
        source="webhook",
        signal_id=signal_id,
        session_id=executed["session_id"],
        strategy_id=body.strategy_id,
        symbol=body.symbol,
        side=body.side,
        signal_action=body.action,
        quantity=None if body.quantity is None else str(body.quantity),
        order_id=executed["order_id"],
    )
    logger.info(
        "Webhook signal %s: %s %s %s on session %s -> %s",
        signal_id,
        body.action,
        body.side,
        body.symbol,
        executed["session_id"],
        executed["status"],
    )

    return {
        "status": "accepted",
        "signal_id": signal_id,
        "message": f"Signal executed on {order_count} session(s) ({executed['status']})",
    }
