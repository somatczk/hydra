"""size_entry and MarketRules from ccxt-shaped Binance markets (spot and USD⌂-M, no network)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.risk.market_rules import (
    USDM_CALLBACK_RATE_PCT,
    MarketRules,
    MarketRulesCache,
    MarketRulesUnavailableError,
    UnsupportedExchangeError,
    load_market_rules,
    public_venue_markets,
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

# The real mainnet Binance USD⌂-M BTCUSDT perpetual filters (fapi exchangeInfo):
# tick 0.10, lot step 0.001, market lot max 120, MIN_NOTIONAL 20 USDT. Futures
# exchangeInfo has no trailing filter — spot's TRAILING_DELTA sits in the dict to
# pin that it is never mapped onto the perp rules.
BINANCEUSDM_BTCUSDT_PERP: dict[str, Any] = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT:USDT",
    "spot": False,
    "swap": True,
    "linear": True,
    "inverse": False,
    "type": "swap",
    "precision": {"amount": 0.001, "price": 0.1},
    "limits": {
        "amount": {"min": 0.001, "max": 1000.0},
        "market": {"min": 0.001, "max": 120.0},
        "cost": {"min": 20.0, "max": None},
    },
    "info": {
        "symbol": "BTCUSDT",
        "contractType": "PERPETUAL",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "maxQty": "120"},
            {"filterType": "MIN_NOTIONAL", "notional": "20"},
            {
                "filterType": "TRAILING_DELTA",
                "minTrailingBelowDelta": "10",
                "maxTrailingBelowDelta": "2000",
            },
        ],
    },
}


def _rules() -> MarketRules:
    return MarketRules.from_ccxt_market(BINANCE_BTCUSDT)


# The bybit v5 spot BTCUSDT instrument (the /v5/market/instruments-info shape ccxt
# parses): lot step 0.00001, min qty 0.00001, min notional 1 USDT — the same
# TICK_SIZE precision mode as Binance, so the lot/tick math ports unchanged. Bybit
# has no percent trailing, so a TRAILING_DELTA-shaped filter is placed in the dict
# to pin that the registry's trailing kind NONE never reads it.
BYBIT_BTCUSDT: dict[str, Any] = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT",
    "spot": True,
    "swap": False,
    "linear": False,
    "inverse": False,
    "type": "spot",
    "precision": {"amount": 0.00001, "price": 0.01},
    "limits": {
        "amount": {"min": 0.00001, "max": 1000.0},
        "market": {"min": 0.0, "max": 100.0},
        "cost": {"min": 1.0, "max": None},
    },
    "info": {
        "symbol": "BTCUSDT",
        "lotSizeFilter": {"qtyStep": "0.00001", "minOrderQty": "0.00001"},
        "priceFilter": {"tickSize": "0.01"},
        "filters": [
            {
                "filterType": "TRAILING_DELTA",
                "minTrailingBelowDelta": "10",
                "maxTrailingBelowDelta": "2000",
            }
        ],
    },
}


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
        assert r.trailing_delta_bips is None

    @pytest.mark.parametrize(
        ("below", "expected"),
        [
            (("10", "2000"), (Decimal("10"), Decimal("2000"))),
            (("1", "1000000"), (Decimal("1"), Decimal("1000000"))),
        ],
    )
    def test_trailing_delta_bips_from_the_filter(
        self, below: tuple[str, str], expected: tuple[Decimal, Decimal]
    ) -> None:
        market = {
            **BINANCE_BTCUSDT,
            "info": {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                    {
                        "filterType": "TRAILING_DELTA",
                        "minTrailingAboveDelta": "10",
                        "maxTrailingAboveDelta": "2000",
                        "minTrailingBelowDelta": below[0],
                        "maxTrailingBelowDelta": below[1],
                    },
                ]
            },
        }

        assert MarketRules.from_ccxt_market(market).trailing_delta_bips == expected

    def test_no_trailing_delta_filter_is_none(self) -> None:
        market = {
            **BINANCE_BTCUSDT,
            "info": {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]},
        }

        assert MarketRules.from_ccxt_market(market).trailing_delta_bips is None

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
    async def test_unregistered_venues_rejected(self) -> None:
        with pytest.raises(UnsupportedExchangeError):
            await load_market_rules("kraken", ["BTCUSDT"], loader=_Loader())
        with pytest.raises(UnsupportedExchangeError):
            await load_market_rules("okx", ["BTCUSDT"], loader=_Loader())

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


class TestUsdmMarketRules:
    """The binanceusdm venue: the fapi filter mapping and the percent trailing bounds."""

    def test_from_ccxt_market_maps_the_fapi_filters(self) -> None:
        rules = MarketRules.from_ccxt_market(BINANCEUSDM_BTCUSDT_PERP, "binanceusdm")
        assert rules.symbol == "BTCUSDT"
        assert rules.amount_step == Decimal("0.001")
        assert rules.min_qty == Decimal("0.001")
        assert rules.min_notional == Decimal("20.0")
        assert rules.max_market_qty == Decimal("120.0")
        assert rules.price_tick == Decimal("0.1")

    def test_spot_trailing_delta_bips_never_map_onto_usdm_rules(self) -> None:
        """The red pin: usdm trailing stops are the fapi callbackRate PERCENT bounds,
        never spot's TRAILING_DELTA bips — even when the raw filter is present."""
        rules = MarketRules.from_ccxt_market(BINANCEUSDM_BTCUSDT_PERP, "binanceusdm")
        assert rules.trailing_delta_bips is None
        assert rules.trailing_callback_rate_pct == USDM_CALLBACK_RATE_PCT


