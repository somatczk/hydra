"""The risk relay: every ``risk_events`` row to ``/ws/risk`` and to Telegram.

The trades relay exists so fills cross the process boundary; this one exists so the
risk surface does — and, load-bearing, so the alerter runs with no browser connected:
the lifespan starts it at app startup (pool-only, like the refreshers), never the first
``/ws/risk`` connection. One poll every 2 s does two things:

1. Drives a serial cursor over ``risk_events`` (anchored at ``MAX(id)`` when the relay
   starts; a failed poll keeps the cursor and retries) and, per new row, broadcasts
   ``{"event": "risk_event", "id", "scope", "kind", "tier", "actor", "reason"}`` on
   channel ``risk`` and hands the row to the Telegram sink — every row, no allowlist to
   drift; Alertmanager keeps infra paging.
2. Reads the ``paper`` risk_state row plus the global kill-switch flag and, on any
   change, broadcasts the ``risk_state`` heartbeat. Its RiskStatus-named fields carry
   RiskStatus-shaped values (drawdown in percent, daily loss in USDT; None stays null,
   never fabricated).

The Telegram send is a fire-and-forget task (the ``session_wiring`` background pattern),
so a hung send (10 s timeout) never delays the poll loop; a failed send is logged and
never raised. Feed staleness is in-memory trader state a DB relay cannot see: it stays
on the polls. The cursor anchors at ``MAX(id)``, so events written while the app was
down are never alerted (they remain in ``/api/risk/events``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from hydra.core.websocket import manager
from hydra.ops.notifications import TelegramNotifier

logger = logging.getLogger(__name__)

RELAY_POLL_S = 2.0
RELAY_BATCH = 500

_HUNDRED = Decimal("100")
_CENTS = Decimal("0.01")

NotifierFactory = Callable[[Any], Awaitable[TelegramNotifier | None]]

_STATE_SQL = (
    "SELECT equity, peak_portfolio_value, day_start_equity, current_drawdown, "
    "consecutive_losses, cb_tier, cb_tier_since, daily_halt_until, "
    "consecutive_pause_until, updated_at FROM risk_state WHERE scope = 'paper'"
)
_KILL_SQL = "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"


async def _stored_telegram_notifier(pool: Any) -> TelegramNotifier | None:
    """The encrypted ``system_config`` creds as a notifier; ``None`` sends nothing."""
    from hydra.dashboard.routes.system import load_system_config

    cfg = await load_system_config(pool)
    token = str(cfg.get("telegram_bot_token") or "")
    chat_id = str(cfg.get("telegram_chat_id") or "")
    if not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id)


def _telegram_text(row: Mapping[str, Any]) -> tuple[str, str]:
    tier = "" if row["tier"] is None else f" (tier {int(row['tier'])})"
    return (
        f"Hydra risk event{tier}: {row['kind']}",
        f"{row['reason']}\nscope: {row['scope']} · actor: {row['actor']}",
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _state_payload(state: Mapping[str, Any], kill_switch: bool | None) -> dict[str, Any]:
    """The ``risk_state`` heartbeat; RiskStatus-shaped, and None stays null.

    The two derived numbers follow ``_account_fields``: exact Decimal math quantized
    at the cent, floated only here, so the heartbeat and ``/api/risk/status`` agree
    on a hostile half instead of straddling it.
    """
    equity = None if state["equity"] is None else float(state["equity"])
    peak = None if state["peak_portfolio_value"] is None else float(state["peak_portfolio_value"])
    day_start = None if state["day_start_equity"] is None else float(state["day_start_equity"])
    daily_loss = (
        None
        if equity is None or day_start is None
        else float(
            max(
                Decimal("0"),
                Decimal(str(state["day_start_equity"])) - Decimal(str(state["equity"])),
            ).quantize(_CENTS)
        )
    )
    return {
        "event": "risk_state",
        "scope": "paper",
        "equity": equity,
        "peak_equity": peak,
        "day_start_equity": day_start,
        "daily_loss": daily_loss,
        "current_drawdown": float(
            (Decimal(str(state["current_drawdown"] or 0)) * _HUNDRED).quantize(_CENTS)
        ),
        "consecutive_losses": int(state["consecutive_losses"] or 0),
        "cb_tier": int(state["cb_tier"] or 0),
        "cb_tier_since": _iso(state["cb_tier_since"]),
        "daily_halt_until": _iso(state["daily_halt_until"]),
        "consecutive_pause_until": _iso(state["consecutive_pause_until"]),
        "kill_switch_active": None if kill_switch is None else bool(kill_switch),
        "updated_at": _iso(state["updated_at"]),
    }


class RiskRelay:
    """One background poll of the risk tables per app, from startup to shutdown.

    The cursor starts at the current ``MAX(id)`` when the relay starts, so rows
    written before it are not re-announced; a failed poll keeps the cursor and
    the last heartbeat, so a DB blip delays a tick, it never drops one.
    """

    def __init__(
        self,
        pool: Any,
        *,
        poll_s: float = RELAY_POLL_S,
        notifier_factory: NotifierFactory | None = None,
    ) -> None:
        self._pool = pool
        self._poll_s = poll_s
        self._notifier_factory: NotifierFactory = notifier_factory or _stored_telegram_notifier
        self._cursor: int | None = None
        self._last_state: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def ensure_started(self) -> None:
        """Start the poll task if it is not running; a restart re-anchors everything."""
        if self._task is None or self._task.done():
            self._cursor = None
            self._last_state = None
            self._task = asyncio.create_task(self._loop(), name="risk-relay")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Risk relay poll failed; retrying next tick")
            await asyncio.sleep(self._poll_s)

    async def _poll_once(self) -> None:
        if self._cursor is None:
            self._cursor = int(
                await self._pool.fetchval("SELECT COALESCE(MAX(id), 0) FROM risk_events")
            )
        rows = await self._pool.fetch(
            "SELECT id, scope, kind, tier, actor, reason FROM risk_events "
            "WHERE id > $1 ORDER BY id LIMIT $2",
            self._cursor,
            RELAY_BATCH,
        )
        for row in rows:
            self._cursor = int(row["id"])
            record = dict(row)
            await manager.broadcast("risk", _event_message(record))
            self._dispatch_telegram(record)
        await self._heartbeat()

    async def _heartbeat(self) -> None:
        state = await self._pool.fetchrow(_STATE_SQL)
        if state is None:
            return  # no paper row yet: nothing to heartbeat, never a fabricated one
        kill_switch = await self._pool.fetchval(_KILL_SQL)
        payload = _state_payload(state, kill_switch)
        if payload != self._last_state:
            self._last_state = payload
            await manager.broadcast("risk", payload)

    def _dispatch_telegram(self, row: dict[str, Any]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(self._send_telegram(row))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    async def _send_telegram(self, row: dict[str, Any]) -> None:
        """Off the poll loop: the creds read and the send both run in the background."""
        try:
            notifier = await self._notifier_factory(self._pool)
            if notifier is None:
                return
            title, message = _telegram_text(row)
            await notifier.send(title, message)
        except Exception as exc:
            # No exc_info: a failure here must never leak the token-bearing URL into logs.
            logger.warning("Risk relay Telegram send failed (%s)", type(exc).__name__)


def _event_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "risk_event",
        "id": int(row["id"]),
        "scope": str(row["scope"]),
        "kind": str(row["kind"]),
        "tier": None if row["tier"] is None else int(row["tier"]),
        "actor": str(row["actor"]),
        "reason": str(row["reason"]),
    }


_BACKGROUND: set[asyncio.Task[None]] = set()
