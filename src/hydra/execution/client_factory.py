"""The only place that constructs exchange clients, executors and feed clients on the session path.

P3-A opens the live path behind the service-layer armed gate: ``build_trading_client`` builds
the keyed client from stored credentials and ``build_live_executor`` the live session executor
(Binance SPOT, D10). P4-B adds the second venue: Binance USD⌂-M linear perps as
``binanceusdm`` FUTURES — public market data, market rules and funding rates. P4-D extends
``build_live_executor`` to FUTURES: behind the perps gate (checked at every live surface) it
builds the live perp executor — the perp margin sleeve at the strategy's 1-3x isolated
leverage — instead of refusing the venue. P7-F adds bybit SPOT, LIVE ONLY: the registry
(``hydra.core.venues``) owns every venue fact, and ``validate_venue`` gains the mode caps —
paper and backtest still run on Binance alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from hydra.core.logging import get_logger
from hydra.core.types import ExchangeId, MarketType
from hydra.core.venues import (
    VENUES,
    SessionMode,
    find_venue,
    registered_venues,
)
from hydra.core.venues import venue_market_type as _registry_market_type
from hydra.data.storage import MarketDataRepository
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.paper_trading import PaperTradingExecutor, SpotMarketRules
from hydra.execution.perp_trading import default_funding_reader

if TYPE_CHECKING:
    from hydra.execution.exchange_client import ExchangeClient
    from hydra.execution.perp_trading import FundingReader, PerpPaperTradingExecutor

logger = get_logger(__name__)

ALLOWED_VENUES: frozenset[tuple[str, MarketType]] = registered_venues()
"""The registered venue pairs — binance SPOT, binanceusdm FUTURES and bybit SPOT (P7-F)."""

SPOT_EXCHANGE_CLASS_MAP: dict[str, str] = {
    spec.exchange_id: spec.ccxt_class
    for (_eid, market_type), spec in VENUES.items()
    if market_type is MarketType.SPOT
}

USDM_EXCHANGE_CLASS_MAP: dict[str, str] = {
    spec.exchange_id: spec.ccxt_class
    for (_eid, market_type), spec in VENUES.items()
    if market_type is MarketType.FUTURES
}

_EXCHANGE_CLASS_MAPS: dict[MarketType, dict[str, str]] = {
    MarketType.SPOT: SPOT_EXCHANGE_CLASS_MAP,
    MarketType.FUTURES: USDM_EXCHANGE_CLASS_MAP,
}


def _market_type(market_type: MarketType | str) -> MarketType:
    try:
        return MarketType(str(market_type).upper())
    except ValueError as exc:
        raise UnsupportedVenueError(f"unknown market type {market_type!r}") from exc


def validate_venue(
    exchange_id: str,
    market_type: MarketType | str = MarketType.SPOT,
    *,
    mode: SessionMode | None = "paper",
) -> ExchangeId:
    """Return *exchange_id* typed; raise UnsupportedVenueError unless the pair is allowed
    for *mode* (the registry's per-venue mode caps).

    The default keeps the pre-P7-F semantics — paper and backtest refuse the live-only
    venue, exactly as they refused every unregistered one — so the runners and the paper
    builders pass nothing; only the live builders and the live recovery pass ``live``.
    ``mode=None`` is the pair-only check, for client construction whose session gate
    already ran (the keyed live clients) or that carries no mode (the public feeds).
    """
    mt = _market_type(market_type)
    spec = find_venue(exchange_id, mt)
    if spec is None:
        raise UnsupportedVenueError(
            f"{exchange_id} {mt.value} is not supported and not scheduled: "
            "the supported venues are binance SPOT, binanceusdm FUTURES "
            "(USD⌂-M perps) and bybit SPOT (live only)"
        )
    if mode is not None and mode not in spec.allowed_modes:
        raise UnsupportedVenueError(
            f"{exchange_id} {mt.value} is a live-only venue: paper and backtest sessions "
            f"run on binance and binanceusdm (asked for {mode})"
        )
    return cast(ExchangeId, exchange_id)


def ccxt_class_for(exchange_id: str, market_type: MarketType | str = MarketType.SPOT) -> str:
    """The ccxt class for a registered venue (binance SPOT -> ``binance``, binanceusdm
    FUTURES -> ``binanceusdm``, bybit SPOT -> ``bybit``); the pair-only check — the
    caller's session gate owns the mode."""
    mt = _market_type(market_type)
    validate_venue(exchange_id, mt, mode=None)
    return _EXCHANGE_CLASS_MAPS[mt][exchange_id]


def _venue_market_type(exchange_id: str) -> MarketType:
    """The market type an exchange id trades: binanceusdm is the USD⌂-M perps venue."""
    return _registry_market_type(exchange_id)


def venue_market_type(exchange_id: str) -> MarketType:
    """Public form of ``_venue_market_type`` (recovery validates a row's own venue pair)."""
    return _venue_market_type(exchange_id)


def _venue_options(exchange_id: str, market_type: MarketType) -> dict[str, Any]:
    spec = find_venue(exchange_id, market_type)
    if spec is None:
        validate_venue(exchange_id, market_type, mode=None)
        raise UnsupportedVenueError(f"{exchange_id} {market_type.value} has no venue spec")
    return dict(spec.client_options)


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
    exchange_id: str,
    stored_creds: Mapping[str, Any],
    *,
    testnet: bool,
    market_type: MarketType | str = MarketType.SPOT,
) -> ExchangeClient:
    """Account-read client (balance, orders, the key-permission re-fetch); the sandbox
    only if *testnet*. The keyed ceremony path is live-mode: the mode caps admit bybit."""
    from hydra.execution.exchange_client import ExchangeClient

    mt = _market_type(market_type)
    eid = validate_venue(exchange_id, mt, mode="live")
    return ExchangeClient(
        exchange_id=eid,
        config=map_credentials(stored_creds),
        testnet=testnet,
        market_type=mt,
    )


