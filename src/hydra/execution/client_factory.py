"""Construction of session executors, exchange clients and market-data feed clients."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hydra.core.types import ExchangeId
from hydra.execution import session_store
from hydra.execution.paper_trading import PaperTradingExecutor

if TYPE_CHECKING:
    from hydra.execution.exchange_client import ExchangeClient

EXCHANGE_CLASS_MAP: dict[str, str] = {
    # Futures / derivatives class (used for live trading)
    "binance": "binanceusdm",
    "bybit": "bybit",
    "kraken": "krakenfutures",
    "okx": "okx",
    # coinbase and mexc do not offer a separate futures class via CCXT; fall
    # back to the spot class so the client can still be instantiated.
    "coinbase": "coinbase",
    "kucoin": "kucoinfutures",
    "gateio": "gateio",
    "mexc": "mexc",
    "bitget": "bitget",
}

SPOT_EXCHANGE_CLASS_MAP: dict[str, str] = {
    "binance": "binance",
    "bybit": "bybit",
    "kraken": "kraken",
    "okx": "okx",
    "coinbase": "coinbase",
    "kucoin": "kucoin",
    "gateio": "gateio",
    "mexc": "mexc",
    "bitget": "bitget",
}


def build_paper_executor(
    exchange_id: ExchangeId,
    initial_balances: dict[str, Decimal],
    db_pool: Any,
    strategy_id: str,
    session_id: str,
) -> PaperTradingExecutor:
    """Create the paper executor for one session."""
    return PaperTradingExecutor(
        exchange_id=exchange_id,
        initial_balances=initial_balances,
        db_pool=db_pool,
        strategy_id=strategy_id,
        session_id=session_id,
    )


def build_live_executor(exchange_id: ExchangeId, config: dict[str, Any]) -> ExchangeClient:
    """Create a live ExchangeClient (lazy import to avoid CCXT at startup)."""
    from hydra.execution.exchange_client import ExchangeClient

    return ExchangeClient(exchange_id=exchange_id, config=config)


async def load_live_credentials(db_pool: Any, exchange_id: str) -> dict[str, Any]:
    """Load and decrypt stored credentials into a CCXT config dict."""
    creds: dict[str, Any] = {}
    try:
        from hydra.core.encryption import decrypt

        cred_row = await session_store.fetch_exchange_credentials(db_pool, exchange_id)
        if cred_row:
            creds = {
                "apiKey": decrypt(cred_row["encrypted_key"]),
                "secret": decrypt(cred_row["encrypted_secret"]),
            }
            if cred_row.get("encrypted_passphrase"):
                creds["password"] = decrypt(cred_row["encrypted_passphrase"])
        else:
            raise ValueError(f"No credentials found for exchange {exchange_id}")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to load credentials for exchange {exchange_id}") from exc
    return creds


def feed_exchange_factory(exchange_id: str) -> Callable[[], Any]:
    """Return a factory for the ccxt.pro client used by a session's market-data feed."""

    def _ccxt_factory() -> Any:
        import ccxt.pro

        cls = getattr(ccxt.pro, exchange_id)
        return cls({"enableRateLimit": True})

    return _ccxt_factory
