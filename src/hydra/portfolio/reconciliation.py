"""Balance reconciliation between local state and exchange-reported state.

Detects discrepancies, generates sync actions, and logs warnings when the
difference exceeds configurable thresholds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from hydra.core.types import Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class SyncActionType(StrEnum):
    """Possible sync actions."""

    UPDATE_LOCAL = "update_local"
    CANCEL_ORPHAN = "cancel_orphan"
    LOG_DISCREPANCY = "log_discrepancy"


@dataclass(slots=True)
class Discrepancy:
    """A single balance discrepancy between local and exchange state."""

    asset: str
    local: Decimal
    exchange: Decimal
    diff_pct: Decimal


@dataclass(slots=True)
class ReconciliationResult:
    """Outcome of a balance reconciliation check."""

    matched: bool
    discrepancies: list[Discrepancy] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class SyncAction:
    """A corrective action to synchronize local state with the exchange."""

    action: SyncActionType
    details: dict


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class BalanceReconciler:
    """Reconciles local balances and positions against exchange-reported data."""

    # ------------------------------------------------------------------
    # Balance reconciliation
    # ------------------------------------------------------------------

    @staticmethod
    def reconcile(
        exchange_id: str,
        local_balances: dict[str, Decimal],
        exchange_balances: dict[str, Decimal],
        threshold: Decimal = Decimal("0.001"),
    ) -> ReconciliationResult:
        """Compare local vs. exchange balances and flag discrepancies.

        Parameters
        ----------
        exchange_id:
            The exchange being reconciled (for logging).
        local_balances:
            Asset -> balance as tracked locally.
        exchange_balances:
            Asset -> balance as reported by the exchange.
        threshold:
            Maximum acceptable relative difference (default 0.1%).
            Discrepancies below this are considered matched.

        Returns
        -------
        ReconciliationResult
        """
        all_assets = set(local_balances.keys()) | set(exchange_balances.keys())
        discrepancies: list[Discrepancy] = []

        for asset in sorted(all_assets):
            local_val = local_balances.get(asset, Decimal("0"))
            exchange_val = exchange_balances.get(asset, Decimal("0"))

            # Compute percentage difference relative to the larger value
            reference = max(abs(local_val), abs(exchange_val))
            if reference == Decimal("0"):
                continue  # both zero, nothing to reconcile

            diff_pct = abs(local_val - exchange_val) / reference

            if diff_pct > threshold:
                discrepancies.append(
                    Discrepancy(
                        asset=asset,
                        local=local_val,
                        exchange=exchange_val,
                        diff_pct=diff_pct,
                    )
                )
                logger.warning(
                    "Balance discrepancy on %s/%s: local=%s exchange=%s diff=%.4f%%",
                    exchange_id,
                    asset,
                    local_val,
                    exchange_val,
                    diff_pct * Decimal("100"),
                )

        matched = len(discrepancies) == 0
        return ReconciliationResult(matched=matched, discrepancies=discrepancies)

    # ------------------------------------------------------------------
    # Full position sync
    # ------------------------------------------------------------------

    @staticmethod
    def full_sync(
        exchange_id: str,
        exchange_positions: list[dict],
        local_positions: list[Position],
    ) -> list[SyncAction]:
        """Generate sync actions to align local positions with the exchange.

        Parameters
        ----------
        exchange_id:
            The exchange being synced.
        exchange_positions:
            Positions as reported by the exchange. Each dict must have at
            least ``symbol``, ``direction``, ``quantity``.
        local_positions:
            Positions currently tracked locally.

        Returns
        -------
        list[SyncAction]
            Actions needed to bring local state in line with the exchange.
        """
        actions: list[SyncAction] = []

        # Index local positions by symbol for fast lookup
        local_by_symbol: dict[str, Position] = {
            p.symbol: p for p in local_positions if p.exchange_id == exchange_id
        }

        # Index exchange positions by symbol
        exchange_by_symbol: dict[str, dict] = {ep["symbol"]: ep for ep in exchange_positions}

        # 1. Positions on the exchange but not locally -> update local
        for symbol, ep in exchange_by_symbol.items():
            if symbol not in local_by_symbol:
                actions.append(
                    SyncAction(
                        action=SyncActionType.UPDATE_LOCAL,
                        details={
                            "exchange_id": exchange_id,
                            "symbol": symbol,
                            "reason": "missing_local_position",
                            "exchange_direction": ep.get("direction", ""),
                            "exchange_quantity": str(ep.get("quantity", "0")),
                        },
                    )
                )
                continue

            # Position exists locally: check for quantity/direction mismatch
            local_pos = local_by_symbol[symbol]
            ex_qty = Decimal(str(ep.get("quantity", "0")))
            ex_dir = ep.get("direction", "")

            if local_pos.quantity != ex_qty or local_pos.direction != ex_dir:
                actions.append(
                    SyncAction(
                        action=SyncActionType.LOG_DISCREPANCY,
                        details={
                            "exchange_id": exchange_id,
                            "symbol": symbol,
                            "local_quantity": str(local_pos.quantity),
                            "exchange_quantity": str(ex_qty),
                            "local_direction": local_pos.direction,
                            "exchange_direction": ex_dir,
                        },
                    )
                )

        # 2. Positions tracked locally but not on the exchange -> orphans
        for symbol, local_pos in local_by_symbol.items():
            if symbol not in exchange_by_symbol:
                actions.append(
                    SyncAction(
                        action=SyncActionType.CANCEL_ORPHAN,
                        details={
                            "exchange_id": exchange_id,
                            "symbol": symbol,
                            "reason": "orphan_local_position",
                            "local_direction": local_pos.direction,
                            "local_quantity": str(local_pos.quantity),
                        },
                    )
                )

        return actions