def build_trading_client(
    exchange_id: str,
    stored_creds: Mapping[str, Any],
    *,
    testnet: bool,
    market_type: MarketType | str = MarketType.SPOT,
) -> ExchangeClient:
    """The keyed live client (orders, trades, balance); same class the read-only path uses.

    The keyed client exists only to trade live, so the mode caps admit the live-only venue."""
    from hydra.execution.exchange_client import ExchangeClient

    mt = _market_type(market_type)
    eid = validate_venue(exchange_id, mt, mode="live")
    return ExchangeClient(
        exchange_id=eid,
        config=map_credentials(stored_creds),
        testnet=testnet,
        market_type=mt,
    )


def build_paper_executor(
    exchange_id: ExchangeId,
    initial_balances: dict[str, Decimal],
    db_pool: Any,
    strategy_id: str,
    session_id: str,
    market_rules: Mapping[str, SpotMarketRules] | None = None,
) -> PaperTradingExecutor:
    """Create the spot paper executor for one session (the SPOT pair only, A1's hard floor)."""
    validate_venue(exchange_id, MarketType.SPOT)
    return PaperTradingExecutor(
        exchange_id=exchange_id,
        initial_balances=initial_balances,
        db_pool=db_pool,
        strategy_id=strategy_id,
        session_id=session_id,
        market_rules=market_rules,
    )


def build_perp_paper_executor(
    exchange_id: ExchangeId,
    initial_balances: dict[str, Decimal],
    db_pool: Any,
    strategy_id: str,
    session_id: str,
    market_rules: Mapping[str, SpotMarketRules] | None = None,
    *,
    leverage: int = 1,
    funding_reader: FundingReader | None = None,
) -> PerpPaperTradingExecutor:
    """Create the perp paper executor for one session (binanceusdm FUTURES only, P4-C)."""
    from hydra.execution.perp_trading import PerpPaperTradingExecutor

    validate_venue(exchange_id, MarketType.FUTURES)
    return PerpPaperTradingExecutor(
        exchange_id=exchange_id,
        initial_balances=initial_balances,
        db_pool=db_pool,
        strategy_id=strategy_id,
        session_id=session_id,
        market_rules=market_rules,
        leverage=leverage,
        funding_reader=funding_reader,
    )


