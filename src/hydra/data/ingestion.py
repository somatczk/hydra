"""Multi-exchange WebSocket feed manager via CCXT Pro.

Manages concurrent WebSocket connections to multiple exchanges, normalizes
incoming data to ``hydra.core.types``, and publishes events via the event bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from hydra.core.events import BarEvent, TradeEvent
from hydra.core.logging import get_logger
from hydra.core.types import ExchangeId, Side, Symbol, Timeframe
from hydra.data.normalizer import DataNormalizer

logger = get_logger(__name__)

# Reconnect backoff parameters
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0
_BACKOFF_FACTOR = 2.0
_STABLE_THRESHOLD_S = 30.0  # sustained success before resetting backoff
_MAX_CONSECUTIVE_ERRORS = 5  # recreate exchange after this many consecutive failures


class ExchangeFeedManager:
    """Manage live WebSocket data streams from multiple exchanges.

    Uses CCXT Pro's ``watch_ohlcv()``, ``watch_trades()``, and
    ``watch_order_book()`` to stream data, normalizes it via
    ``DataNormalizer``, and publishes ``BarEvent`` / ``TradeEvent``
    instances through the provided event bus.

    Parameters
    ----------
    event_bus:
        An object satisfying the ``EventBus`` protocol (``publish()``
        method).
    normalizer:
        ``DataNormalizer`` instance for raw data conversion.
    exchange_factories:
        Mapping of exchange id to a callable returning a CCXT Pro exchange
        instance.
    """

    def __init__(
        self,
        event_bus: Any,
        normalizer: DataNormalizer | None = None,
        exchange_factories: dict[str, Any] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._normalizer = normalizer or DataNormalizer()
        self._exchange_factories = exchange_factories or {}
        self._exchanges: dict[str, Any] = {}
        self._tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._running: dict[ExchangeId, bool] = {}

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(
        self,
        exchange_id: ExchangeId,
        symbols: list[str],
        timeframes: list[Timeframe],
    ) -> None:
        """Start WebSocket streams for an exchange.

        Creates asyncio tasks for each symbol/timeframe combination to
        stream OHLCV bars and trades.

        Parameters
        ----------
        exchange_id:
            The exchange to connect to.
        symbols:
            Trading pair symbols to subscribe to.
        timeframes:
            Bar timeframes to stream.
        """
        if self._running.get(exchange_id, False):
            logger.warning("Already connected", exchange=exchange_id)
            return

        exchange = await self._get_or_create_exchange(exchange_id)
        self._running[exchange_id] = True
        self._tasks[exchange_id] = []

        # Start OHLCV streams
        for symbol in symbols:
            for timeframe in timeframes:
                task = asyncio.create_task(
                    self._watch_ohlcv_loop(exchange_id, exchange, symbol, timeframe),
                    name=f"ohlcv-{exchange_id}-{symbol}-{timeframe}",
                )
                self._tasks[exchange_id].append(task)

        # Start trade streams
        for symbol in symbols:
            task = asyncio.create_task(
                self._watch_trades_loop(exchange_id, exchange, symbol),
                name=f"trades-{exchange_id}-{symbol}",
            )
            self._tasks[exchange_id].append(task)

        logger.info(
            "Exchange feed started",
            exchange=exchange_id,
            symbols=symbols,
            timeframes=[str(tf) for tf in timeframes],
            tasks=len(self._tasks[exchange_id]),
        )

    async def disconnect(self, exchange_id: ExchangeId) -> None:
        """Gracefully close an exchange connection and cancel its tasks."""
        self._running[exchange_id] = False

        tasks = self._tasks.pop(exchange_id, [])
        for task in tasks:
            task.cancel()

        # Wait for tasks to finish
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Close the exchange connection
        exchange = self._exchanges.pop(exchange_id, None)
        if exchange is not None and hasattr(exchange, "close"):
            await exchange.close()

        logger.info("Exchange feed disconnected", exchange=exchange_id)

    async def disconnect_all(self) -> None:
        """Shutdown all exchange connections."""
        exchange_ids: list[ExchangeId] = list(self._running.keys())
        for eid in exchange_ids:
            await self.disconnect(eid)

    # ------------------------------------------------------------------
    # Internal: exchange lifecycle
    # ------------------------------------------------------------------

    async def _get_or_create_exchange(self, exchange_id: ExchangeId) -> Any:
        """Get or create a CCXT Pro exchange instance."""
        if exchange_id not in self._exchanges:
            factory = self._exchange_factories.get(exchange_id)
            if factory is None:
                msg = f"No exchange factory registered for {exchange_id}"
                raise ValueError(msg)
            self._exchanges[exchange_id] = factory()
        return self._exchanges[exchange_id]

    # ------------------------------------------------------------------
    # Internal: watch loops with auto-reconnect
    # ------------------------------------------------------------------

    async def _recreate_exchange(self, exchange_id: ExchangeId) -> Any:
        """Close and recreate a CCXT exchange instance to get fresh WebSocket state."""
        old = self._exchanges.pop(exchange_id, None)
        if old is not None and hasattr(old, "close"):
            with contextlib.suppress(Exception):
                await old.close()
        exchange = await self._get_or_create_exchange(exchange_id)
        logger.info("Recreated exchange instance", exchange=exchange_id)
        return exchange

    async def _watch_ohlcv_loop(
        self,
        exchange_id: ExchangeId,
        exchange: Any,
        symbol: str,
        timeframe: Timeframe,
    ) -> None:
        """Continuously watch OHLCV bars with exponential backoff reconnect."""
        backoff = _INITIAL_BACKOFF_S
        last_success = time.monotonic()
        consecutive_errors = 0

        while self._running.get(exchange_id, False):
            try:
                ohlcv_data = await exchange.watch_ohlcv(symbol, str(timeframe))

                # CCXT Pro returns list of [timestamp, o, h, l, c, v]
                for raw_bar in ohlcv_data:
                    ohlcv = self._normalizer.normalize_ohlcv(raw_bar, exchange_id)

                    event = BarEvent(
                        symbol=Symbol(symbol),
                        timeframe=timeframe,
                        ohlcv=ohlcv,
                        exchange_id=exchange_id,
                    )
                    await self._event_bus.publish(event)

                # Only reset backoff after sustained success
                consecutive_errors = 0
                if time.monotonic() - last_success >= _STABLE_THRESHOLD_S:
                    backoff = _INITIAL_BACKOFF_S
                last_success = time.monotonic()

                try:
                    from hydra.dashboard.metrics import update_data_gap

                    update_data_gap(exchange_id, symbol, 0.0)
                except Exception:
                    pass

            except asyncio.CancelledError:
                break
            except Exception:
                consecutive_errors += 1
                logger.warning(
                    "OHLCV watch error, reconnecting",
                    exchange=exchange_id,
                    symbol=symbol,
                    timeframe=str(timeframe),
                    backoff=backoff,
                    consecutive_errors=consecutive_errors,
                )
                try:
                    from hydra.dashboard.metrics import update_data_gap

                    update_data_gap(exchange_id, symbol, time.monotonic() - last_success)
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF_S)

                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    try:
                        from hydra.dashboard.metrics import record_ws_reconnect

                        record_ws_reconnect(exchange_id)
                    except Exception:
                        pass
                    exchange = await self._recreate_exchange(exchange_id)
                    consecutive_errors = 0

    async def _watch_trades_loop(
        self,
        exchange_id: ExchangeId,
        exchange: Any,
        symbol: str,
    ) -> None:
        """Continuously watch trades with exponential backoff reconnect."""
        backoff = _INITIAL_BACKOFF_S
        last_success = time.monotonic()
        consecutive_errors = 0

        while self._running.get(exchange_id, False):
            try:
                trades = await exchange.watch_trades(symbol)

                for trade in trades:
                    from decimal import Decimal

                    side_str = trade.get("side", "buy").upper()
                    side = Side.BUY if side_str == "BUY" else Side.SELL

                    event = TradeEvent(
                        symbol=Symbol(symbol),
                        price=Decimal(str(trade.get("price", 0))),
                        quantity=Decimal(str(trade.get("amount", 0))),
                        side=side,
                        exchange_id=exchange_id,
                        trade_id=str(trade.get("id", "")),
                    )
                    await self._event_bus.publish(event)

                # Only reset backoff after sustained success
                consecutive_errors = 0
                if time.monotonic() - last_success >= _STABLE_THRESHOLD_S:
                    backoff = _INITIAL_BACKOFF_S
                last_success = time.monotonic()

            except asyncio.CancelledError:
                break
            except Exception:
                consecutive_errors += 1
                logger.warning(
                    "Trade watch error, reconnecting",
                    exchange=exchange_id,
                    symbol=symbol,
                    backoff=backoff,
                    consecutive_errors=consecutive_errors,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF_S)

                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    try:
                        from hydra.dashboard.metrics import record_ws_reconnect

                        record_ws_reconnect(exchange_id)
                    except Exception:
                        pass
                    exchange = await self._recreate_exchange(exchange_id)
                    consecutive_errors = 0
