"""Webhook endpoints for receiving external trading signals.

Supports TradingView alerts and custom JSON payloads. Signals are
routed to running trading sessions for immediate execution.
"""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/signals", tags=["signals"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class WebhookSignal(BaseModel):
    """Incoming webhook signal (TradingView format)."""

    symbol: str = "BTCUSDT"
    side: str = Field(..., pattern="^(buy|sell)$")  # buy or sell
    action: str = Field("entry", pattern="^(entry|exit)$")
    strategy_id: str = ""
    secret: str = ""
    price: float | None = None
    quantity: float | None = None


class WebhookResponse(BaseModel):
    status: str
    signal_id: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_webhook_secret(request: Request) -> str:
    """Read webhook_secret from system config."""
    cfg = getattr(request.app.state, "system_config", None) or {}
    return str(cfg.get("webhook_secret", ""))


def _get_session_manager(request: Request) -> Any:
    return getattr(request.app.state, "session_manager", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/webhook", response_model=WebhookResponse, status_code=200)
async def receive_webhook(body: WebhookSignal, request: Request) -> dict[str, Any]:
    """Receive a trading signal via webhook (TradingView, custom, etc).

    The signal is routed to the running session for the given strategy_id.
    If no strategy_id is provided, the signal is sent to all running sessions
    matching the symbol.
    """
    # Validate secret
    expected_secret = _get_webhook_secret(request)
    if expected_secret and body.secret != expected_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    signal_id = f"sig-{uuid.uuid4().hex[:8]}"

    mgr = _get_session_manager(request)
    if mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trading engine not initialized",
        )

    # Find running session(s) to route the signal to
    sessions = mgr.list_sessions()
    running = [s for s in sessions if s.status == "running"]

    if body.strategy_id:
        targets = [s for s in running if s.strategy_id == body.strategy_id]
    else:
        targets = [s for s in running if body.symbol in s.symbols]

    if not targets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No running session found for strategy_id='{body.strategy_id}' "
            f"or symbol='{body.symbol}'",
        )

    # Execute signal on each matching session
    executed = 0
    for session in targets:
        executor = session._executor
        if executor is None:
            continue

        try:
            side = "BUY" if body.side == "buy" else "SELL"

            if body.action == "exit":
                # Exit: close the position for this symbol
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
                    Decimal(str(body.quantity))
                    if body.quantity
                    else Decimal(str(pos.get("contracts", pos.get("quantity", 0))))
                )
                # Flip the side for closing
                close_side = "SELL" if pos.get("side", "").lower() in ("long", "buy") else "BUY"
                await executor.create_order(
                    symbol=body.symbol,
                    side=close_side,
                    order_type="MARKET",
                    quantity=qty,
                )
            else:
                # Entry: auto-compute quantity from session balance if missing
                if body.quantity is None:
                    try:
                        balance = await executor.get_balance()
                        bal_usdt = balance.get("USDT", Decimal("0"))
                        price = await executor.get_last_price(body.symbol)
                        if bal_usdt > 0 and price > 0:
                            body.quantity = float(
                                Decimal(str(bal_usdt)) * Decimal("0.01") / Decimal(str(price))
                            )
                    except Exception:
                        logger.debug("Could not auto-compute quantity for %s", body.symbol)
                if body.quantity is None:
                    raise HTTPException(
                        status_code=422,
                        detail="quantity is required for entry signals",
                    )
                await executor.create_order(
                    symbol=body.symbol,
                    side=side,
                    order_type="MARKET",
                    quantity=Decimal(str(body.quantity)),
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
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "Failed to execute webhook signal %s on session %s",
                signal_id,
                session.session_id,
            )

    if executed == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Signal received but could not be executed on any session",
        )

    return {
        "status": "accepted",
        "signal_id": signal_id,
        "message": f"Signal executed on {executed} session(s)",
    }


# ---------------------------------------------------------------------------
# Telegram signal format parser
# ---------------------------------------------------------------------------

_TELEGRAM_PATTERN = re.compile(
    r"(BUY|SELL|LONG|SHORT)"  # side
    r"[ \t]+"  # required whitespace (no \s to avoid matching newlines)
    r"(?:(\d+(?:\.\d+)?)[ \t]+)?"  # optional quantity
    r"([A-Za-z0-9]+)"  # symbol (explicit charset, no \w backtrack)
    r"(?:[ \t]*@[ \t]*(\d+(?:\.\d+)?))?"  # optional @ price
    r"(?:[ \t]+TP[ \t]*(\d+(?:\.\d+)?))?"  # optional TP
    r"(?:[ \t]+SL[ \t]*(\d+(?:\.\d+)?))?",  # optional SL
    re.IGNORECASE,
)


class TelegramSignal(BaseModel):
    """Raw text message from a Telegram signal group."""

    text: str
    strategy_id: str = ""
    secret: str = ""


@router.post("/telegram", response_model=WebhookResponse)
async def receive_telegram_signal(body: TelegramSignal, request: Request) -> dict[str, Any]:
    """Parse a Telegram-style text signal and execute it.

    Accepts formats like: ``BUY BTCUSDT @ 85000 TP 87000 SL 84000``
    """
    if len(body.text) > 500:
        raise HTTPException(status_code=422, detail="Signal text too long (max 500 chars)")

    expected_secret = _get_webhook_secret(request)
    if expected_secret and body.secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid secret")

    match = _TELEGRAM_PATTERN.search(body.text)
    if not match:
        raise HTTPException(
            status_code=422,
            detail="Could not parse signal. Expected format: BUY BTCUSDT @ 85000 TP 87000 SL 84000",
        )

    raw_side = match.group(1).upper()
    side = "buy" if raw_side in ("BUY", "LONG") else "sell"
    quantity_str = match.group(2)  # optional quantity before symbol
    symbol = match.group(3).upper()

    quantity = float(quantity_str) if quantity_str else None

    # Convert to standard webhook format and reuse existing handler
    webhook = WebhookSignal(
        symbol=symbol,
        side=side,
        action="entry",
        strategy_id=body.strategy_id,
        secret=body.secret,
        quantity=quantity,
    )
    return await receive_webhook(webhook, request)