def build_live_executor(
    exchange_id: str,
    *,
    client: Any,
    market_rules: Mapping[str, SpotMarketRules],
    db_pool: Any,
    strategy_id: str,
    session_id: str,
    initial_quote: Decimal,
    poll_interval: float = 5.0,
    market_type: MarketType | str = MarketType.SPOT,
    leverage: int = 1,
) -> Any:
    """Create the live executor for one session over the keyed *client* (venue-validated).

    *market_rules* must be real ``MarketRules``: the protective machinery needs
    tick rounding, lot flooring and the trailing bounds, not just the paper
    pre-check trio. Binance SPOT seeds a USDT sleeve (D10); Binance USD⌂-M
    FUTURES (P4-D, behind the perps gate at every live surface) seeds the perp
    margin sleeve at the strategy's 1-3x isolated leverage.
    """
    from hydra.execution.live_executor import LiveTradingExecutor
    from hydra.risk.market_rules import MarketRules

    mt = _market_type(market_type)
    eid = validate_venue(exchange_id, mt, mode="live")
    bad = sorted(k for k, v in market_rules.items() if not isinstance(v, MarketRules))
    if bad:
        raise ValueError(f"live trading needs full MarketRules for: {', '.join(bad)}")
    if db_pool is None:
        raise ValueError("a live session needs the database (order ledger and credentials)")
    if mt is MarketType.FUTURES:
        from hydra.execution.perp_live import LivePerpTradingExecutor

        return LivePerpTradingExecutor(
            client,
            exchange_id=eid,
            initial_balances={"USDT": initial_quote},
            db_pool=db_pool,
            strategy_id=strategy_id,
            session_id=session_id,
            market_rules=market_rules,
            leverage=leverage,
            poll_interval=poll_interval,
        )
    return LiveTradingExecutor(
        client,
        exchange_id=eid,
        initial_balances={"USDT": initial_quote},
        db_pool=db_pool,
        strategy_id=strategy_id,
        session_id=session_id,
        market_rules=market_rules,
        poll_interval=poll_interval,
    )


def feed_exchange_factory(exchange_id: str) -> Callable[[], Any]:
    """Factory for the ccxt.pro client of a session's market-data feed (the venue's class)."""
    mt = _venue_market_type(exchange_id)
    cls_name = ccxt_class_for(exchange_id, mt)

    def _ccxt_factory() -> Any:
        import ccxt.pro

        cls = getattr(ccxt.pro, cls_name)
        return cls({"enableRateLimit": True, "options": _venue_options(exchange_id, mt)})

    return _ccxt_factory


def rest_exchange_factory(exchange_id: str) -> Any:
    """Public ccxt async REST client for feed warm-up and gap fills (no keys)."""
    import ccxt.async_support as ccxt_async

    mt = _venue_market_type(exchange_id)
    cls = getattr(ccxt_async, ccxt_class_for(exchange_id, mt))
    return cls({"enableRateLimit": True, "options": _venue_options(exchange_id, mt)})


type PoolKey = tuple[str, str | None]
type RestPoolFactory = Callable[[str, str | None], Any]


def _public_rest_factory(exchange_id: str, creds: str | None) -> Any:
    """The pool's default construction: public (no-keys) spot clients only (S10b).

    A non-``None`` credential identity is a distinct pool key, but building the
    trade-credentials client is P3-A/P3-B's seam, not this PR's.
    """
    if creds is not None:
        raise NotImplementedError("trade-credentials REST entries arrive with P3-A/P3-B")
    return rest_exchange_factory(exchange_id)


@dataclass(eq=False, slots=True)
class _PoolEntry:
    key: PoolKey
    client: Any
    refs: int


class RestHandle:
    """One reference on a pooled REST client; ``client`` is the caller's lease object."""

    __slots__ = ("_entry", "_pool", "_released", "client")

    def __init__(self, pool: SharedRestClients, entry: _PoolEntry) -> None:
        self._pool = pool
        self._entry = entry
        self.client: Any = entry.client
        self._released = False

    async def release(self) -> None:
        """Drop this reference; the last release closes the client.

        Raises ``RuntimeError`` on a second release of the same handle — a leak of
        refcount bugs beats hiding one. Cancellation-safe: the reference drops
        before the first suspension, so a cancelled release never leaks it.
        """
        if self._released:
            raise RuntimeError("RestHandle.release() called twice")
        self._released = True
        await self._pool._release(self._entry)


