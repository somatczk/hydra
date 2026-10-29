"""Fresh-real-ccxt surface guard: the implicit (camelCase) method names hydra calls must exist.

``ExchangeClient.fetch_api_restrictions`` shipped calling ``privateGetAccountApiRestrictions`` —
a name real ccxt has never generated for GET /sapi/v1/account/apiRestrictions (the real implicit
method is ``sapiGetAccountApiRestrictions``, the sapi-namespaced one), and every live-path test
faked the bogus name, so CI was blind to the ``AttributeError`` a real mainnet key connect and
the R8 arm re-fetch die with. ccxt generates its implicit API surface offline from the endpoint
definitions — construction is keyless, auth and the HTTP session are lazy — so this file builds
the real classes (sync and async, spot and futures) and asserts every name hydra's
``ExchangeClient`` calls exists, plus the fapi names the futures live work will call. Nothing
here sends a request.
"""

from __future__ import annotations

from typing import Any

import ccxt
import ccxt.async_support

from hydra.core.types import MarketType
from hydra.execution.exchange_client import ExchangeClient

SPOT_IMPLICIT_METHODS: tuple[str, ...] = (
    "privatePostOrderListOco",
    "privateDeleteOrderList",
    "privateDeleteOpenOrders",
    "privateGetOpenOrderList",
    "sapiGetAccountApiRestrictions",
)

BOGUS_API_RESTRICTIONS_NAME = "privateGetAccountApiRestrictions"

FAPI_IMPLICIT_METHODS: tuple[str, ...] = (
    "fapiPrivateGetIncome",
    "fapiPrivateGetPositionRisk",
    "fapiPrivatePostLeverage",
    "fapiPrivatePostMarginType",
    "fapiPrivatePostPositionSideDual",
    "fapiPrivateGetUserTrades",
    "fapiPrivateDeleteAllOpenOrders",
)

# The live perps executor's own calls (P4-D), each pinned to its call site on the
# installed ccxt: ccxt 4.5.88 routes every conditional futures order through the
# fapi ALGO endpoints, so the ladder's placements/resolves/cancels live there while
# plain MARKET/LIMIT orders ride POST /fapi/v1/order; the wallet read is the V3
# account endpoint (fetch_balance/fetch_balance_totals), the position truth the V3
# positionRisk endpoint, and the venue setup the leverage/marginType/positionSide
# trio plus its GET. A ccxt upgrade that renames or removes any of these fails here
# BEFORE a live session can hit the AttributeError the P4-A hotfix taught us about.
FAPI_LIVE_IMPLICIT_METHODS: tuple[str, ...] = (
    # plain MARKET/LIMIT placements, reads and cancels (POST/GET/DELETE /fapi/v1/order)
    "fapiPrivatePostOrder",
    "fapiPrivateGetOrder",
    "fapiPrivateDeleteOrder",
    # open orders in each namespace (GET /fapi/v1/openOrders and openAlgoOrders)
    "fapiPrivateGetOpenOrders",
    "fapiPrivateGetOpenAlgoOrders",
    # the conditional ladder: placements, reads and cancels through the ALGO api
    "fapiPrivatePostAlgoOrder",
    "fapiPrivateGetAlgoOrder",
    "fapiPrivateDeleteAlgoOrder",
    # the kill-switch sweep's second half (DELETE /fapi/v1/algoOpenOrders)
    "fapiPrivateDeleteAlgoOpenOrders",
    # the wallet truth (GET /fapi/v3/account) and the position truth (V3 positionRisk)
    "fapiPrivateV3GetAccount",
    "fapiPrivateV3GetPositionRisk",
    # the one-way position mode's read (GET /fapi/v1/positionSide/dual)
    "fapiPrivateGetPositionSideDual",
)

# The funding writer (P4-B) fetches history through the unified fetchFundingRateHistory,
# which resolves to the fapi public fundingRate endpoint for linear markets.
FUNDING_IMPLICIT_METHODS: tuple[str, ...] = (
    "fapiPublicGetFundingRate",
    "fetchFundingRateHistory",
)


def _missing(exchange: Any, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not hasattr(exchange, name)]


def test_binance_spot_implicit_methods_exist_sync() -> None:
    exchange = ccxt.binance()
    missing = _missing(exchange, SPOT_IMPLICIT_METHODS)
    assert not missing, f"ccxt {ccxt.__version__} binance lacks implicit methods: {missing}"
    assert not hasattr(exchange, BOGUS_API_RESTRICTIONS_NAME)


async def test_binance_spot_implicit_methods_exist_async() -> None:
    exchange = ccxt.async_support.binance()
    try:
        missing = _missing(exchange, SPOT_IMPLICIT_METHODS)
        assert not missing, f"ccxt {ccxt.__version__} async binance lacks: {missing}"
        assert not hasattr(exchange, BOGUS_API_RESTRICTIONS_NAME)
    finally:
        await exchange.close()


def test_binanceusdm_sapi_api_restrictions_exists_sync() -> None:
    """The perps arm re-fetches the futures permission on the binanceusdm class (the
    #220 review's A2): the sapi name must exist there too, as it inherits binance's."""
    exchange = ccxt.binanceusdm()
    missing = _missing(exchange, ("sapiGetAccountApiRestrictions",))
    assert not missing, f"ccxt {ccxt.__version__} binanceusdm lacks: {missing}"


async def test_binanceusdm_sapi_api_restrictions_exists_async() -> None:
    exchange = ccxt.async_support.binanceusdm()
    try:
        missing = _missing(exchange, ("sapiGetAccountApiRestrictions",))
        assert not missing, f"ccxt {ccxt.__version__} async binanceusdm lacks: {missing}"
    finally:
        await exchange.close()


