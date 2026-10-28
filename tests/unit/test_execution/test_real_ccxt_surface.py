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
