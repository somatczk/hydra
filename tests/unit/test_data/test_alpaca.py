"""AlpacaClient over httpx.MockTransport: labels, request shape, error mapping (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt
import httpx
import pytest

from hydra.core.types import Timeframe
from hydra.data.alpaca import AlpacaClient, session_label_ms
from hydra.data.backfill import fetch_closed_bars

NOW = datetime(2024, 4, 2, 14, 30, tzinfo=UTC)


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp() * 1000)


def _bar(t: str, close: float) -> dict[str, Any]:
    return {"t": t, "o": close - 1, "h": close + 1, "l": close - 2, "c": close, "v": 1000}


class _Alpaca:
    """Answers the bars, asset and account endpoints; records every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def _client(fake: _Alpaca) -> AlpacaClient:
    return AlpacaClient("key-id", "secret", transport=httpx.MockTransport(fake), clock=lambda: NOW)


def test_bars_are_labelled_at_midnight_utc_of_the_new_york_session_date() -> None:
    assert session_label_ms("2024-03-28T04:00:00Z") == _ms("2024-03-28")  # EDT
    assert session_label_ms("2024-01-02T05:00:00Z") == _ms("2024-01-02")  # EST
    # 22:00 New York on Jan 2 is already Jan 3 in UTC: the session date wins
    assert session_label_ms("2024-01-03T03:00:00Z") == _ms("2024-01-02")


async def test_fetch_ohlcv_asks_for_split_adjusted_sip_days_ending_16_minutes_ago() -> None:
    fake = _Alpaca(
        httpx.Response(
            200,
            json={
                "bars": [_bar("2024-01-02T05:00:00Z", 185.64), _bar("2024-03-28T04:00:00Z", 171.48)]
            },
        )
    )
    rows = await _client(fake).fetch_ohlcv("AAPL/USD", "1d", since=_ms("2024-01-02"), limit=1000)
    assert rows == [
        [_ms("2024-01-02"), 184.64, 186.64, 183.64, 185.64, 1000],
        [_ms("2024-03-28"), 170.48, 172.48, 169.48, 171.48, 1000],
    ]
    [request] = fake.requests
    assert request.url.host == "data.alpaca.markets"
    assert request.url.path == "/v2/stocks/AAPL/bars"
    assert dict(request.url.params) == {
        "timeframe": "1Day",
        "adjustment": "split",
        "feed": "sip",
        "sort": "asc",
        "start": "2024-01-02T00:00:00Z",
        "end": "2024-04-02T14:14:00Z",
        "limit": "1000",
    }
    assert request.headers["APCA-API-KEY-ID"] == "key-id"
    assert request.headers["APCA-API-SECRET-KEY"] == "secret"


async def test_a_429_is_retried_by_the_page_fetch() -> None:
    fake = _Alpaca(
        httpx.Response(429, json={"message": "too many requests"}),
        httpx.Response(200, json={"bars": [_bar("2024-03-28T04:00:00Z", 171.48)]}),
        httpx.Response(200, json={"bars": []}),
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    bars = await fetch_closed_bars(
        _client(fake),
        "AAPL/USD",
        Timeframe.D1,
        datetime(2024, 3, 28, tzinfo=UTC),
        datetime(2024, 3, 29, tzinfo=UTC),
        exchange_id="alpaca",
        now=NOW,
        sleep=sleep,
    )
    assert [(b.timestamp, b.close) for b in bars] == [
        (datetime(2024, 3, 28, tzinfo=UTC), Decimal("171.48"))
    ]
    assert len(slept) == 1
    assert [r.url.params["start"] for r in fake.requests] == [
        "2024-03-28T00:00:00Z",
        "2024-03-28T00:00:00Z",
        "2024-03-29T00:00:00Z",
    ]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ccxt.AuthenticationError),
        (403, ccxt.AuthenticationError),
        (429, ccxt.RateLimitExceeded),
        (503, ccxt.NetworkError),
        (422, ccxt.ExchangeError),
    ],
)
async def test_status_codes_raise_ccxt_errors(status: int, error: type[Exception]) -> None:
    fake = _Alpaca(httpx.Response(status, json={"message": "no"}))
    with pytest.raises(error):
        await _client(fake).fetch_ohlcv("AAPL/USD", "1d", since=_ms("2024-01-02"))


async def test_an_unreachable_host_is_a_network_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = AlpacaClient("k", "s", transport=httpx.MockTransport(refuse))
    with pytest.raises(ccxt.NetworkError, match="ConnectError"):
        await client.fetch_balance()


async def test_only_daily_bars_and_usd_tickers_are_served() -> None:
    client = _client(_Alpaca())
    with pytest.raises(ccxt.NotSupported, match="1d bars only, not 1h"):
        await client.fetch_ohlcv("AAPL/USD", "1h")
    with pytest.raises(ccxt.BadSymbol, match="TICKER/USD"):
        await client.fetch_ohlcv("AAPLUSDT", "1d")


async def test_fetch_asset_reads_the_paper_api_and_requires_us_equity() -> None:
    equity = {"id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "class": "us_equity", "symbol": "AAPL"}
    fake = _Alpaca(
        httpx.Response(200, json=equity),
        httpx.Response(200, json={"id": "x", "class": "crypto", "symbol": "BTC/USD"}),
        httpx.Response(404, json={"message": "asset not found"}),
    )
    client = _client(fake)
    assert await client.fetch_asset("AAPL/USD") == equity
    with pytest.raises(ccxt.BadSymbol, match="'crypto', not us_equity"):
        await client.fetch_asset("BTC/USD")
    with pytest.raises(ccxt.BadSymbol):
        await client.fetch_asset("NOPE/USD")
    assert [(r.url.host, r.url.path) for r in fake.requests] == [
        ("paper-api.alpaca.markets", "/v2/assets/AAPL"),
        ("paper-api.alpaca.markets", "/v2/assets/BTC"),
        ("paper-api.alpaca.markets", "/v2/assets/NOPE"),
    ]


async def test_fetch_balance_is_the_paper_account_cash() -> None:
    fake = _Alpaca(httpx.Response(200, json={"cash": "100000.25", "status": "ACTIVE"}))
    assert await _client(fake).fetch_balance() == {"USD": Decimal("100000.25")}
    assert str(fake.requests[0].url) == "https://paper-api.alpaca.markets/v2/account"
