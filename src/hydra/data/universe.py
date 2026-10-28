"""The top-100 trading universe: Binance SPOT USDT pairs by rolling-24h quote volume.

``rank_universe`` is the pure ranking: eligible markets are spot, active, USDT-quoted,
tradable bases (no stablecoin or fiat base, none of the exact historic leveraged-token
bases) with positive rolling-24h quote volume; sorted by volume descending with the
exchange symbol ascending as the deterministic tie-break, capped at 100 (fewer only
when the exchange offers fewer). ``compute_universe`` makes the two public REST calls —
one ``load_markets`` and one whole-exchange ``fetch_tickers`` (weight 80;
``GET /api/v3/ticker/24hr``) — on the pooled client it is given, never a bespoke one.
``UniverseRefresher`` is the background loop around an injected refresh callable: the
first refresh runs immediately, then every 24 h; a failure logs a warning and retries
hourly, so it never raises into the lifespan and is not a readiness check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

EXCHANGE_ID = "binance"
QUOTE_ASSET = "USDT"
SOURCE = "binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)"
UNIVERSE_SIZE = 100

REFRESH_INTERVAL_S = 24.0 * 60 * 60
RETRY_INTERVAL_S = 60.0 * 60

type Refresh = Callable[[], Awaitable[None]]
type Sleep = Callable[[float], Awaitable[None]]

# Real top-100 volume, but not tradable pairs: stablecoin bases and fiat off-ramps.
_NON_TRADING_BASES = frozenset(
    {
        "USDC",
        "TUSD",
        "FDUSD",
        "DAI",
        "USD1",
        "USDP",
        "BUSD",
        "USDE",
        "USDS",
        "USDG",
        "AEUR",
        "EURI",
        "EUR",
        "TRY",
        "BRL",
        "ARS",
        "GBP",
        "AUD",
        "JPY",
        "RUB",
        "UAH",
        "ZAR",
        "MXN",
        "PLN",
        "RON",
        "CZK",
        "INR",
        "IDR",
        "NGN",
        "COP",
        "CAD",
        "CHF",
    }
)
# The exact historic Binance leveraged-token bases (all delisted 2022-2024); no suffix heuristic.
_LEVERAGED_BASES = frozenset(
    {
        "BTCUP",
        "BTCDOWN",
        "ETHUP",
        "ETHDOWN",
        "BNBUP",
        "BNBDOWN",
        "XRPUP",
        "XRPDOWN",
        "ADAUP",
        "ADADOWN",
        "TRXUP",
        "TRXDOWN",
        "LTCUP",
        "LTCDOWN",
        "BCHUP",
        "BCHDOWN",
        "DOTUP",
        "DOTDOWN",
        "LINKUP",
        "LINKDOWN",
        "XTZUP",
        "XTZDOWN",
        "ATOMUP",
        "ATOMDOWN",
        "EOSUP",
        "EOSDOWN",
        "UNIUP",
        "UNIDOWN",
        "SXPUP",
        "SXPDOWN",
        "FILUP",
        "FILDOWN",
        "AAVEUP",
        "AAVEDOWN",
        "SUSHIUP",
        "SUSHIDOWN",
        "XLMUP",
        "XLMDOWN",
        "1INCHUP",
        "1INCHDOWN",
        "YFIUP",
        "YFIDOWN",
        "BANDUP",
        "BANDDOWN",
        "DOGEUP",
        "DOGEDOWN",
        "SOLUP",
        "SOLDOWN",
    }
)


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One ranked pair; ``symbol`` is the exchange market id without the slash."""

    rank: int
    symbol: str
    quote_volume: float


def rank_universe(
    markets: Iterable[Mapping[str, Any]], tickers: Iterable[Mapping[str, Any]]
) -> list[UniverseEntry]:
    """Rank the exchange's USDT spot markets by rolling-24h quote volume; the top 100.

    Market metadata decides eligibility (spot, active, quote asset, tradable base); the
    ticker of the same unified symbol supplies the volume. The tie-break on the exchange
    symbol keeps the ranking deterministic; ``count`` is fewer than 100 only when the
    exchange offers fewer eligible markets.
    """
    ticker_by_symbol = {str(t.get("symbol")): t for t in tickers if t.get("symbol")}
    eligible: list[tuple[float, str]] = []
    for market in markets:
        if not market.get("spot", False) or not market.get("active", False):
            continue
        if market.get("quote") != QUOTE_ASSET:
            continue
        base = str(market.get("base") or "").upper()
        symbol = market.get("symbol")
        if not base or not symbol or base in _NON_TRADING_BASES:
            continue
        if base in _LEVERAGED_BASES:
            continue
        volume = ticker_by_symbol.get(str(symbol), {}).get("quoteVolume")
        if not isinstance(volume, int | float) or not math.isfinite(volume) or volume <= 0:
            continue
        eligible.append((float(volume), str(symbol)))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    return [
        UniverseEntry(rank=index + 1, symbol=symbol.replace("/", ""), quote_volume=volume)
        for index, (volume, symbol) in enumerate(eligible[:UNIVERSE_SIZE])
    ]


async def compute_universe(rest_client: Any) -> list[UniverseEntry]:
    """One ``load_markets`` plus one whole-exchange ``fetch_tickers`` on *rest_client*."""
    markets = await rest_client.load_markets()
    tickers = await rest_client.fetch_tickers()
    return rank_universe(markets.values(), tickers.values())


class UniverseRefresher:
    """Background refresh loop: immediate first run, then every ``interval_s``.

    A failed refresh is logged as a warning and retried after ``retry_s`` (an hour by
    default); the loop itself never raises into the caller that started it. ``sleep`` is
    injectable so tests run the schedule without waiting on wall time.
    """

    def __init__(
        self,
        refresh: Refresh,
        *,
        interval_s: float = REFRESH_INTERVAL_S,
        retry_s: float = RETRY_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._refresh = refresh
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Schedule the loop; the first refresh runs immediately, in the background."""
        if self._task is not None:
            raise RuntimeError("UniverseRefresher.start() called twice")
        self._task = asyncio.create_task(self._run(), name="universe-refresher")

    async def stop(self) -> None:
        """Cancel the loop and wait for it; safe to call twice or before ``start``."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Universe refresh failed; retrying in %.0f s", self._retry_s, exc_info=True
                )
                await self._sleep(self._retry_s)
                continue
            logger.info("Universe refresh completed")
            await self._sleep(self._interval_s)
