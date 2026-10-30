"""Lot-size, min-notional and trailing bounds from public ccxt market metadata (no API keys).

Spot rules come from Binance spot markets (binance) and bybit spot markets (bybit, P7-F);
USD⌂-M perp rules from binanceusdm's linear swap markets — whose trailing stops are the
fapi TRAILING_STOP_MARKET ``callbackRate`` PERCENT bounds, not spot's per-symbol
TRAILING_DELTA bips. The trailing form per venue comes from the registry
(``hydra.core.venues``): bybit has no percent trail, so its rules carry no trailing bounds
and the protective ladder falls back to the fixed stop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from hydra.core.types import MarketType
from hydra.core.venues import (
    TrailingKind,
    find_venue,
    registered_venue_ids,
    venue_market_type,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXCHANGES = registered_venue_ids()
"""The registered venue ids — market rules exist only for them (binance, binanceusdm, bybit)."""

MarketsLoader = Callable[[str], Awaitable[Iterable[Mapping[str, Any]]]]

USDM_CALLBACK_RATE_PCT: tuple[Decimal, Decimal] = (Decimal("0.1"), Decimal("10"))
"""The fapi TRAILING_STOP_MARKET callbackRate bounds in percent (0.1 to 10): the order
API's fixed range — futures exchangeInfo carries no trailing filter, unlike spot's
per-symbol TRAILING_DELTA."""


class UnsupportedExchangeError(ValueError):
    """Market rules exist only for the supported venues (Binance spot, Binance USD⌂-M)."""


class MarketRulesUnavailableError(RuntimeError):
    """Rules for a symbol could not be loaded; openings on it must not proceed."""


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _trailing_delta_bips(market: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    """The SELL-stop trailing bounds (BIPS) from the raw TRAILING_DELTA filter.

    A SELL STOP_LOSS trailing order is bounded by the ``minTrailingBelowDelta`` /
    ``maxTrailingBelowDelta`` pair; ccxt has no unified limits field for it, so it is read
    from ``info.filters``. None when the symbol has no filter.
    """
    filters = (market.get("info") or {}).get("filters") or []
    for item in filters:
        if not isinstance(item, Mapping) or item.get("filterType") != "TRAILING_DELTA":
            continue
        low = _dec(item.get("minTrailingBelowDelta"))
        high = _dec(item.get("maxTrailingBelowDelta"))
        if low is not None and high is not None:
            return (low, high)
    return None


@dataclass(frozen=True, slots=True)
class MarketRules:
    symbol: str
    amount_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_market_qty: Decimal | None = None
    price_tick: Decimal | None = None
    trailing_delta_bips: tuple[Decimal, Decimal] | None = None
    trailing_callback_rate_pct: tuple[Decimal, Decimal] | None = None

    @classmethod
    def from_ccxt_market(
        cls, market: Mapping[str, Any], exchange_id: str = "binance"
    ) -> MarketRules:
        """Build from a ccxt market dict in TICK_SIZE precision mode (precision.amount = step).

        The trailing bounds come from the venue's registry trailing kind: binance spot
        keeps the TRAILING_DELTA bips; the USD⌂-M perp venue gets the fapi callbackRate
        percent bounds instead (the spot filter is never mapped onto it); bybit has no
        percent trail at all.
        """
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        step = _dec(precision.get("amount"))
        if step is None or step <= 0:
            raise MarketRulesUnavailableError(
                f"{market.get('id')}: no amount step in market metadata"
            )
        spec = find_venue(exchange_id, venue_market_type(exchange_id))
        trailing_bips = None
        trailing_callback = None
        if spec is not None:
            if spec.trailing_kind is TrailingKind.BIPS_FILTER:
                trailing_bips = _trailing_delta_bips(market)
            elif spec.trailing_kind is TrailingKind.CALLBACK_RATE_PCT:
                trailing_callback = USDM_CALLBACK_RATE_PCT
        return cls(
            symbol=str(market["id"]),
            amount_step=step,
            min_qty=_dec((limits.get("amount") or {}).get("min")) or step,
            min_notional=_dec((limits.get("cost") or {}).get("min")) or Decimal("0"),
            max_market_qty=_dec((limits.get("market") or {}).get("max")),
            price_tick=_dec(precision.get("price")),
            trailing_delta_bips=trailing_bips,
            trailing_callback_rate_pct=trailing_callback,
        )

    def floor_qty(self, qty: Decimal) -> Decimal:
        """Round down to a whole number of lot steps; never rounds up."""
        if qty <= 0:
            return Decimal("0")
        steps = (qty / self.amount_step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.amount_step).quantize(self.amount_step)

    def validate_open(self, qty: Decimal, price: Decimal) -> str | None:
        """Reason the exchange would reject this quantity at *price*, or None."""
        if qty <= 0:
            return f"{self.symbol}: non-positive quantity {qty}"
        if qty % self.amount_step != 0:
            return f"{self.symbol}: quantity {qty} is not a multiple of lot step {self.amount_step}"
        if qty < self.min_qty:
            return f"{self.symbol}: quantity {qty} below minimum {self.min_qty}"
        if (
            self.max_market_qty is not None
            and self.max_market_qty > 0
            and qty > self.max_market_qty
        ):
            return f"{self.symbol}: quantity {qty} above market maximum {self.max_market_qty}"
        notional = qty * price
        if notional < self.min_notional:
            return f"{self.symbol}: notional {notional} below min notional {self.min_notional}"
        return None


async def _load_public_markets(class_name: str, options: dict[str, Any]) -> list[Mapping[str, Any]]:
    """One public ``load_markets`` on the venue's ccxt class and options, closed after."""
    import ccxt.async_support as ccxt_async

    client = getattr(ccxt_async, class_name)({"enableRateLimit": True, "options": dict(options)})
    try:
        markets = await client.load_markets()
    finally:
        await client.close()
    return list(markets.values())


