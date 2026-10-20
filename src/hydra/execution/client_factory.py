"""The only place that constructs exchange clients, executors and feed clients on the session path.

P1 accepts Binance SPOT only (D10); live executors are refused until P3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NoReturn, cast

from hydra.core.types import ExchangeId, MarketType
from hydra.data.storage import MarketDataRepository
from hydra.execution.errors import LiveTradingDisabledError, UnsupportedVenueError
from hydra.execution.paper_trading import PaperTradingExecutor, SpotMarketRules

if TYPE_CHECKING:
    from hydra.execution.exchange_client import ExchangeClient

ALLOWED_VENUES: frozenset[tuple[str, MarketType]] = frozenset({("binance", MarketType.SPOT)})

SPOT_EXCHANGE_CLASS_MAP: dict[str, str] = {"binance": "binance"}

SPOT_OPTIONS: dict[str, Any] = {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}


def _market_type(market_type: MarketType | str) -> MarketType:
    try:
        return MarketType(str(market_type).upper())
    except ValueError as exc:
        raise UnsupportedVenueError(f"unknown market type {market_type!r}") from exc


def validate_venue(exchange_id: str, market_type: MarketType | str = MarketType.SPOT) -> ExchangeId:
    """Return *exchange_id* typed; raise UnsupportedVenueError unless it is Binance SPOT."""
    mt = _market_type(market_type)
    if (exchange_id, mt) not in ALLOWED_VENUES:
        raise UnsupportedVenueError(
            f"{exchange_id} {mt.value} is not supported: only binance SPOT until P4"
        )
    return cast(ExchangeId, exchange_id)


def ccxt_class_for(exchange_id: str, market_type: MarketType | str = MarketType.SPOT) -> str:
    """The ccxt class name for an allowed venue (``binance`` SPOT -> ``binance``)."""
    validate_venue(exchange_id, market_type)
    return SPOT_EXCHANGE_CLASS_MAP[exchange_id]


def map_credentials(stored: Mapping[str, Any]) -> dict[str, str]:
    """Stored ``{api_key, api_secret, passphrase}`` -> ccxt ``{apiKey, secret, password}``."""
    key, secret = stored.get("api_key"), stored.get("api_secret")
    if not key or not secret:
        raise ValueError("stored credentials need api_key and api_secret")
    out = {"apiKey": str(key), "secret": str(secret)}
    if stored.get("passphrase"):
        out["password"] = str(stored["passphrase"])
    return out


def build_readonly_client(
    exchange_id: str, stored_creds: Mapping[str, Any], *, testnet: bool
) -> ExchangeClient:
    """Account-read client (balance, orders) on the spot class; the sandbox only if *testnet*."""
    from hydra.execution.exchange_client import ExchangeClient

    eid = validate_venue(exchange_id, MarketType.SPOT)
    return ExchangeClient(
        exchange_id=eid,
        config=map_credentials(stored_creds),
        testnet=testnet,
        market_type=MarketType.SPOT,
    )


def build_paper_executor(
    exchange_id: ExchangeId,
    initial_balances: dict[str, Decimal],
    db_pool: Any,
    strategy_id: str,
    session_id: str,
    market_rules: Mapping[str, SpotMarketRules] | None = None,
) -> PaperTradingExecutor:
    """Create the paper executor for one session, with the venue's lot and notional rules."""
    return PaperTradingExecutor(
        exchange_id=exchange_id,
        initial_balances=initial_balances,
        db_pool=db_pool,
        strategy_id=strategy_id,
        session_id=session_id,
        market_rules=market_rules,
    )


def build_live_executor(exchange_id: str, config: Mapping[str, Any] | None = None) -> NoReturn:
    """Live trading is disabled until P3; nothing is constructed."""
    raise LiveTradingDisabledError(
        f"live trading on {exchange_id} is disabled until P3 (paper only in P1)"
    )


def feed_exchange_factory(exchange_id: str) -> Callable[[], Any]:
    """Factory for the ccxt.pro spot client of a session's market-data feed."""
    cls_name = ccxt_class_for(exchange_id, MarketType.SPOT)

    def _ccxt_factory() -> Any:
        import ccxt.pro

        cls = getattr(ccxt.pro, cls_name)
        return cls({"enableRateLimit": True, "options": dict(SPOT_OPTIONS)})

    return _ccxt_factory


def rest_exchange_factory(exchange_id: str) -> Any:
    """Public ccxt async spot REST client for feed warm-up and gap fills (no keys)."""
    import ccxt.async_support as ccxt_async

    cls = getattr(ccxt_async, ccxt_class_for(exchange_id, MarketType.SPOT))
    return cls({"enableRateLimit": True, "options": dict(SPOT_OPTIONS)})


async def public_markets_loader(exchange_id: str) -> list[Mapping[str, Any]]:
    """Public spot ``load_markets`` for ``MarketRulesCache`` (no keys)."""
    client = rest_exchange_factory(exchange_id)
    try:
        markets = await client.load_markets()
    finally:
        await client.close()
    return list(markets.values())


def bars_repository(db_pool: Any) -> MarketDataRepository | None:
    """Where the feed persists closed bars and reads warm-up history (``ts.ohlcv_1m``)."""
    return MarketDataRepository.from_pool(db_pool) if db_pool is not None else None


MarketsLoader = Callable[[str], Awaitable[Iterable[Mapping[str, Any]]]]


@dataclass(frozen=True, slots=True)
class ClientFactory:
    """The exchange seams of a session: WebSocket feed, REST warm-up/gap fill, market rules.

    Production uses the public Binance spot clients and the ``ts.ohlcv_1m`` repository; tests and
    the paper smoke inject fakes.
    """

    ws_factory: Callable[[str], Callable[[], Any]] = feed_exchange_factory
    rest_factory: Callable[[str], Any] = rest_exchange_factory
    markets_loader: MarketsLoader = public_markets_loader
    repository_factory: Callable[[Any], MarketDataRepository | None] = bars_repository
    history_window: int = 1000
