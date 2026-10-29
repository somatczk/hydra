"""Every ExchangeClient call records latency with an outcome label, and errors are counted."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any

import ccxt
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.execution.exchange_client import ExchangeClient

CALL_SECONDS = 0.25


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeCcxt:
    """Stands in for a ccxt exchange instance; each call takes CALL_SECONDS on the clock."""

    def __init__(self, clock: _Clock, fail: BaseException | None = None) -> None:
        self._clock = clock
        self._fail = fail
        self.calls: list[str] = []

    async def _call(self, name: str, result: Any) -> Any:
        self.calls.append(name)
        self._clock.now += CALL_SECONDS
        if self._fail is not None:
            raise self._fail
        return result

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("create_order", {"id": "o1", **kwargs})

    async def cancel_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._call("cancel_order", {"id": order_id, "symbol": symbol})

    async def fetch_balance(self) -> dict[str, Any]:
        return await self._call(
            "fetch_balance", {"free": {"USDT": 1000.5, "BTC": 0}, "total": {"USDT": 1500.5}}
        )

    async def fetch_open_orders(self, symbol: str | None) -> list[dict[str, Any]]:
        return await self._call("fetch_open_orders", [])

    async def fetch_order(self, id: str, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._call("fetch_order", {"id": id, "status": "NEW"})

    async def fetch_my_trades(self, symbol: str, since: int | None = None) -> list[dict[str, Any]]:
        return await self._call("fetch_my_trades", [])

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return await self._call("fetch_positions", [])

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._call("fetch_ticker", {"last": 65000.1})

    async def set_leverage(self, leverage: int, symbol: str) -> None:
        await self._call("set_leverage", None)

    async def set_margin_mode(self, mode: str, symbol: str) -> None:
        await self._call("set_margin_mode", None)

    async def set_position_mode(self, hedged: bool) -> None:
        await self._call("set_position_mode", None)


async def _fake_post_oco(fake: _FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    return await fake._call("create_oco", {"orderListId": 5, **params})


async def _fake_delete_order_list(fake: _FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    return await fake._call("cancel_order_list", {"code": 200, **params})


async def _fake_delete_open_orders(fake: _FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    return await fake._call("cancel_all_orders", {"code": 200, **params})


async def _fake_get_open_order_lists(
    fake: _FakeCcxt, params: dict[str, Any]
) -> list[dict[str, Any]]:
    return await fake._call("fetch_open_order_lists", [{"orderListId": 5}])


async def _fake_get_account_api_restrictions(
    fake: _FakeCcxt, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await fake._call(
        "fetch_api_restrictions",
        {"ipRestrict": False, "enableWithdrawals": False, "enableSpotAndMarginTrading": True},
    )


async def _fake_v3_get_position_risk(
    fake: _FakeCcxt, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await fake._call(
        "fetch_position_risk",
        [{"symbol": "BTCUSDT", "positionAmt": "0.01", "positionSide": "BOTH"}],
    )


async def _fake_get_income(
    fake: _FakeCcxt, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await fake._call(
        "fetch_funding_income",
        [{"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "-0.01"}],
    )


async def _fake_get_position_side_dual(
    fake: _FakeCcxt, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await fake._call("fetch_position_side_dual", {"dualSidePosition": False})


# ccxt's camelCase implicit API names, bound as attributes (pep8-naming flags the defs)
_FakeCcxt.privatePostOrderListOco = _fake_post_oco
_FakeCcxt.privateDeleteOrderList = _fake_delete_order_list
_FakeCcxt.privateDeleteOpenOrders = _fake_delete_open_orders
_FakeCcxt.privateGetOpenOrderList = _fake_get_open_order_lists
_FakeCcxt.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions
_FakeCcxt.fapiPrivateV3GetPositionRisk = _fake_v3_get_position_risk
_FakeCcxt.fapiPrivateGetIncome = _fake_get_income
_FakeCcxt.fapiPrivateGetPositionSideDual = _fake_get_position_side_dual


ClientCall = Callable[[ExchangeClient], Awaitable[Any]]

CALLS: list[tuple[str, ClientCall]] = [
    (
        "create_order",
        lambda c: c.create_order("BTC/USDT", "BUY", "MARKET", Decimal("0.01")),
    ),
    (
        "create_oco",
        lambda c: c.create_oco("BTCUSDT", "SELL", "0.01000", "103.00", "98.00"),
    ),
    ("cancel_order", lambda c: c.cancel_order("o1", "BTC/USDT")),
    ("cancel_order_list", lambda c: c.cancel_order_list("5", "BTCUSDT")),
    ("cancel_all_orders", lambda c: c.cancel_all_orders("BTCUSDT")),
    ("fetch_balance", lambda c: c.fetch_balance()),
    ("fetch_open_orders", lambda c: c.fetch_open_orders("BTC/USDT")),
    ("fetch_open_order_lists", lambda c: c.fetch_open_order_lists("BTCUSDT")),
    (
        "fetch_api_restrictions",
        lambda c: c.fetch_api_restrictions(),
    ),
    (
        "fetch_order",
        lambda c: c.fetch_order("o1", "BTCUSDT", params={"clientOrderId": "hyd-1"}),
    ),
    ("fetch_my_trades", lambda c: c.fetch_my_trades("BTCUSDT", since=None)),
    ("fetch_positions", lambda c: c.fetch_positions("BTC/USDT")),
    ("fetch_ticker", lambda c: c.get_last_price("BTC/USDT")),
    ("set_leverage", lambda c: c.set_leverage("BTC/USDT", 2)),
    ("set_margin_mode", lambda c: c.set_margin_mode("BTC/USDT", "isolated")),
    ("set_position_mode", lambda c: c.set_one_way_position_mode()),
    ("fetch_position_risk", lambda c: c.fetch_position_risk("BTCUSDT")),
    ("fetch_funding_income", lambda c: c.fetch_funding_income("BTCUSDT", since_ms=1)),
    ("fetch_position_side_dual", lambda c: c.fetch_position_side_dual()),
]


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(metrics, "_monotonic", fake)
    return fake


def _client(fake: _FakeCcxt) -> ExchangeClient:
    client = ExchangeClient("binance", {})
    client._exchange = fake
    return client


def _latency(endpoint: str, outcome: str, suffix: str) -> float | None:
    labels = {"exchange_id": "binance", "endpoint": endpoint, "outcome": outcome}
    return REGISTRY.get_sample_value(f"hydra_exchange_api_latency_seconds_{suffix}", labels)


def _errors(endpoint: str, error_type: str) -> float | None:
    labels = {"exchange_id": "binance", "endpoint": endpoint, "error_type": error_type}
    return REGISTRY.get_sample_value("hydra_exchange_api_errors_total", labels)


async def test_successful_fetch_balance_records_ok_latency(clock: _Clock) -> None:
    balances = await _client(_FakeCcxt(clock)).fetch_balance()

    assert balances == {"USDT": Decimal("1000.5")}
    assert _latency("fetch_balance", "ok", "count") == 1.0
    assert _latency("fetch_balance", "ok", "sum") == CALL_SECONDS
    assert _latency("fetch_balance", "error", "count") is None
    assert REGISTRY.get_sample_value("hydra_exchange_api_errors_total", {}) is None


async def test_fetch_balance_totals_reads_the_wallet_total(clock: _Clock) -> None:
    """A2: the reconcile read is the wallet total (free + locked), not the free balance."""
    totals = await _client(_FakeCcxt(clock)).fetch_balance_totals()

    assert totals == {"USDT": Decimal("1500.5")}
    assert _latency("fetch_balance", "ok", "count") == 1.0


@pytest.mark.parametrize(("endpoint", "call"), CALLS, ids=[e for e, _ in CALLS])
async def test_every_call_is_observed_on_success(
    clock: _Clock, endpoint: str, call: ClientCall
) -> None:
    fake = _FakeCcxt(clock)
    await call(_client(fake))

    assert fake.calls == [endpoint]
    assert _latency(endpoint, "ok", "count") == 1.0
    assert _latency(endpoint, "ok", "sum") == CALL_SECONDS


@pytest.mark.parametrize(("endpoint", "call"), CALLS, ids=[e for e, _ in CALLS])
async def test_every_call_counts_errors_and_reraises(
    clock: _Clock, endpoint: str, call: ClientCall
) -> None:
    with pytest.raises(ccxt.InsufficientFunds):
        await call(_client(_FakeCcxt(clock, fail=ccxt.InsufficientFunds("no funds"))))

    assert _latency(endpoint, "error", "count") == 1.0
    assert _latency(endpoint, "error", "sum") == CALL_SECONDS
    assert _latency(endpoint, "ok", "count") is None
    assert _errors(endpoint, "InsufficientFunds") == 1.0


async def test_ccxt_request_timeout_is_timeout_outcome(clock: _Clock) -> None:
    client = _client(_FakeCcxt(clock, fail=ccxt.RequestTimeout("binance GET timed out")))
    with pytest.raises(ccxt.RequestTimeout):
        await client.create_order("BTC/USDT", "SELL", "MARKET", Decimal("0.01"))

    assert _latency("create_order", "timeout", "count") == 1.0
    assert _latency("create_order", "timeout", "sum") == CALL_SECONDS
    assert _errors("create_order", "RequestTimeout") == 1.0


async def test_builtin_timeout_is_timeout_outcome(clock: _Clock) -> None:
    client = _client(_FakeCcxt(clock, fail=TimeoutError()))
    with pytest.raises(TimeoutError):
        await client.cancel_order("o1", "BTC/USDT")

    assert _latency("cancel_order", "timeout", "count") == 1.0
    assert _errors("cancel_order", "TimeoutError") == 1.0


async def test_cancelled_call_is_observed_but_not_an_error(clock: _Clock) -> None:
    client = _client(_FakeCcxt(clock, fail=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await client.fetch_open_orders("BTC/USDT")

    assert _latency("fetch_open_orders", "cancelled", "count") == 1.0
    assert _errors("fetch_open_orders", "CancelledError") is None


async def test_outcomes_accumulate_per_endpoint(clock: _Clock) -> None:
    ok = _client(_FakeCcxt(clock))
    failing = _client(_FakeCcxt(clock, fail=ccxt.ExchangeNotAvailable("503")))
    for _ in range(3):
        await ok.get_last_price("BTC/USDT")
    with pytest.raises(ccxt.ExchangeNotAvailable):
        await failing.get_last_price("BTC/USDT")

    assert _latency("fetch_ticker", "ok", "count") == 3.0
    assert _latency("fetch_ticker", "error", "count") == 1.0
    assert _errors("fetch_ticker", "ExchangeNotAvailable") == 1.0


CLIENT_METHOD = {"fetch_ticker": "get_last_price", "set_position_mode": "set_one_way_position_mode"}
DELEGATES = {
    "get_balance": "fetch_balance",
    "fetch_balance_totals": "fetch_balance",
    "get_equity": "fetch_balance",
    "get_positions": "fetch_positions",
}
NOT_REST_CALLS = {"get_filled_orders", "start_user_data_stream", "close"}


def test_calls_cover_every_public_async_method() -> None:
    public = {
        name
        for name, _ in inspect.getmembers(ExchangeClient, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    covered = {CLIENT_METHOD.get(endpoint, endpoint) for endpoint, _ in CALLS}
    assert public - set(DELEGATES) - NOT_REST_CALLS == covered


@pytest.mark.parametrize(("method", "endpoint"), sorted(DELEGATES.items()))
async def test_delegating_wrappers_are_observed_through_their_call(
    clock: _Clock, method: str, endpoint: str
) -> None:
    fake = _FakeCcxt(clock)
    await getattr(_client(fake), method)()
    assert fake.calls == [endpoint]
    assert _latency(endpoint, "ok", "count") == 1.0