async def public_venue_markets(exchange_id: str) -> list[Mapping[str, Any]]:
    """One public ``load_markets`` on the registry's ccxt class and options (no keys).

    binanceusdm keeps its linear filter through the registry options
    (``fetchMarkets types ['linear']``); bybit's options pin the spot category.
    """
    spec = find_venue(exchange_id, venue_market_type(exchange_id))
    if spec is None:
        raise UnsupportedExchangeError(f"no market rules for exchange {exchange_id!r}")
    return await _load_public_markets(spec.ccxt_class, dict(spec.client_options))


_DEFAULT_MARKETS_LOADERS: Mapping[str, MarketsLoader] = {
    "binance": public_venue_markets,
    "binanceusdm": public_venue_markets,
    "bybit": public_venue_markets,
}


async def load_public_markets(exchange_id: str) -> list[Mapping[str, Any]]:
    """The per-exchange public loader: Binance spot, USD⌂-M linear perps on binanceusdm,
    bybit spot — every registered venue, through the registry's ccxt class."""
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise UnsupportedExchangeError(f"no market rules for exchange {exchange_id!r}")
    return list(await _DEFAULT_MARKETS_LOADERS[exchange_id](exchange_id))


def _is_rules_market(exchange_id: str, market: Mapping[str, Any]) -> bool:
    """Spot markets on the spot venues; linear swap markets on binanceusdm (the perp rules)."""
    if venue_market_type(exchange_id) is MarketType.FUTURES:
        return bool(market.get("linear")) and bool(market.get("swap"))
    return market.get("spot", True)


def _now() -> datetime:
    return datetime.now(UTC)


class MarketRulesCache:
    """Per-process cache of market rules, refreshed after ``ttl``."""

    def __init__(
        self,
        loader: MarketsLoader = load_public_markets,
        ttl: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[datetime, dict[str, MarketRules]]] = {}

    async def get(self, exchange_id: str, symbols: Iterable[str]) -> dict[str, MarketRules]:
        if exchange_id not in SUPPORTED_EXCHANGES:
            raise UnsupportedExchangeError(f"no market rules for exchange {exchange_id!r}")
        now = self._clock()
        cached = self._cache.get(exchange_id)
        if cached is None or now - cached[0] >= self._ttl:
            try:
                markets = await self._loader(exchange_id)
            except UnsupportedExchangeError:
                raise
            except Exception as exc:
                raise MarketRulesUnavailableError(
                    f"loading {exchange_id} market rules failed: {exc}"
                ) from exc
            by_id: dict[str, MarketRules] = {}
            for market in markets:
                if market.get("id") and _is_rules_market(exchange_id, market):
                    by_id[str(market["id"])] = MarketRules.from_ccxt_market(market, exchange_id)
            cached = (now, by_id)
            self._cache[exchange_id] = cached
        rules: dict[str, MarketRules] = {}
        for symbol in symbols:
            found = cached[1].get(symbol.replace("/", ""))
            if found is None:
                raise MarketRulesUnavailableError(
                    f"{exchange_id} has no {self._market_noun(exchange_id)} market {symbol!r}"
                )
            rules[symbol] = found
        return rules

    @staticmethod
    def _market_noun(exchange_id: str) -> str:
        return "linear" if venue_market_type(exchange_id) is MarketType.FUTURES else "spot"


async def load_market_rules(
    exchange_id: str,
    symbols: Iterable[str],
    loader: MarketsLoader = load_public_markets,
) -> dict[str, MarketRules]:
    """Uncached load of rules for *symbols* (raises for unsupported venues and unknown symbols)."""
    return await MarketRulesCache(loader=loader).get(exchange_id, symbols)
