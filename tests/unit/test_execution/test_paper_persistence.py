"""Tests for PaperTradingExecutor DB persistence: fills persisted with source='paper'."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from hydra.execution.paper_trading import PaperTradingExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db_pool() -> MagicMock:
    """Mock asyncpg pool for persistence tests."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestPaperPersistence:
    async def test_fill_persists_to_db(self, mock_db_pool: MagicMock) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
            db_pool=mock_db_pool,
            strategy_id="test-strat",
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )

        # Give the fire-and-forget task a chance to run
        import asyncio

        await asyncio.sleep(0.1)

        # Verify DB insert was called
        conn = mock_db_pool.acquire().__aenter__.return_value
        conn.execute.assert_called()
        # Check the SQL contains 'paper' source
        call_args = conn.execute.call_args
        assert "paper" in call_args[0][0]

    async def test_paper_capital_from_param(self) -> None:
        capital = Decimal("50000")
        executor = PaperTradingExecutor(
            initial_balances={"USDT": capital},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        balances = await executor.fetch_balance()
        assert balances["USDT"] == capital

    async def test_default_capital_is_10000(self) -> None:
        executor = PaperTradingExecutor()
        balances = await executor.fetch_balance()
        assert balances["USDT"] == Decimal("10000")

    async def test_insufficient_balance_still_raises(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
            db_pool=MagicMock(),
            strategy_id="test",
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        with pytest.raises(ValueError, match="Insufficient"):
            await executor.create_order(
                symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
            )

    async def test_no_db_pool_no_error(self) -> None:
        """Executor works fine without db_pool — no persistence, no crash."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            db_pool=None,
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        result = await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        assert result["status"] == "FILLED"

    async def test_strategy_id_stored(self) -> None:
        executor = PaperTradingExecutor(
            strategy_id="momentum-rsi",
        )
        assert executor._strategy_id == "momentum-rsi"
