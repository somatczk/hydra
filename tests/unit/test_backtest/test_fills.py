"""Tests for hydra.backtest.fills -- CommissionConfig (fills live in hydra.execution)."""

from __future__ import annotations

from decimal import Decimal

from hydra.backtest.fills import CommissionConfig
from hydra.core.types import MarketType


class TestCommission:
    def test_spot_maker_rate(self) -> None:
        config = CommissionConfig(spot_maker=Decimal("0.0005"), spot_taker=Decimal("0.001"))
        assert config.fee_rate(MarketType.SPOT, is_maker=True) == Decimal("0.0005")
        assert config.fee_rate(MarketType.SPOT, is_maker=False) == Decimal("0.001")

    def test_futures_rate(self) -> None:
        config = CommissionConfig(futures_maker=Decimal("0.0002"), futures_taker=Decimal("0.0004"))
        assert config.fee_rate(MarketType.FUTURES, is_maker=True) == Decimal("0.0002")
        assert config.fee_rate(MarketType.FUTURES, is_maker=False) == Decimal("0.0004")
