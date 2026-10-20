"""Webhook endpoint for external trading signals (TradingView-style JSON).

The route sits behind the API auth middleware, and the body must also carry the configured
``webhook_secret``: with no secret configured the webhook is disabled (503), a wrong one is 401.
Orders go through ``SessionManager.submit_manual_order`` (risk-checked, protective stop on the
entry fill). An entry without ``quantity`` is sized like a strategy entry; exits are reduce-only
and default to the whole position.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from hydra.core.types import OrderType, Side
from hydra.dashboard.routes._db import api_error, unauthenticated
from hydra.execution.errors import SessionNotRunningError, SpotShortError
from hydra.execution.order_manager import RiskRejectedError

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

    is_exit = body.action == "exit"
    if not is_exit and body.side == "sell":
        raise api_error(
            422, "spot_short", "Short entries are not supported on spot sessions", "invalid"
        )
    try:
        session, result = await mgr.submit_manual_order(
            symbol=body.symbol,
            side=Side.SELL if is_exit else Side.BUY,
            order_type=OrderType.MARKET,
            quantity=body.quantity,
            strategy_id=body.strategy_id,
        )
    except SessionNotRunningError as exc:
        raise api_error(
            404,
            "session_not_found",
            f"No running session for strategy_id '{body.strategy_id}'",
            "not_found",
        ) from exc
    except SpotShortError as exc:
        if is_exit:
            raise api_error(
                409, "no_position", f"No open {body.symbol} position to exit", "no_data"
            ) from exc
        raise api_error(422, exc.code, str(exc), "invalid") from exc
    except RiskRejectedError as exc:
        raise api_error(
            422, "risk_rejected", exc.reason, "invalid", check=exc.check, intent=exc.intent
        ) from exc
    except ValueError as exc:
        raise api_error(422, "order_rejected", str(exc), "invalid") from exc
    except Exception as exc:
        logger.exception("Webhook signal %s failed", signal_id)
        raise api_error(
            502,
            "execution_failed",
            f"Signal {signal_id} failed on the session of '{body.strategy_id}'",
            "unavailable",
        ) from exc
    executed = 1 if result.order_id is not None else 0
    logger.info(
        "Webhook signal %s: %s %s %s on session %s -> %s",
        signal_id,
        body.action,
        body.side,
        body.symbol,
        session.session_id,
        result.status,
    )

    return {
        "status": "accepted",
        "signal_id": signal_id,
        "message": f"Signal executed on {executed} session(s) ({result.status})",
    }