class TestBybitMarketRules:
    """The bybit venue (P7-F): the same TICK_SIZE precision math, and the registry's
    trailing kind NONE — the protective ladder falls back to the fixed stop."""

    def test_from_ccxt_market_maps_the_spot_filters(self) -> None:
        rules = MarketRules.from_ccxt_market(BYBIT_BTCUSDT, "bybit")
        assert rules.symbol == "BTCUSDT"
        assert rules.amount_step == Decimal("0.00001")
        assert rules.min_qty == Decimal("0.00001")
        assert rules.min_notional == Decimal("1.0")
        assert rules.price_tick == Decimal("0.01")

    def test_floor_qty_and_validate_open_with_real_numbers(self) -> None:
        rules = MarketRules.from_ccxt_market(BYBIT_BTCUSDT, "bybit")
        assert rules.floor_qty(Decimal("0.000037")) == Decimal("0.00003")
        assert rules.validate_open(Decimal("0.00003"), Decimal("30000")) == (
            "BTCUSDT: notional 0.90000 below min notional 1.0"
        )
        assert rules.validate_open(Decimal("0.00004"), Decimal("30000")) is None

    def test_no_trailing_bounds_are_invented_for_bybit(self) -> None:
        """The red pin: bybit has no percent trail (its createTrailingAmountOrder is
        amount-based), so a raw TRAILING_DELTA-shaped filter must never become bounds —
        the ladder falls back to the fixed stop."""
        rules = MarketRules.from_ccxt_market(BYBIT_BTCUSDT, "bybit")
        assert rules.trailing_delta_bips is None
        assert rules.trailing_callback_rate_pct is None

    async def test_bybit_rules_load_through_the_default_loader(self) -> None:
        async def bybit_loader(exchange_id: str) -> list[dict[str, Any]]:
            assert exchange_id == "bybit"
            return [BYBIT_BTCUSDT]

        cache = MarketRulesCache(loader=bybit_loader)
        rules = await cache.get("bybit", ["BTCUSDT"])
        assert rules["BTCUSDT"].amount_step == Decimal("0.00001")
        assert rules["BTCUSDT"].min_notional == Decimal("1.0")
        assert rules["BTCUSDT"].trailing_delta_bips is None

    def test_spot_rules_never_get_the_callback_rate_bounds(self) -> None:
        rules = MarketRules.from_ccxt_market(
            {
                **BINANCE_BTCUSDT,
                "info": {
                    "filters": [
                        {
                            "filterType": "TRAILING_DELTA",
                            "minTrailingBelowDelta": "10",
                            "maxTrailingBelowDelta": "2000",
                        }
                    ]
                },
            }
        )
        assert rules.trailing_callback_rate_pct is None
        assert rules.trailing_delta_bips == (Decimal("10"), Decimal("2000"))

    def test_validate_open_enforces_the_fapi_min_notional(self) -> None:
        rules = MarketRules.from_ccxt_market(BINANCEUSDM_BTCUSDT_PERP, "binanceusdm")
        assert rules.validate_open(Decimal("0.001"), Decimal("60000")) is None
        reason = rules.validate_open(Decimal("0.001"), Decimal("15000"))
        assert reason is not None and "below min notional 20.0" in reason


class _UsdmLoader:
    """The usdm loader test double: linear swap markets only, as binanceusdm loads."""

    def __init__(self, markets: list[Mapping[str, Any]]) -> None:
        self._markets = markets

    async def __call__(self, exchange_id: str) -> Iterable[Mapping[str, Any]]:
        assert exchange_id == "binanceusdm"
        return self._markets


class TestUsdmLoader:
    async def test_usdm_cache_keeps_only_linear_swap_markets(self) -> None:
        loader = _UsdmLoader(
            [
                BINANCEUSDM_BTCUSDT_PERP,
                {**BINANCEUSDM_BTCUSDT_PERP, "id": "ETHUSDT", "linear": False},
                {**BINANCEUSDM_BTCUSDT_PERP, "id": "SOLUSDT", "swap": False},
                {**BINANCE_BTCUSDT, "id": "SPOTUSDT", "spot": True, "linear": True},
            ]
        )
        cache = MarketRulesCache(loader=loader)
        rules = await cache.get("binanceusdm", ["BTCUSDT", "BTC/USDT"])
        assert rules["BTCUSDT"] == MarketRules.from_ccxt_market(
            BINANCEUSDM_BTCUSDT_PERP, "binanceusdm"
        )
        for unknown in ("ETHUSDT", "SOLUSDT", "SPOTUSDT"):
            with pytest.raises(MarketRulesUnavailableError, match=f"linear market '{unknown}'"):
                await cache.get("binanceusdm", [unknown])

    async def test_usdm_rules_flow_through_the_default_loader(self) -> None:
        rules = await load_market_rules(
            "binanceusdm", ["BTCUSDT"], loader=_UsdmLoader([BINANCEUSDM_BTCUSDT_PERP])
        )
        assert rules["BTCUSDT"].trailing_callback_rate_pct == USDM_CALLBACK_RATE_PCT

    async def test_the_public_loader_refuses_unregistered_venues(self) -> None:
        with pytest.raises(UnsupportedExchangeError, match="no market rules for exchange"):
            await public_venue_markets("kraken")
        with pytest.raises(UnsupportedExchangeError, match="no market rules for exchange"):
            await public_venue_markets("okx")

    async def test_the_default_cache_loader_dispatches_per_exchange(self) -> None:
        """The undecorated cache (the backtest runner's) dispatches per exchange id."""
        from hydra.risk import market_rules

        cache = MarketRulesCache()
        assert cache._loader is market_rules.load_public_markets
        assert market_rules._DEFAULT_MARKETS_LOADERS["bybit"] is (market_rules.public_venue_markets)
        with pytest.raises(UnsupportedExchangeError):
            await market_rules.load_public_markets("kraken")