def test_binanceusdm_fapi_implicit_methods_exist_sync() -> None:
    missing = _missing(ccxt.binanceusdm(), FAPI_IMPLICIT_METHODS)
    assert not missing, f"ccxt {ccxt.__version__} binanceusdm lacks implicit methods: {missing}"


async def test_binanceusdm_fapi_implicit_methods_exist_async() -> None:
    exchange = ccxt.async_support.binanceusdm()
    try:
        missing = _missing(exchange, FAPI_IMPLICIT_METHODS)
        assert not missing, f"ccxt {ccxt.__version__} async binanceusdm lacks: {missing}"
    finally:
        await exchange.close()


def test_binanceusdm_funding_surface_exists_sync() -> None:
    """The funding writer's names on a fresh real binanceusdm (construction-only)."""
    exchange = ccxt.binanceusdm()
    missing = _missing(exchange, FUNDING_IMPLICIT_METHODS)
    assert not missing, f"ccxt {ccxt.__version__} binanceusdm lacks funding surface: {missing}"
    assert exchange.has.get("fetchFundingRateHistory") is True


async def test_binanceusdm_funding_surface_exists_async() -> None:
    exchange = ccxt.async_support.binanceusdm()
    try:
        missing = _missing(exchange, FUNDING_IMPLICIT_METHODS)
        assert not missing, f"ccxt {ccxt.__version__} async binanceusdm lacks funding: {missing}"
        assert exchange.has.get("fetchFundingRateHistory") is True
    finally:
        await exchange.close()


def test_binanceusdm_live_perps_implicit_methods_exist_sync() -> None:
    """Every implicit name the live perps executor reaches (P4-D): the plain order
    endpoints, BOTH open-order namespaces, the ALGO trio the conditional ladder
    rides on 4.5.88, the algo cancel-all half of the kill-switch sweep, the V3
    wallet + positionRisk reads and the positionSide GET. Construction only."""
    missing = _missing(ccxt.binanceusdm(), FAPI_LIVE_IMPLICIT_METHODS)
    assert not missing, f"ccxt {ccxt.__version__} binanceusdm lacks: {missing}"


async def test_binanceusdm_live_perps_implicit_methods_exist_async() -> None:
    exchange = ccxt.async_support.binanceusdm()
    try:
        missing = _missing(exchange, FAPI_LIVE_IMPLICIT_METHODS)
        assert not missing, f"ccxt {ccxt.__version__} async binanceusdm lacks: {missing}"
    finally:
        await exchange.close()


def test_binanceusdm_routes_conditional_orders_through_the_algo_api() -> None:
    """The wire assumption the ladder is built on: on 4.5.88 a futures order whose
    params name a trigger (stopLossPrice/takeProfitPrice/callbackRate) is CONDITIONAL
    and resolves to the ALGO endpoints, while a plain order resolves to /fapi/v1/order."""
    exchange = ccxt.binanceusdm()
    assert exchange.is_conditional_order({"stopLossPrice": "95000"}), (
        "a stopLossPrice order must classify as conditional on binanceusdm"
    )
    assert exchange.is_conditional_order({"takeProfitPrice": "105000"}), (
        "a takeProfitPrice order must classify as conditional on binanceusdm"
    )
    assert exchange.is_conditional_order({"callbackRate": "2.5"}), (
        "a callbackRate order must classify as conditional on binanceusdm"
    )
    assert not exchange.is_conditional_order({"reduceOnly": True})


async def test_binanceusdm_routes_the_wrappers_conditional_read_to_the_algo_namespace() -> None:
    """The SEAM call, pinned on a fresh real binanceusdm (construction-only): the
    wrapper's ``conditional=True`` read must hit ``fapiPrivateGetOpenAlgoOrders``
    and the unconditional read ``fapiPrivateGetOpenOrders``. The P4-D review's A1:
    passing the params dict positionally bound it to ``since`` (the 4.5.88
    signature is ``(symbol, since, limit, params)``), so the ladder's namespace
    was never read and every armed perp ladder verified ``missing``."""
    exchange = ccxt.async_support.binanceusdm({"apiKey": "x", "secret": "y"})
    try:
        exchange.set_markets(
            [
                {
                    "id": "BTCUSDT",
                    "symbol": "BTC/USDT:USDT",
                    "base": "BTC",
                    "quote": "USDT",
                    "type": "swap",
                    "spot": False,
                    "swap": True,
                    "future": False,
                    "option": False,
                    "margin": False,
                    "linear": True,
                    "inverse": False,
                    "contract": True,
                }
            ]
        )
        calls: list[str] = []

        async def _algo_open(request: Any = None, params: Any = None) -> list[dict[str, Any]]:
            calls.append("algo")
            return []

        async def _plain_open(request: Any = None, params: Any = None) -> list[dict[str, Any]]:
            calls.append("plain")
            return []

        exchange.fapiPrivateGetOpenAlgoOrders = _algo_open
        exchange.fapiPrivateGetOpenOrders = _plain_open
        client = ExchangeClient("binanceusdm", {}, market_type=MarketType.FUTURES)
        client._exchange = exchange

        assert await client.fetch_open_orders("BTC/USDT:USDT", conditional=True) == []
        assert calls == ["algo"], (
            "the wrapper's conditional read must hit the fapi ALGO namespace, not the "
            f"regular one (hit: {calls})"
        )
        calls.clear()
        assert await client.fetch_open_orders("BTC/USDT:USDT") == []
        assert calls == ["plain"], (
            f"the wrapper's unconditional read must stay on the regular namespace (hit: {calls})"
        )
    finally:
        await exchange.close()
