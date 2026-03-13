"""Unified exchange client wrapping CCXT Pro for Binance/Bybit/Kraken/OKX.

The ``ExchangeClient`` is exchange-agnostic: all exchange-specific behavior is
handled internally by CCXT.  ``ccxt`` is **not** imported at module level so that
test environments without the package installed can still import this module.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from hydra.core.types import ExchangeId

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange configuration defaults
# ---------------------------------------------------------------------------

_EXCHANGE_CLASS_MAP: dict[str, str] = {
    "binance": "binanceusdm",
    "bybit": "bybit",
    "kraken": "krakenfutures",
    "okx": "okx",
}

_SPOT_EXCHANGE_CLASS_MAP: dict[str, str] = {
    "binance": "binance",
    "bybit": "bybit",
    "kraken": "kraken",
    "okx": "okx",
}


# ---------------------------------------------------------------------------
# ExchangeClient
# ---------------------------------------------------------------------------


class ExchangeClient:
    """Unified async wrapper around a CCXT Pro exchange instance.

    Parameters
    ----------
    exchange_id:
        One of the supported exchange identifiers.
    config:
        CCXT-compatible configuration dict (apiKey, secret, etc.).
    testnet:
        Whether to connect to the testnet/sandbox endpoint.
    """

    def __init__(
        self,
        exchange_id: ExchangeId,
        config: dict[str, Any],
        testnet: bool = True,
    ) -> None:
        self._exchange_id: ExchangeId = exchange_id
        self._config = dict(config)
        self._testnet = testnet
        self._exchange: Any | None = None
        self._ws_connected: bool = False

    # ------------------------------------------------------------------
    # Lazy CCXT initialization
    # ------------------------------------------------------------------

    def _get_exchange(self) -> Any:
        """Lazily create the CCXT exchange instance."""
        if self._exchange is not None:
            return self._exchange

        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:
            raise ImportError(
                "ccxt is required for live exchange connectivity. "
                "Install with: pip install 'ccxt[async]'"
            ) from exc

        class_name = _EXCHANGE_CLASS_MAP.get(self._exchange_id, self._exchange_id)
        exchange_cls = getattr(ccxt_async, class_name, None)
        if exchange_cls is None:
            raise ValueError(f"Unknown exchange class: {class_name}")

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            **self._config,
        }
        if self._testnet:
            opts["sandbox"] = True

        self._exchange = exchange_cls(opts)
        return self._exchange

    # ------------------------------------------------------------------
    # Order API
    # ------------------------------------------------------------------

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place an order on the exchange.  Returns the CCXT order dict."""
        exchange = self._get_exchange()
        extra: dict[str, Any] = dict(params or {})

        if stop_price is not None:
            extra["stopPrice"] = float(stop_price)

        price_f: float | None = float(price) if price is not None else None
        result: dict[str, Any] = await exchange.create_order(
            symbol=symbol,
            type=order_type.lower(),
            side=side.lower(),
            amount=float(quantity),
            price=price_f,
            params=extra,
        )
        return result

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel an order on the exchange."""
        exchange = self._get_exchange()
        result: dict[str, Any] = await exchange.cancel_order(order_id, symbol)
        return result

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> dict[str, Decimal]:
        """Fetch account balances.  Returns {currency: free_balance}."""
        exchange = self._get_exchange()
        raw: dict[str, Any] = await exchange.fetch_balance()
        balances: dict[str, Decimal] = {}
        free: dict[str, Any] = raw.get("free", {})
        for currency, amount in free.items():
            if amount is not None and float(amount) > 0:
                balances[currency] = Decimal(str(amount))
        return balances

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch open orders from the exchange."""
        exchange = self._get_exchange()
        result: list[dict[str, Any]] = await exchange.fetch_open_orders(symbol)
        return result

    async def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch open positions (futures only)."""
        exchange = self._get_exchange()
        if symbol:
            result: list[dict[str, Any]] = await exchange.fetch_positions([symbol])
        else:
            result = await exchange.fetch_positions()
        return result

    # ------------------------------------------------------------------
    # Futures-specific
    # ------------------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a futures symbol."""
        exchange = self._get_exchange()
        await exchange.set_leverage(leverage, symbol)

    async def set_margin_mode(self, symbol: str, mode: str) -> None:
        """Set margin mode ('cross' or 'isolated') for a futures symbol."""
        exchange = self._get_exchange()
        await exchange.set_margin_mode(mode, symbol)

    # ------------------------------------------------------------------
    # WebSocket reconnection helper
    # ------------------------------------------------------------------

    async def start_user_data_stream(self) -> None:
        """Start the WebSocket user-data stream with auto-reconnect logic."""
        exchange = self._get_exchange()
        self._ws_connected = True
        max_retries = 5
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                if hasattr(exchange, "watch_orders"):
                    await exchange.watch_orders()
                break
            except Exception:
                logger.warning(
                    "WebSocket reconnect attempt %d/%d",
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the exchange connection."""
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            self._ws_connected = False
