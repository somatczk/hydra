"""The TraderBackend seam: every engine operation the routes and MCP tools call.

D4 promotes the services layer to a process boundary: the dashboard routes and the MCP
tools stop reading ``app.state.session_manager`` and call ``app.state.trader`` — a
:class:`TraderBackend` — instead. :class:`LocalTrader` is today's behaviour verbatim: it
wraps the same ``hydra.services`` functions and makes the same ``SessionManager`` /
``RiskService`` reads the routes and tools made before the rewiring, with the same
envelopes. D5 adds ``RemoteTrader`` (the two-process split) behind the same protocol; the
op set is pinned for all of P3 — additive optional parameters only, no new ops.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from hydra.risk.circuit_breakers import CircuitBreakerRestrictions, TierNotActiveError
from hydra.risk.service import RiskService
from hydra.risk.state import RiskStateNotPersistedError, RiskStateUnavailableError
from hydra.services import sessions as sessions_svc
from hydra.services import strategies as strategies_svc
from hydra.services.errors import DB_ERRORS, db_errors, require_pool, service_error
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest

if TYPE_CHECKING:
    from hydra.execution.session_manager import HealthSnapshot

logger = logging.getLogger(__name__)

_STORE_ERRORS: tuple[type[BaseException], ...] = (RiskStateUnavailableError, *DB_ERRORS)
_CENTS = Decimal("0.01")
_PCT4 = Decimal("0.0001")
_HUNDRED = Decimal("100")


@runtime_checkable
class TraderBackend(Protocol):
    """The engine surface the REST routes and MCP tools need, as one protocol.

    Implementations raise ``hydra.services.errors.ServiceError`` with the shared
    envelope; the routes turn it into the HTTP error, the tools into the tool error, both
    byte-identical to the pre-seam answers. Every op is async: the seam has to cross a
    process boundary in D5.
    """

    async def start_session(self, actor: str, body: StartSessionRequest) -> dict[str, Any]: ...
    async def stop_session(self, session_id: str) -> dict[str, Any]: ...
    async def list_sessions(self) -> list[dict[str, Any]]: ...
    async def session_detail(self, session_id: str) -> dict[str, Any]: ...
    async def quick_trade(self, order: QuickTradeRequest) -> dict[str, Any]: ...
    async def webhook_signal(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        quantity: Decimal | None,
        strategy_id: str,
    ) -> dict[str, Any]: ...
    async def reload_strategy(self, strategy_id: str) -> dict[str, Any]: ...
    async def activate_kill_switch(self, actor: str, reason: str) -> dict[str, Any]: ...
    async def release_kill_switch(self, actor: str, reason: str) -> dict[str, Any]: ...
    async def kill_switch_active(self) -> bool: ...
    async def risk_status(self) -> dict[str, Any]: ...
    async def var_estimate(self) -> dict[str, Any]: ...
    async def circuit_breakers(self) -> list[dict[str, Any]]: ...
    async def reset_breaker(
        self, tier: int, actor: str, reason: str, confirm_tier: int
    ) -> list[dict[str, Any]]: ...
    async def reset_paper_account(self, actor: str, reason: str) -> None: ...
    async def live_status(self, source: str = "paper") -> dict[str, Any]: ...
    async def positions(self) -> list[dict[str, Any]]: ...
    async def reload_paper_capital(self, actor: str) -> None: ...
    async def health_snapshot(self) -> HealthSnapshot: ...


# ---------------------------------------------------------------------------
# Shared risk views (the former routes/risk.py and tools/risk.py builders)
# ---------------------------------------------------------------------------


def _restrictions(r: CircuitBreakerRestrictions) -> dict[str, Any]:
    return {
        "active_tier": r.tier,
        "can_open_new_trades": r.can_open_new_trades,
        "size_multiplier": r.position_size_multiplier,
        "must_flatten": r.must_flatten,
        "is_shutdown": r.is_shutdown,
        "daily_halt_until": r.daily_halt_until,
        "consecutive_pause_until": r.consecutive_pause_until,
    }


def _breakers(svc: RiskService) -> list[dict[str, Any]]:
    drawdown_pct = (svc.account().drawdown * _HUNDRED).quantize(_CENTS)
    return [
        {
            "tier": b.tier,
            "label": b.label,
            "threshold": f"{(b.threshold * _HUNDRED).quantize(_CENTS)}% drawdown",
            "threshold_pct": (b.threshold * _HUNDRED).quantize(_PCT4),
            "current_value": drawdown_pct,
            "status": b.status,
            "active_since": b.active_since,
            "requires_manual_review": b.requires_manual_review,
        }
        for b in svc.breakers()
    ]


def _account_fields(svc: RiskService) -> dict[str, Any]:
    """The RiskStatus-shaped fields of one scope's service (drawdown, daily loss, tiers).

    The percent derivations are exact Decimal math quantized at the cent — a hostile
    half (0.02675 -> 2.68) no longer lands on the float product's wrong side — and the
    dict carries Decimals that every egress floats: the routes' float-typed response
    models, the trader server's JSON encoder and the MCP tools' dump.
    """
    view = svc.account()
    cfg = view.config
    start = view.day_start_equity
    daily_loss = (
        max(Decimal("0"), start - view.equity)
        if start is not None and view.equity is not None
        else None
    )
    return {
        "current_drawdown": (view.drawdown * _HUNDRED).quantize(_CENTS),
        "max_drawdown_limit": (cfg.max_drawdown_pct * _HUNDRED).quantize(_CENTS),
        "daily_loss": None if daily_loss is None else daily_loss.quantize(_CENTS),
        "daily_loss_limit": (
            None if start is None else (start * cfg.max_daily_loss_pct).quantize(_CENTS)
        ),
        "equity": view.equity,
        "peak_equity": view.peak_equity,
        "day_start_equity": start,
        "consecutive_losses": view.consecutive_losses,
        "max_consecutive_losses": cfg.max_consecutive_losses,
        "circuit_breakers": _breakers(svc),
        "circuit_breakers_state": "live",
        "restrictions": _restrictions(view.restrictions),
    }


class LocalTrader:
    """The single-process backend: today's services calls and engine reads, verbatim.

    ``db_pool`` and ``session_manager`` are the two engine handles the routes used to
    read off ``app.state``; a ``None`` manager answers the same 503s the routes answered
    before the rewiring. The ``risk`` property exposes the manager's RiskService for the
    local paper-capital reads the system and portfolio routes kept — a remote backend has
    none, and those reads fall back to the database exactly as they do without an engine.
    """

    def __init__(self, db_pool: Any, session_manager: Any = None) -> None:
        self.db_pool = db_pool
        self.session_manager = session_manager

    @property
    def risk(self) -> RiskService | None:
        svc = getattr(self.session_manager, "risk", None)
        return svc if isinstance(svc, RiskService) else None

    @property
    def live_risk(self) -> RiskService | None:
        """Q1's live-scoped service, when the engine built one (live_capital configured)."""
        svc = getattr(self.session_manager, "live_risk", None)
        return svc if isinstance(svc, RiskService) else None

    def _require_live_risk(self) -> RiskService:
        """The live-scoped service, or the seam #8 503 both transports answer."""
        svc = self.live_risk
        if svc is None:
            raise service_error(
                503,
                "live_risk_unavailable",
                "The live-scoped risk service is not built (no live_capital is configured)",
                "unavailable",
            )
        if not svc.available:
            raise service_error(
                503,
                "live_risk_unavailable",
                "Live risk state could not be loaded; live-scope reads are refused",
                "unavailable",
            )
        return svc

    def _require_risk(self) -> RiskService:
        """The engine's RiskService, or the 503 both transports answer."""
        svc = self.risk
        if svc is None:
            raise service_error(
                503,
                "risk_service_unavailable",
                "The trading engine's risk service is not running",
                "unavailable",
            )
        if not svc.available:
            raise service_error(
                503,
                "risk_state_unavailable",
                "Risk state could not be loaded or persisted; opening orders are refused",
                "unavailable",
            )
        return svc

    # -- Sessions -----------------------------------------------------------

    async def start_session(self, actor: str, body: StartSessionRequest) -> dict[str, Any]:
        return await sessions_svc.start_session(self.session_manager, actor, body)

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        return await sessions_svc.stop_session(self.session_manager, session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return sessions_svc.list_sessions(self.session_manager)

    async def session_detail(self, session_id: str) -> dict[str, Any]:
        return await sessions_svc.get_session_detail(self.session_manager, session_id)

    async def quick_trade(self, order: QuickTradeRequest) -> dict[str, Any]:
        return await sessions_svc.submit_manual_order(self.session_manager, order)

    async def webhook_signal(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        quantity: Decimal | None,
        strategy_id: str,
    ) -> dict[str, Any]:
        return await sessions_svc.submit_webhook_order(
            self.session_manager,
            symbol=symbol,
            side=side,
            action=action,
            quantity=quantity,
            strategy_id=strategy_id,
            pool=self.db_pool,
            webhook_origin=True,
        )

    async def reload_strategy(self, strategy_id: str) -> dict[str, Any]:
        return await strategies_svc.reload_strategy(self.session_manager, strategy_id)

    # -- Kill switch ----------------------------------------------------------

    async def activate_kill_switch(self, actor: str, reason: str) -> dict[str, Any]:
        return await sessions_svc.activate_kill_switch(self.session_manager, actor, reason)

    async def release_kill_switch(self, actor: str, reason: str) -> dict[str, Any]:
        """Release the switch; the gated executor's re-validation lives here, so an
        already-released switch answers a clean no-op on both transports."""
        mgr = sessions_svc.require_manager(self.session_manager)
        if not mgr.kill_switch_active():
            return {
                "status": "already_released",
                "active": False,
                "message": "The kill switch was already released; nothing changed",
            }
        return await sessions_svc.release_kill_switch(self.db_pool, mgr, actor, reason)

    async def kill_switch_active(self) -> bool:
        return bool(sessions_svc.require_manager(self.session_manager).kill_switch_active())

    # -- Risk reads and mutations ----------------------------------------------

    async def risk_status(self) -> dict[str, Any]:
        svc = self._require_risk()
        return _account_fields(svc)

    async def var_estimate(self) -> dict[str, Any]:
        svc = self._require_risk()
        equity = svc.account().equity
        if equity is None:
            raise service_error(
                503, "equity_unknown", "No account equity has been marked yet", "unavailable"
            )
        pool = require_pool(self.db_pool)

        var_95 = Decimal("0")
        var_99 = Decimal("0")
        cvar_95 = Decimal("0")

        async with db_errors("computing VaR"), pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT SUM(pnl - fee) AS daily_pnl FROM trades "
                "WHERE source = 'paper' "
                "GROUP BY date_trunc('day', timestamp) "
                "ORDER BY date_trunc('day', timestamp)",
            )

        if len(rows) >= 5:
            # str() avoids Decimal(float)'s binary-expansion noise
            daily_pnls = sorted(Decimal(str(r["daily_pnl"])) for r in rows)
            n = len(daily_pnls)
            idx_95 = max(0, int(n * Decimal("0.05")))
            idx_99 = max(0, int(n * Decimal("0.01")))
            var_95 = abs(min(daily_pnls[idx_95], Decimal(0)))
            var_99 = abs(min(daily_pnls[idx_99], Decimal(0)))
            tail = [v for v in daily_pnls[: idx_95 + 1] if v < 0]
            cvar_95 = abs(sum(tail, Decimal(0)) / len(tail)) if tail else var_95

        try:
            portfolio_value = float(equity.quantize(_CENTS))
        except InvalidOperation as exc:
            raise service_error(
                503,
                "equity_out_of_range",
                "Account equity is too large to report in cents; check the paper_capital setting",
                "unavailable",
            ) from exc
        return {
            "var_95": float(var_95.quantize(_CENTS)),
            "var_99": float(var_99.quantize(_CENTS)),
            "cvar_95": float(cvar_95.quantize(_CENTS)),
            "portfolio_value": portfolio_value,
            "calculation_method": "historical_simulation",
            "sample_days": len(rows),
        }

    async def circuit_breakers(self) -> list[dict[str, Any]]:
        return _breakers(self._require_risk())

    async def reset_breaker(
        self, tier: int, actor: str, reason: str, confirm_tier: int
    ) -> list[dict[str, Any]]:
        svc = self._require_risk()
        try:
            await svc.reset_breaker(tier, actor=actor, reason=reason, confirm_tier=confirm_tier)
        except TierNotActiveError as exc:
            raise service_error(
                409, "tier_not_active", str(exc), "invalid", active_tier=svc.account().tier
            ) from exc
        except ValueError as exc:
            raise service_error(422, "invalid_reset", str(exc), "invalid") from exc
        except RiskStateNotPersistedError as exc:
            logger.exception("Circuit breaker reset not persisted; rolled back")
            raise service_error(
                503,
                "reset_not_persisted",
                "The reset could not be saved and was rolled back; the breaker is unchanged",
                "unavailable",
            ) from exc
        except _STORE_ERRORS as exc:
            logger.exception("Circuit breaker reset not audited; state unchanged")
            raise service_error(
                503,
                "audit_unavailable",
                "The reset could not be written to the audit log; nothing changed",
                "unavailable",
            ) from exc
        return _breakers(svc)

    async def reset_paper_account(self, actor: str, reason: str) -> None:
        svc = self._require_risk()
        try:
            await svc.reset_paper_account(actor=actor, reason=reason)
        except _STORE_ERRORS as exc:
            logger.exception("Paper account reset refused or not audited")
            raise service_error(
                503,
                "paper_reset_unavailable",
                "The paper account was not reset: risk state is recovering, a held position "
                "has no mark yet, or the audit log could not be written",
                "unavailable",
            ) from exc

    async def live_status(self, source: str = "paper") -> dict[str, Any]:
        """Kill switch, sessions and breaker restrictions; unknown stays unknown.

        ``source="live"`` (R11, additive) reads Q1's live-scoped service instead: the
        same operating picture computed on live equity plus the live-scope account
        numbers (equity, drawdown, active tier), so the risk routes' source=live reads
        ride this one op (seam #5). No live service built is the 503 of seam #8.
        """
        if source == "live":
            return await self._live_scope_status()
        kill_switch: bool | None = None
        pool = self.db_pool
        if pool is not None:
            row: Any = None
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM risk_config WHERE scope = $1", "global"
                    )
            except DB_ERRORS:
                logger.warning("Kill switch state unknown: risk_config read failed", exc_info=True)
                row = None
            if row is not None:
                kill_switch = bool(row["kill_switch_active"])

        svc = self.risk
        if svc is not None and svc.kill_switch_active():
            kill_switch = True

        running: int | None = None
        mgr = self.session_manager
        if mgr is not None:
            running = sum(1 for s in mgr.list_sessions() if s.status == "running")

        live = svc is not None and svc.available
        return {
            "kill_switch_active": kill_switch,
            "kill_switch_state": (
                "unknown" if kill_switch is None else ("active" if kill_switch else "inactive")
            ),
            "running_sessions": running,
            "circuit_breaker_restrictions": (
                _restrictions(svc.restrictions()) if svc is not None and live else None
            ),
            "circuit_breakers_state": "live" if live else "unavailable",
        }

    async def _live_scope_status(self) -> dict[str, Any]:
        """The live scope's operating picture: the one global kill switch as the
        live-scoped service sees it, the running live sessions, and the live account."""
        svc = self._require_live_risk()
        fields = _account_fields(svc)
        mgr = self.session_manager
        running = (
            None
            if mgr is None
            else sum(
                1 for s in mgr.list_sessions() if s.status == "running" and s.trading_mode == "live"
            )
        )
        active = svc.kill_switch_active()
        return {
            "kill_switch_active": active,
            "kill_switch_state": "active" if active else "inactive",
            "running_sessions": running,
            "circuit_breaker_restrictions": fields["restrictions"],
            "circuit_breakers_state": "live",
            "active_tier": fields["restrictions"]["active_tier"],
            **fields,
        }

    async def positions(self) -> list[dict[str, Any]]:
        """The held positions of every running session with its session and strategy."""
        mgr = sessions_svc.require_manager(self.session_manager)
        held: list[dict[str, Any]] = []
        for s in mgr.list_sessions():
            if s.status != "running":
                continue
            detail = await sessions_svc.get_session_detail(mgr, s.session_id)
            held.append(
                {
                    "session_id": s.session_id,
                    "strategy_id": s.strategy_id,
                    "positions": detail["positions"],
                }
            )
        return held

    async def reload_paper_capital(self, actor: str) -> None:
        """Hand a saved paper_capital to the risk ledger (its audited paper reset).

        No risk service (no engine running) is not an error: the saved value applies at
        the next engine start, exactly as before the seam. Store errors propagate.
        """
        svc = self.risk
        if svc is None:
            return
        await svc.reload_config(actor=actor)

    async def health_snapshot(self) -> HealthSnapshot:
        mgr = sessions_svc.require_manager(self.session_manager)
        return cast("HealthSnapshot", await mgr.health_snapshot())
