"""client_factory: the venue gate (binance SPOT, binanceusdm FUTURES), credential mapping,
offline client construction."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from hydra.core.types import MarketType
from hydra.execution import client_factory
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.exchange_client import ExchangeClient
from hydra.risk.market_rules import MarketRules


def _rules() -> MarketRules:
    return MarketRules(
        symbol="BTCUSDT",
        amount_step=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.01"),
    )


def test_binance_spot_is_the_only_venue() -> None:
    assert client_factory.validate_venue("binance", MarketType.SPOT) == "binance"
    assert client_factory.validate_venue("binance", "spot") == "binance"
    assert client_factory.ccxt_class_for("binance", MarketType.SPOT) == "binance"


def test_binanceusdm_futures_is_the_second_venue() -> None:
    assert client_factory.validate_venue("binanceusdm", MarketType.FUTURES) == "binanceusdm"
    assert client_factory.validate_venue("binanceusdm", "futures") == "binanceusdm"
    assert client_factory.ccxt_class_for("binanceusdm", MarketType.FUTURES) == "binanceusdm"


@pytest.mark.parametrize(
    ("exchange_id", "market_type"),
    [
        ("binance", MarketType.FUTURES),
        ("binanceusdm", MarketType.SPOT),
        ("bybit", MarketType.SPOT),
        ("kraken", MarketType.SPOT),
        ("okx", MarketType.FUTURES),
        ("binance", "margin"),
    ],
)
def test_other_venues_are_refused(exchange_id: str, market_type: Any) -> None:
    with pytest.raises(UnsupportedVenueError) as err:
        client_factory.validate_venue(exchange_id, market_type)
    assert err.value.code == "unsupported_venue"
    with pytest.raises(UnsupportedVenueError):
        client_factory.ccxt_class_for(exchange_id, market_type)


def test_the_refusal_names_both_venues() -> None:
    with pytest.raises(UnsupportedVenueError, match="binance FUTURES is not supported") as err:
        client_factory.validate_venue("binance", MarketType.FUTURES)
    assert "binance SPOT and binanceusdm FUTURES" in str(err.value)
    with pytest.raises(UnsupportedVenueError, match="binanceusdm SPOT is not supported"):
        client_factory.validate_venue("binanceusdm", MarketType.SPOT)


def test_map_credentials() -> None:
    assert client_factory.map_credentials({"api_key": "k", "api_secret": "s"}) == {
        "apiKey": "k",
        "secret": "s",
    }
    assert client_factory.map_credentials(
        {"api_key": "k", "api_secret": "s", "passphrase": "p"}
    ) == {"apiKey": "k", "secret": "s", "password": "p"}
    with pytest.raises(ValueError, match="api_key and api_secret"):
        client_factory.map_credentials({"api_key": "k"})


@pytest.mark.parametrize("testnet", [True, False])
async def test_readonly_client_is_spot_and_sandbox_only_on_testnet(testnet: bool) -> None:
    client = client_factory.build_readonly_client(
        "binance", {"api_key": "k", "api_secret": "s"}, testnet=testnet
    )
    exchange = client._get_exchange()
    try:
        assert type(exchange).__name__ == "binance"
        assert (exchange.apiKey, exchange.secret) == ("k", "s")
        assert exchange.isSandboxModeEnabled is testnet
    finally:
        await client.close()


def test_readonly_client_refuses_other_venues() -> None:
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_readonly_client(
            "bybit", {"api_key": "k", "api_secret": "s"}, testnet=True
        )


@pytest.mark.parametrize("testnet", [True, False])
async def test_usdm_clients_build_the_binanceusdm_class(testnet: bool) -> None:
    """The FUTURES venue builds the binanceusdm class; sandbox means the futures testnet."""
    for client in (
        client_factory.build_readonly_client(
            "binanceusdm",
            {"api_key": "k", "api_secret": "s"},
            testnet=testnet,
            market_type=MarketType.FUTURES,
        ),
        client_factory.build_trading_client(
            "binanceusdm",
            {"api_key": "k", "api_secret": "s"},
            testnet=testnet,
            market_type=MarketType.FUTURES,
        ),
    ):
        exchange = client._get_exchange()
        try:
            assert type(exchange).__name__ == "binanceusdm"
            assert (exchange.apiKey, exchange.secret) == ("k", "s")
            assert exchange.isSandboxModeEnabled is testnet
            fapi_url = str(exchange.urls["api"]["fapiPublic"])
            expected_host = "testnet.binancefuture.com" if testnet else "fapi.binance.com"
            assert urlsplit(fapi_url).hostname == expected_host
        finally:
            await client.close()


def test_usdm_clients_refuse_the_spot_market_type() -> None:
    for builder in (client_factory.build_readonly_client, client_factory.build_trading_client):
        with pytest.raises(UnsupportedVenueError, match="binanceusdm SPOT is not supported"):
            builder("binanceusdm", {"api_key": "k", "api_secret": "s"}, testnet=True)


def test_build_live_executor_constructs_the_live_executor() -> None:
    fake = object()
    executor = client_factory.build_live_executor(
        "binance",
        client=fake,
        market_rules={"BTCUSDT": _rules()},
        db_pool=object(),
        strategy_id="s",
        session_id="sess",
        initial_quote=Decimal("100"),
        poll_interval=5.0,
    )
    assert executor.exchange_client is fake
    assert executor.poll_interval == 5.0
    assert executor.get_account_snapshot().quote_free == Decimal("100")


def test_build_live_executor_refuses_other_venues() -> None:
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_live_executor(
            "bybit",
            client=object(),
            market_rules={"BTCUSDT": _rules()},
            db_pool=object(),
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
        )


def test_build_live_executor_needs_full_market_rules_and_a_pool() -> None:
    with pytest.raises(ValueError, match="MarketRules"):
        client_factory.build_live_executor(
            "binance",
            client=object(),
            market_rules={"BTCUSDT": SimpleNamespace(amount_step=Decimal("0.00001"))},
            db_pool=object(),
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
        )
    with pytest.raises(ValueError, match="database"):
        client_factory.build_live_executor(
            "binance",
            client=object(),
            market_rules={"BTCUSDT": _rules()},
            db_pool=None,
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
        )


async def test_trading_client_maps_credentials_and_stays_off_mainnet_on_testnet() -> None:
    client = client_factory.build_trading_client(
        "binance", {"api_key": "k", "api_secret": "s"}, testnet=True
    )
    exchange = client._get_exchange()
    try:
        assert (exchange.apiKey, exchange.secret) == ("k", "s")
        assert exchange.isSandboxModeEnabled is True
    finally:
        await client.close()
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_trading_client(
            "kraken", {"api_key": "k", "api_secret": "s"}, testnet=False
        )


def test_feed_and_rest_factories_build_spot_classes() -> None:
    feed = client_factory.feed_exchange_factory("binance")()
    rest = client_factory.rest_exchange_factory("binance")
    for inst in (feed, rest):
        assert inst.id == "binance"
        assert inst.options["defaultType"] == "spot"
        assert inst.options["fetchMarkets"]["types"] == ["spot"]
    with pytest.raises(UnsupportedVenueError):
        client_factory.feed_exchange_factory("kraken")


def test_feed_and_rest_factories_build_the_usdm_class_for_binanceusdm() -> None:
    """The usdm factory path: the binanceusdm class with the linear-swap-only options
    (verified against the installed ccxt's binanceusdm describe())."""
    feed = client_factory.feed_exchange_factory("binanceusdm")()
    rest = client_factory.rest_exchange_factory("binanceusdm")
    for inst in (feed, rest):
        assert inst.id == "binanceusdm"
        assert inst.options["defaultType"] == "swap"
        assert inst.options["defaultSubType"] == "linear"
        assert inst.options["fetchMarkets"]["types"] == ["linear"]
    with pytest.raises(UnsupportedVenueError):
        client_factory.rest_exchange_factory("binanceusd")


class _Ccxt:
    def __init__(self) -> None:
        self.tickers: list[str] = []

    async def fetch_balance(self) -> dict[str, Any]:
        return {"total": {"USDT": 1000.5, "BTC": 0.02, "ETH": 0, "SOL": None}}

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self.tickers.append(symbol)
        return {"last": 60000.0}


async def test_exchange_client_get_equity_marks_every_asset() -> None:
    client = ExchangeClient("binance", {}, testnet=True)
    fake = _Ccxt()
    client._exchange = fake
    assert await client.get_equity() == Decimal("1000.5") + Decimal("0.02") * Decimal("60000.0")
    assert fake.tickers == ["BTC/USDT"]
