"""size_entry and MarketRules from a ccxt-shaped Binance spot market (no network)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.risk.market_rules import (
    MarketRules,
    MarketRulesCache,
    MarketRulesUnavailableError,
    UnsupportedExchangeError,
    load_market_rules,
)
from hydra.risk.sizing import size_entry

BINANCE_BTCUSDT: dict[str, Any] = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT",
    "spot": True,
    "precision": {"amount": 1e-05, "price": 0.01},
    "limits": {
        "amount": {"min": 1e-05, "max": 9000.0},
        "market": {"min": 0.0, "max": 120.5},
        "cost": {"min": 5.0, "max": 9000000.0},
    },
}


def _rules() -> MarketRules:
    return MarketRules.from_ccxt_market(BINANCE_BTCUSDT)


def _size(sleeve: str, mult: str = "1", quote: str = "100000") -> Decimal | None:
    return size_entry(
        sleeve_equity=Decimal(sleeve),
        fraction=Decimal("0.10"),
        mark=Decimal("61234.56"),
        size_multiplier=Decimal(mult),
        rules=_rules(),
        quote_balance=Decimal(quote),
        fee_buffer=Decimal("0.002"),
    )


class TestMarketRules:
    def test_from_ccxt_market(self) -> None:
        r = _rules()
        assert r.symbol == "BTCUSDT"
        assert r.amount_step == Decimal("0.00001")
        assert r.min_qty == Decimal("0.00001")
        assert r.min_notional == Decimal("5.0")
        assert r.max_market_qty == Decimal("120.5")
        assert r.price_tick == Decimal("0.01")

    @pytest.mark.parametrize(
        ("raw", "floored"),
        [
            ("0.000019999", "0.00001"),
            ("0.0032661", "0.00326"),
            ("0.00326", "0.00326"),
            ("0.000009", "0"),
        ],
    )
    def test_floor_qty_never_rounds_up(self, raw: str, floored: str) -> None:
        assert _rules().floor_qty(Decimal(raw)) == Decimal(floored)

    def test_validate_open(self) -> None:
        r = _rules()
        assert r.validate_open(Decimal("0.001"), Decimal("60000")) is None
        assert "lot step" in str(r.validate_open(Decimal("0.0010001"), Decimal("60000")))
        assert "min notional" in str(r.validate_open(Decimal("0.00008"), Decimal("60000")))
        assert "market maximum" in str(r.validate_open(Decimal("121"), Decimal("1")))


class TestSizeEntry:
    def test_sleeve_fraction_floored_to_step(self) -> None:
        assert _size("2000") == Decimal("0.00326")

    def test_cb_multiplier_halves(self) -> None:
        assert _size("2000", mult="0.5") == Decimal("0.00163")

    def test_below_min_notional_is_none(self) -> None:
        assert _size("40") is None

    def test_capped_by_quote_balance_net_of_fee_buffer(self) -> None:
        qty = _size("2000", quote="100")
        assert qty == Decimal("0.00162")
        assert qty * Decimal("61234.56") * Decimal("1.002") <= Decimal("100")


class _Loader:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, exchange_id: str) -> Iterable[Mapping[str, Any]]:
        self.calls += 1
        return [BINANCE_BTCUSDT]


class TestLoader:
    async def test_non_binance_rejected(self) -> None:
        with pytest.raises(UnsupportedExchangeError):
            await load_market_rules("bybit", ["BTCUSDT"], loader=_Loader())

    async def test_unknown_symbol_rejected(self) -> None:
        with pytest.raises(MarketRulesUnavailableError, match="DOGEUSDT"):
            await load_market_rules("binance", ["DOGEUSDT"], loader=_Loader())

    async def test_loader_failure_is_unavailable(self) -> None:
        async def boom(exchange_id: str) -> Iterable[Mapping[str, Any]]:
            raise ConnectionError("down")

        with pytest.raises(MarketRulesUnavailableError, match="down"):
            await load_market_rules("binance", ["BTCUSDT"], loader=boom)

    async def test_cache_reloads_after_ttl(self) -> None:
        loader = _Loader()
        now = [datetime(2026, 10, 20, tzinfo=UTC)]
        cache = MarketRulesCache(loader=loader, clock=lambda: now[0])
        rules = await cache.get("binance", ["BTCUSDT", "BTC/USDT"])
        assert rules["BTCUSDT"] == rules["BTC/USDT"] == _rules()
        now[0] += timedelta(hours=23)
        await cache.get("binance", ["BTCUSDT"])
        assert loader.calls == 1
        now[0] += timedelta(hours=1)
        await cache.get("binance", ["BTCUSDT"])
        assert loader.calls == 2
