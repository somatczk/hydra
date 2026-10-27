"""The session health data model and the per-session health snapshot (S6 pure move)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from hydra.execution import session_store
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.service import RiskService

if TYPE_CHECKING:
    from hydra.execution.reconcile import ReconcileStatus
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionHealth:
    session_id: str
    strategy_id: str
    mode: str
    status: str
    desired: bool
    supervised: bool
    last_bar_at: datetime | None
    last_bar_age_s: float | None
    last_fill_at: datetime | None
    open_positions: int
    flatten_incomplete: bool
    entries_paused: str | None
    feed_live: bool
    feed_stale_reasons: dict[str, list[str]]
    persist_backlog: int
    persist_dirty: bool
    error_message: str | None
    protection_failures: tuple[str, ...] = ()
    status_persisted: bool = True
    reconcile_state: str | None = None
    last_reconciled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    sessions: tuple[SessionHealth, ...]
    desired_from_db: bool
    kill_switch: bool
    risk_available: bool


class SessionHealthMixin:
    """``health_snapshot``, moved verbatim from ``session_manager`` (S6 pure move)."""

    _db_pool: Any
    _sessions: dict[str, TradingSession]
    risk: RiskService | None

    # Provided by the host; mypy's override checks keep the signature in sync.
    def kill_switch_active(self) -> bool:
        raise NotImplementedError

    def reconcile_status(self, session_id: str) -> ReconcileStatus | None:
        raise NotImplementedError

    async def health_snapshot(self) -> HealthSnapshot:
        """Per session: desired (DB) vs supervised (task alive), feed and bar freshness."""
        desired_ids: set[str] | None = None
        if self._db_pool is not None:
            try:
                desired_ids = await session_store.running_ids(self._db_pool)
            except Exception:
                logger.warning("health snapshot: running rows unreadable", exc_info=True)
        now = datetime.now(UTC)
        out: list[SessionHealth] = []
        for s in self._sessions.values():
            rt = s._runtime
            executor = s._executor
            snap = (
                executor.get_account_snapshot()
                if isinstance(executor, PaperTradingExecutor)
                else None
            )
            eid = s.exchange_id
            reconcile = self.reconcile_status(s.session_id) if s.trading_mode == "live" else None
            out.append(
                SessionHealth(
                    session_id=s.session_id,
                    strategy_id=s.strategy_id,
                    mode=s.trading_mode,
                    status=s.status,
                    desired=(s.session_id in desired_ids)
                    if desired_ids is not None
                    else s.status == "running",
                    supervised=s.task_alive,
                    last_bar_at=s.last_bar_at,
                    last_bar_age_s=(now - s.last_bar_at).total_seconds() if s.last_bar_at else None,
                    last_fill_at=s.last_fill_at,
                    open_positions=s.holdings,
                    flatten_incomplete=s.flatten_incomplete,
                    entries_paused=rt.entries_paused if rt is not None else None,
                    feed_live=rt.feed.is_live(eid) if rt is not None and s.task_alive else False,
                    feed_stale_reasons=rt.feed.stale_reasons(eid) if rt is not None else {},
                    persist_backlog=rt.feed.persist_backlog(eid) if rt is not None else 0,
                    persist_dirty=bool(snap.persist_dirty) if snap is not None else False,
                    error_message=s.error_message,
                    protection_failures=tuple(rt.protect.failures[-10:]) if rt is not None else (),
                    status_persisted=s.persist_error is None,
                    reconcile_state=None if reconcile is None else reconcile.state,
                    last_reconciled_at=None if reconcile is None else reconcile.last_reconciled_at,
                )
            )
        for sid in sorted((desired_ids or set()) - set(self._sessions)):
            out.append(
                SessionHealth(
                    session_id=sid,
                    strategy_id="",
                    mode="",
                    status="running",
                    desired=True,
                    supervised=False,
                    last_bar_at=None,
                    last_bar_age_s=None,
                    last_fill_at=None,
                    open_positions=0,
                    flatten_incomplete=False,
                    entries_paused=None,
                    feed_live=False,
                    feed_stale_reasons={},
                    persist_backlog=0,
                    persist_dirty=False,
                    error_message="running in the database but not in this process",
                )
            )
        return HealthSnapshot(
            sessions=tuple(out),
            desired_from_db=desired_ids is not None,
            kill_switch=self.kill_switch_active(),
            risk_available=self.risk is not None and self.risk.available,
        )
