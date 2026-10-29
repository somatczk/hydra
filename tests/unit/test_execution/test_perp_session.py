"""Perp paper sessions through the real SessionManager: dispatch, shorts, guards.

Covers the P4-C session seams: a FUTURES config starts on the perp executor with its
strategy leverage (validated against the live cap at start), SHORT entries flow through
the wiring with BUY-side protection, manual orders are long-only (the 2.8.0 cut), a stop
closes a session's shorts by BUY-back, and a liquidation halts the session honestly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.core.types import Direction, OrderType, Side
from hydra.execution.client_factory import ClientFactory
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.perp_trading import PerpPaperTradingExecutor, static_funding_reader
from hydra.risk.pretrade import RiskConfig
from tests.unit.test_execution.session_harness import (
    Harness,
    rule,
    settle,
    write_strategy,
)

D = Decimal
ALWAYS = rule(("greater_than", 0.0))
AT_100 = rule(("less_than", 100.5), ("greater_than", 99.5))


def perp_clients(h: Harness, funding: dict[str, list[Any]] | None = None) -> ClientFactory:
    base = h.clients()
    return ClientFactory(
        ws_factory=base.ws_factory,
        rest_pool=base.rest_pool,
        markets_loader=base.markets_loader,
        repository_factory=base.repository_factory,
        history_window=base.history_window,
        funding_reader_factory=lambda _pool, _eid: static_funding_reader(funding or {}),
    )


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    assert harness.mgr is not None
    harness.mgr._clients = perp_clients(harness)
    yield harness
    await harness.close()


def write_perp(strategies_dir: Path, sid: str, **kwargs: Any) -> str:
    return write_strategy(
        strategies_dir,
        sid,
        exchange_id="binanceusdm",
        market_type="FUTURES",
        **kwargs,
    )


class TestSessionDispatch:
    async def test_a_futures_config_starts_on_the_perp_executor(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-start", entry_long=ALWAYS)
        session = await h.session(sid)
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        assert ex.get_account_snapshot().leverage == 1
        assert session.exchange_id == "binanceusdm"

    async def test_leverage_rides_the_config_into_the_executor(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-lev", entry_long=ALWAYS, leverage=3)
        session = await h.session(sid)
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        assert ex.get_account_snapshot().leverage == 3

    async def test_start_refuses_leverage_above_the_live_cap(self, strategies_dir: Path) -> None:
        sid = write_perp(strategies_dir, "perp-hot", entry_long=ALWAYS, leverage=2)
        harness = Harness(strategies_dir, risk_config=RiskConfig(max_perp_leverage=1))
        await harness.start()
        assert harness.mgr is not None
        harness.mgr._clients = perp_clients(harness)
        try:
            with pytest.raises(ValueError, match="leverage 2x is outside 1-1x"):
                await harness.mgr.start_session(sid, "paper", D("1000"))
        finally:
            await harness.close()

    async def test_spot_configs_stay_on_the_spot_executor(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(strategies_dir, "spot-plain", entry_long=ALWAYS)
        session = await h.session(sid)
        assert isinstance(session._executor, PaperTradingExecutor)
        assert not isinstance(session._executor, PerpPaperTradingExecutor)

    async def test_the_spot_builder_refuses_the_perp_venue(self) -> None:
        from hydra.execution.client_factory import build_paper_executor

        with pytest.raises(UnsupportedVenueError):
            build_paper_executor("binanceusdm", {"USDT": D("100")}, None, "s", "sess")


class TestShortsThroughTheWiring:
    async def test_a_short_entry_opens_a_signed_position_with_buy_protection(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-short", entry_short=AT_100)
        session = await h.session(sid)
        await h.bar(session, "100", exchange_id="binanceusdm")
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        [position] = await ex.get_positions()
        assert position.direction == Direction.SHORT
        assert position.quantity > 0
        stops = await ex.fetch_open_orders("BTCUSDT")
        assert stops and all(o["side"] == "BUY" for o in stops)
        assert stops[0]["stop_price"] > 100.0  # the stop sits above a short's entry

    async def test_a_short_exits_by_buy_back_with_realized_pnl(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(
            strategies_dir,
            "perp-short-exit",
            entry_short=AT_100,
            exit_short=rule(("less_than", 95.0)),
        )
        session = await h.session(sid)
        await h.bar(session, "100", exchange_id="binanceusdm")
        await h.bar(session, "90", exchange_id="binanceusdm")
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        assert (await ex.get_positions()) == []
        # the strategy exit is a reduce-only BUY whose realized PnL is the short's gain
        buys = [f for f in ex.get_fills() if f.side == "BUY" and f.net_pnl is not None]
        assert buys and buys[0].net_pnl > 0
        om = session._order_manager
        assert om is not None
        exits = [oid for oid, t in om._orders.items() if t.request.tag == "exit"]
        assert exits

    async def test_manual_orders_on_perps_are_long_only(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-manual")
        await h.session(sid)
        assert h.mgr is not None
        with pytest.raises(ValueError, match=r"long-only in 2\.8\.0"):
            await h.mgr.submit_manual_order(
                symbol="BTCUSDT", side=Side.SELL, order_type=OrderType.MARKET, quantity=D("0.1")
            )

    async def test_a_manual_buy_over_a_strategy_short_is_refused(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-flip", entry_short=AT_100)
        session = await h.session(sid)
        await h.bar(session, "100", exchange_id="binanceusdm")
        assert h.mgr is not None
        with pytest.raises(ValueError, match=r"long-only in 2\.8\.0"):
            await h.mgr.submit_manual_order(
                symbol="BTCUSDT", side=Side.BUY, order_type=OrderType.MARKET, quantity=D("0.1")
            )

    async def test_stopping_the_session_closes_a_short_by_buy_back(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_perp(strategies_dir, "perp-flat", entry_short=AT_100)
        session = await h.session(sid)
        await h.bar(session, "100", exchange_id="binanceusdm")
        await h.price(session, "100")
        assert h.mgr is not None
        await h.mgr.stop_session(session.session_id)
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        assert (await ex.get_positions()) == []
        # the flatten went through the OrderManager as a reduce-only BUY
        om = session._order_manager
        assert om is not None
        flatten = [t for t in om._orders.values() if t.request.tag == "flatten"]
        assert flatten and all(
            t.request.side == Side.BUY and t.request.reduce_only for t in flatten
        )
        closing = [f for f in ex.get_fills() if f.side == "BUY" and f.net_pnl is not None]
        assert [f.side for f in closing] == ["BUY"]
        assert session.status == "stopped"
        assert session.positions_at_stop == 0

    async def test_a_liquidation_halts_the_session_honestly(self, strategies_dir: Path) -> None:
        # the 45% catastrophe stop sits beyond the 3x liquidation boundary (~33%), so a
        # 40% drop liquidates before any protective level fires; the wide
        # max_risk_per_trade admits the far stop
        harness = Harness(strategies_dir, risk_config=RiskConfig(max_risk_per_trade=D("0.5")))
        await harness.start()
        assert harness.mgr is not None
        harness.mgr._clients = perp_clients(harness)
        try:
            sid = write_perp(
                strategies_dir,
                "perp-liq",
                entry_long=ALWAYS,
                leverage=3,
                risk={"stop_loss_method": "fixed_pct", "stop_loss_value": 45.0},
            )
            session = await harness.session(sid)
            await harness.bar(session, "100", exchange_id="binanceusdm")
            ex = session._executor
            assert isinstance(ex, PerpPaperTradingExecutor)
            snap = ex.get_account_snapshot()
            assert snap.leverage == 3 and snap.base_totals == {"BTC": D("3")}
            await harness.bar(session, "60", exchange_id="binanceusdm")
            await settle()
            snap = ex.get_account_snapshot()
            assert snap.liquidations == 1
            assert snap.halted is not None and "liquidated" in snap.halted
            assert session.status == "error"
            assert session.error_message is not None and "liquidated" in session.error_message
        finally:
            await harness.close()


class TestConfigValidation:
    def test_the_strategy_config_validates_the_perps_fields(self) -> None:
        from hydra.strategy.config import StrategyConfig

        base = {
            "id": "c",
            "name": "c",
            "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        }
        cfg = StrategyConfig(
            **base,
            exchange={"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": 3},
        )
        assert (cfg.exchange.leverage, cfg.exchange.margin_mode) == (3, "isolated")
        with pytest.raises(ValueError, match="leverage"):
            StrategyConfig(
                **base,
                exchange={"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": 4},
            )
        with pytest.raises(ValueError, match="spot"):
            StrategyConfig(
                **base,
                exchange={"exchange_id": "binance", "market_type": "SPOT", "leverage": 2},
            )

    def test_the_service_layer_writes_the_perp_exchange_block(self) -> None:
        from hydra.services.strategies import _exchange_block

        assert _exchange_block("binanceusdm", 3) == {
            "exchange_id": "binanceusdm",
            "market_type": "FUTURES",
            "leverage": 3,
            "margin_mode": "isolated",
        }
        assert _exchange_block("binance", 1) == {
            "exchange_id": "binance",
            "market_type": "SPOT",
        }
        from hydra.services.errors import ServiceError

        with pytest.raises(ServiceError, match="futures"):
            _exchange_block("binance", 2)