class SharedRestClients:
    """Process-wide refcounted REST client pool, keyed by (exchange id, credential identity).

    The first reference constructs the client through *rest_factory* (venue-validated for
    the public entries), every later reference reuses it, and the last release closes it.
    The public entry (``creds=None``) and a credentials-scoped entry of the same exchange
    are distinct keys that coexist; constructing the trade-credentials client itself is
    P3-A/P3-B's to add behind the same *rest_factory* seam. Releases never wait on the
    lock: the decrement is synchronous, so a cancelled release cannot leak a reference.
    """

    def __init__(self, rest_factory: RestPoolFactory = _public_rest_factory) -> None:
        self._factory = rest_factory
        self._entries: dict[PoolKey, _PoolEntry] = {}
        self._lock = asyncio.Lock()

    async def handle(self, exchange_id: str, creds: str | None = None) -> RestHandle:
        """Acquire the entry for ``(exchange_id, creds)``; constructing it on first use."""
        key: PoolKey = (exchange_id, creds)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _PoolEntry(key, self._factory(exchange_id, creds), 0)
            entry.refs += 1
            return RestHandle(self, entry)

    async def markets_loader(self, exchange_id: str) -> list[Mapping[str, Any]]:
        """Public ``load_markets`` for ``MarketRulesCache`` through a pooled handle."""
        handle = await self.handle(exchange_id)
        try:
            markets = await handle.client.load_markets()
        finally:
            await handle.release()
        return list(markets.values())

    async def _release(self, entry: _PoolEntry) -> None:
        # Synchronous: a cancelled release must never be able to skip the decrement.
        entry.refs -= 1
        if entry.refs != 0:
            return
        self._entries.pop(entry.key, None)
        try:
            await self._close_quietly(entry)
        except asyncio.CancelledError:
            # The reference already dropped; finish the interrupted close.
            await self._close_quietly(entry)
            raise

    async def _close_quietly(self, entry: _PoolEntry) -> None:
        try:
            await entry.client.close()
        except Exception:
            logger.warning(
                "Pooled REST client close failed", exchange=entry.key[0], creds=entry.key[1]
            )


def bars_repository(db_pool: Any) -> MarketDataRepository | None:
    """Where the feed persists closed bars and reads warm-up history (``ts.ohlcv_1m``)."""
    return MarketDataRepository.from_pool(db_pool) if db_pool is not None else None


MarketsLoader = Callable[[str], Awaitable[Iterable[Mapping[str, Any]]]]
TradingClientFactory = Callable[..., Any]
"""``(exchange_id, creds, testnet, market_type=SPOT)`` -> the keyed live client.

The perp start passes ``MarketType.FUTURES`` as the fourth argument so the
built client is the binanceusdm class; the spot start keeps the three-arg call
(every existing injected fake keeps working)."""
FundingReaderFactory = Callable[[Any, ExchangeId], "FundingReader | None"]


@dataclass(frozen=True, slots=True)
class ClientFactory:
    """The exchange seams of a session: WebSocket feed, REST warm-up/gap fill, market rules.

    Production uses the public Binance spot clients and the ``ts.ohlcv_1m`` repository; tests
    and the paper smoke inject fakes. The REST client comes from the shared refcounted pool
    (S10b); the WebSocket client stays per feed. ``trading_client_factory`` builds the keyed
    live client from stored credentials — the seam the live tests use to inject the fake.
    ``funding_reader_factory`` builds the perp sessions' funding source (P4-C): the PG reader
    over ``ts.funding_rates`` in production, a preloaded reader in tests and backtests.
    """

    ws_factory: Callable[[str], Callable[[], Any]] = feed_exchange_factory
    rest_pool: SharedRestClients = field(default_factory=SharedRestClients)
    markets_loader: MarketsLoader | None = None
    repository_factory: Callable[[Any], MarketDataRepository | None] = bars_repository
    history_window: int = 1000
    trading_client_factory: TradingClientFactory = (
        lambda eid, creds, testnet, market_type=MarketType.SPOT: build_trading_client(
            eid, creds, testnet=testnet, market_type=market_type
        )
    )
    funding_reader_factory: FundingReaderFactory = default_funding_reader
