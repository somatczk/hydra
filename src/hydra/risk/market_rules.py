"""Spot lot-size and min-notional rules from public ccxt market metadata (no API keys)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_EXCHANGES = frozenset({"binance"})

MarketsLoader = Callable[[str], Awaitable[Iterable[Mapping[str, Any]]]]


class UnsupportedExchangeError(ValueError):
    """Market rules exist only for the supported spot venues (Binance in P1)."""


class MarketRulesUnavailableError(RuntimeError):
    """Rules for a symbol could not be loaded; openings on it must not proceed."""


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class MarketRules:
    symbol: str
    amount_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_market_qty: Decimal | None = None
    price_tick: Decimal | None = None

    @classmethod
    def from_ccxt_market(cls, market: Mapping[str, Any]) -> MarketRules:
        """Build from a ccxt market dict in TICK_SIZE precision mode (precision.amount = step)."""
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        step = _dec(precision.get("amount"))
        if step is None or step <= 0:
            raise MarketRulesUnavailableError(
                f"{market.get('id')}: no amount step in market metadata"
            )
        return cls(
            symbol=str(market["id"]),
            amount_step=step,
            min_qty=_dec((limits.get("amount") or {}).get("min")) or step,
            min_notional=_dec((limits.get("cost") or {}).get("min")) or Decimal("0"),
            max_market_qty=_dec((limits.get("market") or {}).get("max")),
            price_tick=_dec(precision.get("price")),
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


async def binance_spot_markets(exchange_id: str) -> list[Mapping[str, Any]]:
    """Public Binance spot load_markets (no keys); the default loader until P1-E injects its own."""
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise UnsupportedExchangeError(f"no market rules for exchange {exchange_id!r}")
    import ccxt.async_support as ccxt_async

    client = ccxt_async.binance({"enableRateLimit": True})
    try:
        markets = await client.load_markets()
    finally:
        await client.close()
    return list(markets.values())


def _now() -> datetime:
    return datetime.now(UTC)


class MarketRulesCache:
    """Per-process cache of market rules, refreshed after ``ttl``."""

    def __init__(
        self,
        loader: MarketsLoader = binance_spot_markets,
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
                if market.get("spot", True) and market.get("id"):
                    by_id[str(market["id"])] = MarketRules.from_ccxt_market(market)
            cached = (now, by_id)
            self._cache[exchange_id] = cached
        rules: dict[str, MarketRules] = {}
        for symbol in symbols:
            found = cached[1].get(symbol.replace("/", ""))
            if found is None:
                raise MarketRulesUnavailableError(f"{exchange_id} has no spot market {symbol!r}")
            rules[symbol] = found
        return rules


async def load_market_rules(
    exchange_id: str,
    symbols: Iterable[str],
    loader: MarketsLoader = binance_spot_markets,
) -> dict[str, MarketRules]:
    """Uncached load of rules for *symbols* (raises for non-Binance and unknown symbols)."""
    return await MarketRulesCache(loader=loader).get(exchange_id, symbols)
