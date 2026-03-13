"""Tests for BalanceReconciler: matching, discrepancies, full sync."""

from __future__ import annotations

from decimal import Decimal

from hydra.core.types import Direction, Position, Symbol
from hydra.portfolio.reconciliation import (
    BalanceReconciler,
    SyncActionType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

reconciler = BalanceReconciler()


def _pos(
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    quantity: str = "1",
    exchange_id: str = "binance",
) -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=direction,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal("40000"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        strategy_id="test",
        exchange_id=exchange_id,
    )


# ---------------------------------------------------------------------------
# Balance reconciliation
# ---------------------------------------------------------------------------


class TestReconcileMatchingBalances:
    def test_identical_balances_match(self) -> None:
        local = {"BTC": Decimal("1.5"), "USDT": Decimal("10000")}
        exchange = {"BTC": Decimal("1.5"), "USDT": Decimal("10000")}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is True
        assert len(result.discrepancies) == 0

    def test_both_zero_match(self) -> None:
        local = {"BTC": Decimal("0")}
        exchange = {"BTC": Decimal("0")}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is True


class TestReconcileDiscrepancy:
    def test_large_discrepancy_flagged(self) -> None:
        local = {"BTC": Decimal("1.0")}
        exchange = {"BTC": Decimal("1.5")}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is False
        assert len(result.discrepancies) == 1
        d = result.discrepancies[0]
        assert d.asset == "BTC"
        assert d.local == Decimal("1.0")
        assert d.exchange == Decimal("1.5")
        # diff_pct = |1.0 - 1.5| / 1.5 = 0.333...
        assert d.diff_pct > Decimal("0.001")

    def test_small_discrepancy_within_threshold(self) -> None:
        # 0.0005 / 1.0 = 0.05% < 0.1% threshold
        local = {"BTC": Decimal("1.0")}
        exchange = {"BTC": Decimal("1.0005")}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is True
        assert len(result.discrepancies) == 0

    def test_custom_threshold(self) -> None:
        local = {"BTC": Decimal("1.0")}
        exchange = {"BTC": Decimal("1.005")}
        # With tight threshold of 0.001 (0.1%), diff of 0.5% should fail
        result = reconciler.reconcile("binance", local, exchange, threshold=Decimal("0.001"))
        assert result.matched is False

        # With loose threshold of 0.01 (1%), same diff should pass
        result = reconciler.reconcile("binance", local, exchange, threshold=Decimal("0.01"))
        assert result.matched is True

    def test_missing_asset_locally(self) -> None:
        local: dict[str, Decimal] = {}
        exchange = {"BTC": Decimal("1.0")}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is False
        assert result.discrepancies[0].asset == "BTC"
        assert result.discrepancies[0].local == Decimal("0")

    def test_missing_asset_on_exchange(self) -> None:
        local = {"BTC": Decimal("1.0")}
        exchange: dict[str, Decimal] = {}
        result = reconciler.reconcile("binance", local, exchange)
        assert result.matched is False
        assert result.discrepancies[0].exchange == Decimal("0")


# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------


class TestFullSyncOrphanDetection:
    def test_orphan_local_position(self) -> None:
        """Local has a position that exchange does not -- orphan."""
        local_positions = [_pos(symbol="BTCUSDT")]
        exchange_positions: list[dict] = []
        actions = reconciler.full_sync("binance", exchange_positions, local_positions)

        assert len(actions) == 1
        assert actions[0].action == SyncActionType.CANCEL_ORPHAN
        assert actions[0].details["symbol"] == "BTCUSDT"
        assert actions[0].details["reason"] == "orphan_local_position"


class TestFullSyncMissingLocal:
    def test_missing_local_position(self) -> None:
        """Exchange has a position that local does not -- create local."""
        local_positions: list[Position] = []
        exchange_positions = [{"symbol": "ETHUSDT", "direction": "LONG", "quantity": "5"}]
        actions = reconciler.full_sync("binance", exchange_positions, local_positions)

        assert len(actions) == 1
        assert actions[0].action == SyncActionType.UPDATE_LOCAL
        assert actions[0].details["symbol"] == "ETHUSDT"
        assert actions[0].details["reason"] == "missing_local_position"


class TestFullSyncQuantityMismatch:
    def test_quantity_mismatch_logged(self) -> None:
        """Both sides have the position but quantities differ."""
        local_positions = [_pos(symbol="BTCUSDT", quantity="1")]
        exchange_positions = [{"symbol": "BTCUSDT", "direction": Direction.LONG, "quantity": "2"}]
        actions = reconciler.full_sync("binance", exchange_positions, local_positions)

        assert len(actions) == 1
        assert actions[0].action == SyncActionType.LOG_DISCREPANCY
        assert actions[0].details["local_quantity"] == "1"
        assert actions[0].details["exchange_quantity"] == "2"


class TestFullSyncAllMatched:
    def test_no_actions_when_synced(self) -> None:
        local_positions = [_pos(symbol="BTCUSDT", quantity="1")]
        exchange_positions = [{"symbol": "BTCUSDT", "direction": Direction.LONG, "quantity": "1"}]
        actions = reconciler.full_sync("binance", exchange_positions, local_positions)
        assert len(actions) == 0
