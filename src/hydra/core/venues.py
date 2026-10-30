"""The venue registry: one frozen ``VenueSpec`` per (exchange_id, MarketType).

Every surface that encodes a venue fact reads it here — the allowed-venue gate and the
client construction (``hydra.execution.client_factory``), the market rules
(``hydra.risk.market_rules``), the public market-data fetchers (``hydra.data.ingestion``),
the key-permission ceremony (``hydra.services.live_gate``, the Settings connect) and the
per-venue protective adapters (``hydra.execution.venue_stops``). ``core`` is the only
layer both ``data`` and ``execution`` may import, so the registry lives here.

Registered venues (P7-F): binance SPOT, binanceusdm FUTURES and — live only — bybit SPOT.
Kraken and OKX are NOT registered: Kraken has no testnet and no key-permission endpoint the
fail-closed arm re-fetch could honestly re-verify; OKX's demo trading needs separate
demo-account keys and its ``cancelAllOrders`` is False, which would weaken the kill-switch
sweep. Each becomes one bounded follow-up PR by adding a spec here.

Bybit is LIVE-ONLY (``allowed_modes``): paper and backtest sessions stay Binance-only, so a
paper or backtest start on bybit is refused by the registry's mode cap, not incidentally.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from hydra.core.types import MarketType

SessionMode = Literal["paper", "live"]
"""What a start/recovery asks a venue for; backtests ride the paper cap."""


class KeyCheck(Enum):
    """How a key's permissions are re-read: the fail-closed R8 ceremony's per-venue read."""

    SAPI = "sapi"
    """Binance: GET /sapi/v1/account/apiRestrictions (``sapiGetAccountApiRestrictions``)."""

    BYBIT_QUERY_API = "bybit_query_api"
    """Bybit: GET /v5/user/query-api (``privateGetV5UserQueryApi``)."""


class TrailingKind(Enum):
    """The venue's trailing-stop wire form; ``NONE`` falls back to the fixed stop."""

    BIPS_FILTER = "bips_filter"
    """Binance spot: a single STOP_LOSS with ``trailingDelta`` in BIPS, bounded per symbol
    by the raw TRAILING_DELTA filter (``minTrailingBelowDelta``/``maxTrailingBelowDelta``)."""

    CALLBACK_RATE_PCT = "callback_rate_pct"
    """Binance USD⌂-M: a TRAILING_STOP_MARKET with ``callbackRate`` in PERCENT (0.1-10)."""

    NONE = "none"
    """Bybit: ``createTrailingAmountOrder`` carries an amount, not a percent — hydra's
    percent trail has no honest wire form, so a trailing spec degrades to the fixed stop."""


class ProtectiveKind(Enum):
    """The venue's exchange-side protective form for a held position."""

    BINANCE_SPOT_OCO = "binance_spot_oco"
    """The Binance spot OCO ladder (``exchange_stops``)."""

    BINANCE_FAPI_PAIR = "binance_fapi_pair"
    """The Binance USD⌂-M reduce-only conditional pair (``perp_live``); futures have no OCO."""

    BYBIT_PAIR = "bybit_pair"
    """Bybit spot has no OCO/list orders: a TP+SL conditional pair with sibling-cancel
    (``venue_stops``), the stop leg placed first."""


@dataclass(frozen=True, slots=True)
class VenueSpec:
    """One venue's facts; the registry is the single source of every one of them."""

    exchange_id: str
    market_type: MarketType
    ccxt_class: str
    client_options: Mapping[str, Any]
    has_testnet: bool
    key_check: KeyCheck
    trailing_kind: TrailingKind
    protective_kind: ProtectiveKind
    allowed_modes: frozenset[SessionMode]
    in_universe: bool
    """True only for the venue the top-100 universe is ranked from (binance): membership
    there implies the symbol is listed; every other venue needs the save-time
    venue-existence check."""


BINANCE_SPOT = VenueSpec(
    exchange_id="binance",
    market_type=MarketType.SPOT,
    ccxt_class="binance",
    client_options={"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}},
    has_testnet=True,
    key_check=KeyCheck.SAPI,
    trailing_kind=TrailingKind.BIPS_FILTER,
    protective_kind=ProtectiveKind.BINANCE_SPOT_OCO,
    allowed_modes=frozenset({"paper", "live"}),
    in_universe=True,
)

BINANCE_USDM = VenueSpec(
    exchange_id="binanceusdm",
    market_type=MarketType.FUTURES,
    ccxt_class="binanceusdm",
    client_options={
        "defaultType": "swap",
        "defaultSubType": "linear",
        "fetchMarkets": {"types": ["linear"]},
    },
    has_testnet=True,
    key_check=KeyCheck.SAPI,
    trailing_kind=TrailingKind.CALLBACK_RATE_PCT,
    protective_kind=ProtectiveKind.BINANCE_FAPI_PAIR,
    allowed_modes=frozenset({"paper", "live"}),
    in_universe=True,
)

BYBIT_SPOT = VenueSpec(
    exchange_id="bybit",
    market_type=MarketType.SPOT,
    ccxt_class="bybit",
    # bybit's own defaultType is 'swap'; the spot pair pins it and fetches spot markets only
    client_options={"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}},
    has_testnet=True,
    key_check=KeyCheck.BYBIT_QUERY_API,
    trailing_kind=TrailingKind.NONE,
    protective_kind=ProtectiveKind.BYBIT_PAIR,
    allowed_modes=frozenset({"live"}),
    in_universe=False,
)

VENUES: Mapping[tuple[str, MarketType], VenueSpec] = {
    (BINANCE_SPOT.exchange_id, MarketType.SPOT): BINANCE_SPOT,
    (BINANCE_USDM.exchange_id, MarketType.FUTURES): BINANCE_USDM,
    (BYBIT_SPOT.exchange_id, MarketType.SPOT): BYBIT_SPOT,
}
"""The registered venues, keyed by the (exchange_id, MarketType) pair a session carries."""


def registered_venues() -> frozenset[tuple[str, MarketType]]:
    """Every registered (exchange_id, market_type) pair — the allowed-venue gate's set."""
    return frozenset(VENUES)


def registered_venue_ids() -> frozenset[str]:
    """Every registered exchange id (public market data and the order-book allowlist)."""
    return frozenset(exchange_id for exchange_id, _ in VENUES)


def spot_venue_ids() -> frozenset[str]:
    """The registered SPOT venue ids — the live arm ceremony loops the connected ones."""
    return frozenset(
        exchange_id for (exchange_id, market_type) in VENUES if market_type is MarketType.SPOT
    )


def find_venue(exchange_id: str, market_type: MarketType) -> VenueSpec | None:
    """The spec for a registered pair, or None (the caller owns the typed refusal)."""
    return VENUES.get((exchange_id, market_type))


def venue_market_type(exchange_id: str) -> MarketType:
    """The market type an exchange id trades (SPOT for an unknown id — the pair gate
    then refuses it with the venue named)."""
    if any(
        spec.exchange_id == exchange_id and spec.market_type is MarketType.FUTURES
        for spec in VENUES.values()
    ):
        return MarketType.FUTURES
    return MarketType.SPOT
