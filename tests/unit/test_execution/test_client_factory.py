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
        ("bybit", MarketType.FUTURES),
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


def test_the_refusal_names_the_venues_and_the_unregistered() -> None:
    """K/O are named 'not scheduled' (the honest README); bybit FUTURES is refused —
    bybit is a spot-only venue."""
    with pytest.raises(UnsupportedVenueError, match="binance FUTURES is not supported"):
        client_factory.validate_venue("binance", MarketType.FUTURES)
    with pytest.raises(UnsupportedVenueError, match="not scheduled"):
        client_factory.validate_venue("kraken", MarketType.SPOT)
    with pytest.raises(UnsupportedVenueError, match="okx FUTURES is not supported"):
        client_factory.validate_venue("okx", MarketType.FUTURES)
    with pytest.raises(UnsupportedVenueError, match="binanceusdm SPOT is not supported"):
        client_factory.validate_venue("binanceusdm", MarketType.SPOT)


class TestTheModeCaps:
    """The registry's per-venue mode caps (P7-F): bybit is LIVE-ONLY."""

    def test_bybit_spot_paper_and_backtest_are_refused(self) -> None:
        with pytest.raises(UnsupportedVenueError, match="live-only venue") as err:
            client_factory.validate_venue("bybit", MarketType.SPOT)
        assert "paper and backtest" in str(err.value)

    def test_bybit_spot_live_is_admitted(self) -> None:
        assert client_factory.validate_venue("bybit", MarketType.SPOT, mode="live") == "bybit"
        assert client_factory.ccxt_class_for("bybit", MarketType.SPOT) == "bybit"

    def test_binance_admits_both_modes(self) -> None:
        assert client_factory.validate_venue("binance", MarketType.SPOT, mode="paper") == "binance"
        assert client_factory.validate_venue("binance", MarketType.SPOT, mode="live") == "binance"
        assert (
            client_factory.validate_venue("binanceusdm", MarketType.FUTURES, mode="live")
            == "binanceusdm"
        )

    @pytest.mark.parametrize(
        "builder",
        [
            client_factory.build_readonly_client,
            client_factory.build_trading_client,
        ],
    )
    def test_the_keyed_client_builders_are_the_live_path(self, builder: Any) -> None:
        """The keyed ceremony path admits the live-only venue (mode caps: live)."""
        client = builder("bybit", {"api_key": "k", "api_secret": "s"}, testnet=True)
        assert client._exchange_id == "bybit"


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


def test_readonly_client_refuses_unregistered_venues() -> None:
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_readonly_client(
            "kraken", {"api_key": "k", "api_secret": "s"}, testnet=True
        )


@pytest.mark.parametrize("testnet", [True, False])
async def test_bybit_clients_build_the_bybit_class(testnet: bool) -> None:
    """bybit SPOT joins the venue set (P7-F): offline construction of the real ccxt
    class, the spot options pinned by the registry (bybit's own default is 'swap')."""
    for client in (
        client_factory.build_readonly_client(
            "bybit", {"api_key": "k", "api_secret": "s"}, testnet=testnet
        ),
        client_factory.build_trading_client(
            "bybit", {"api_key": "k", "api_secret": "s"}, testnet=testnet
        ),
    ):
        exchange = client._get_exchange()
        try:
            assert type(exchange).__name__ == "bybit"
            assert (exchange.apiKey, exchange.secret) == ("k", "s")
            assert exchange.isSandboxModeEnabled is testnet
            assert exchange.options["defaultType"] == "spot"
        finally:
            await client.close()


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


def test_build_live_executor_admits_bybit_live() -> None:
    """The mode caps' admission: only ``build_live_executor`` passes live, so bybit —
    the live-only venue — builds its live spot executor (P7-F)."""
    fake = object()
    executor = client_factory.build_live_executor(
        "bybit",
        client=fake,
        market_rules={"BTCUSDT": _rules()},
        db_pool=object(),
        strategy_id="s",
        session_id="sess",
        initial_quote=Decimal("100"),
        poll_interval=5.0,
    )
    assert executor.exchange_client is fake
    assert executor._pair_venue is True
    assert executor.get_account_snapshot().quote_free == Decimal("100")


def test_build_live_executor_refuses_unregistered_venues() -> None:
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_live_executor(
            "kraken",
            client=object(),
            market_rules={"BTCUSDT": _rules()},
            db_pool=object(),
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
        )


def test_build_live_executor_refuses_the_paper_only_path_for_bybit() -> None:
    """The paper builders keep today's semantics: bybit paper executors are refused."""
    with pytest.raises(UnsupportedVenueError, match="live-only"):
        client_factory.build_paper_executor(
            "bybit", {"USDT": Decimal("100")}, object(), "s", "sess"
        )
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_perp_paper_executor(
            "bybit", {"USDT": Decimal("100")}, object(), "s", "sess"
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


def test_feed_and_rest_factories_build_the_bybit_class() -> None:
    """bybit's public feed and REST clients: the registry's options pin the spot
    category (bybit's own defaultType default is 'swap')."""
    feed = client_factory.feed_exchange_factory("bybit")()
    rest = client_factory.rest_exchange_factory("bybit")
    for inst in (feed, rest):
        assert inst.id == "bybit"
        assert inst.options["defaultType"] == "spot"
        assert inst.options["fetchMarkets"]["types"] == ["spot"]


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


def test_build_live_executor_builds_the_perp_executor_for_futures() -> None:
    from hydra.execution.perp_live import LivePerpTradingExecutor

    fake = object()
    executor = client_factory.build_live_executor(
        "binanceusdm",
        client=fake,
        market_rules={"BTCUSDT": _rules()},
        db_pool=object(),
        strategy_id="s",
        session_id="sess",
        initial_quote=Decimal("100"),
        poll_interval=5.0,
        market_type="FUTURES",
        leverage=2,
    )
    assert isinstance(executor, LivePerpTradingExecutor)
    assert executor.exchange_client is fake
    assert executor.leverage == 2
    assert executor.get_account_snapshot().wallet_available == Decimal("100")


def test_build_live_executor_refuses_the_futures_venue_without_the_market_type() -> None:
    """The P4-C fail-closed refusal still stands for a mismatched pair: binanceusdm
    must ask for FUTURES explicitly (and every live surface checks the perps gate
    before this build is even reached)."""
    with pytest.raises(UnsupportedVenueError):
        client_factory.build_live_executor(
            "binanceusdm",
            client=object(),
            market_rules={"BTCUSDT": _rules()},
            db_pool=object(),
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
        )


def test_build_live_executor_futures_needs_rules_and_a_pool_too() -> None:
    with pytest.raises(ValueError, match="MarketRules"):
        client_factory.build_live_executor(
            "binanceusdm",
            client=object(),
            market_rules={"BTCUSDT": SimpleNamespace(amount_step=Decimal("0.00001"))},
            db_pool=object(),
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
            market_type="FUTURES",
            leverage=2,
        )
    with pytest.raises(ValueError, match="database"):
        client_factory.build_live_executor(
            "binanceusdm",
            client=object(),
            market_rules={"BTCUSDT": _rules()},
            db_pool=None,
            strategy_id="s",
            session_id="sess",
            initial_quote=Decimal("100"),
            market_type="FUTURES",
            leverage=2,
        )
