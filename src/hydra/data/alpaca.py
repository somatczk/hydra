"""Alpaca's historical daily bars and paper account, ccxt-shaped (S1).

``AlpacaClient`` answers the slice of the ccxt surface the fetch engine and the Settings key
check call — ``fetch_ohlcv``, ``fetch_balance``, a truthy ``markets``, ``close`` — and raises
ccxt's error classes, so ``backfill``'s retries and the connect route's error mapping work
unchanged. Bars are 1Day, split-adjusted, from the SIP feed, ascending, with ``end`` held 16
minutes behind now (the free plan serves SIP only past its 15-minute delay). Each bar is
labelled 00:00 UTC of its New York session date. Plain httpx, one connection per request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import ccxt
import httpx

DATA_URL = "https://data.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
SIP_DELAY = timedelta(minutes=16)
TIMEOUT_S = 10.0
MAX_LIMIT = 10_000
_NEW_YORK = ZoneInfo("America/New_York")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def session_label_ms(stamp: str) -> int:
    """Alpaca's bar time -> 00:00 UTC of its New York session date, in ms."""
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    day = when.astimezone(_NEW_YORK).date()
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def ticker(symbol: str) -> str:
    """``AAPL/USD`` -> ``AAPL``; anything not USD-quoted is ccxt's ``BadSymbol``."""
    base, sep, quote_asset = symbol.partition("/")
    if not base or not sep or quote_asset != "USD":
        raise ccxt.BadSymbol(f"alpaca symbols are TICKER/USD, got {symbol!r}")
    return base


class AlpacaClient:
    """The Alpaca paper key's data and account reads (no orders)."""

    markets: Mapping[str, Any] = {"us_equity": True}
    """Truthy, so the fetch engine never calls ``load_markets``: Alpaca has no market list."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
        self._transport = transport
        self._clock = clock

    async def _get(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, headers=self._headers, timeout=TIMEOUT_S
            ) as http:
                response = await http.get(url, params=params)
        except httpx.TransportError as exc:
            raise ccxt.NetworkError(f"alpaca unreachable: {type(exc).__name__}") from exc
        status = response.status_code
        if status == 429:
            raise ccxt.RateLimitExceeded("alpaca rate limit (429)")
        if status in (401, 403):
            raise ccxt.AuthenticationError(f"alpaca rejected the key ({status})")
        if status >= 500:
            raise ccxt.NetworkError(f"alpaca server error ({status})")
        if status == 404:
            raise ccxt.BadSymbol(f"alpaca has no such resource: {url}")
        if status != 200:
            raise ccxt.ExchangeError(f"alpaca answered {status}: {response.text[:200]}")
        return response.json()

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        since: int | None = None,
        limit: int | None = None,
    ) -> list[list[Any]]:
        """Daily bars from ``since`` (ms), oldest first, as ``[ms, o, h, l, c, v]`` rows."""
        if timeframe != "1d":
            raise ccxt.NotSupported(f"alpaca serves 1d bars only, not {timeframe}")
        params: dict[str, Any] = {
            "timeframe": "1Day",
            "adjustment": "split",
            "feed": "sip",
            "sort": "asc",
            "end": _iso(self._clock() - SIP_DELAY),
            "limit": min(limit or MAX_LIMIT, MAX_LIMIT),
        }
        if since is not None:
            params["start"] = _iso(datetime.fromtimestamp(since / 1000, UTC))
        url = f"{DATA_URL}/v2/stocks/{quote(ticker(symbol), safe='')}/bars"
        body = await self._get(url, params)
        return [
            [session_label_ms(bar["t"]), bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]]
            for bar in body.get("bars") or []
        ]

    async def fetch_asset(self, symbol: str) -> dict[str, Any]:
        """The ticker's asset record; ``BadSymbol`` unless it is a ``us_equity``."""
        name = ticker(symbol)
        asset: dict[str, Any] = await self._get(f"{PAPER_URL}/v2/assets/{quote(name, safe='')}")
        if asset.get("class") != "us_equity":
            raise ccxt.BadSymbol(f"alpaca asset {name} is {asset.get('class')!r}, not us_equity")
        return asset

    async def fetch_balance(self) -> dict[str, Decimal]:
        """The paper account's cash in USD (the Settings key check's read)."""
        account = await self._get(f"{PAPER_URL}/v2/account")
        return {"USD": Decimal(str(account["cash"]))}

    async def load_markets(self) -> Mapping[str, Any]:
        return self.markets

    async def close(self) -> None:
        """Nothing to close: every request opens and closes its own connection."""
