"""Point-in-time features in StrategyContext: as-of alignment, revisions, staleness, transforms."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.events import BarEvent
from hydra.core.features import FEATURES, FeatureDef
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.strategy.builtin.rule_based import RuleBasedStrategy, param_bindings
from hydra.strategy.condition_schema import Condition, RuleSet
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext, resolve_indicator
from hydra.strategy.rule_engine import evaluate_condition, indicator_lookback

_T0 = datetime(2026, 5, 4, tzinfo=UTC)
_TF = Timeframe.H1
_SYM = "BTC/USDT"
_OI = "feature.test.oi"
_VIX = "feature.test.vix"


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(FEATURES, "test.oi", FeatureDef("test.oi", "symbol", timedelta(hours=3)))
    monkeypatch.setitem(FEATURES, "test.vix", FeatureDef("test.vix", "market", timedelta(days=2)))


def _close(i: int) -> datetime:
    """Close time of bar *i* (bars are labelled by their open)."""
    return _T0 + timedelta(hours=i + 1)


def _ctx(n: int, symbol: str = _SYM) -> StrategyContext:
    ctx = StrategyContext()
    for i in range(n):
        c = Decimal(100 + i)
        bar = OHLCV(
            open=c, high=c, low=c, close=c, volume=Decimal(1), timestamp=_T0 + timedelta(hours=i)
        )
        ctx.add_bar(symbol, _TF, bar)
    return ctx


def _vals(ctx: StrategyContext, name: str = _OI, symbol: str = _SYM, **params: int) -> list[float]:
    return [float(v) for v in ctx.indicator(name, symbol, _TF, **params)]


def test_a_value_is_invisible_until_its_available_at() -> None:
    ctx = _ctx(4)
    ctx.add_feature(
        "test.oi", _SYM, ts=_close(0), available_at=_close(1) + timedelta(seconds=1), value=7.0
    )
    vals = _vals(ctx)
    assert np.isnan(vals[:2]).all()
    assert vals[2:] == [7.0, 7.0]


def test_a_value_available_exactly_at_the_close_is_visible_on_that_bar() -> None:
    ctx = _ctx(2)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=5.0)
    assert _vals(ctx) == [5.0, 5.0]


def test_a_revision_supersedes_the_first_vintage_only_from_its_own_available_at() -> None:
    ctx = _ctx(4)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=1.0)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(2), value=2.0)
    assert _vals(ctx) == [1.0, 1.0, 2.0, 2.0]


def test_a_newer_event_wins_over_a_later_published_older_one() -> None:
    ctx = _ctx(3)
    ctx.add_feature("test.oi", _SYM, ts=_close(1), available_at=_close(1), value=9.0)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(2), value=3.0)
    assert _vals(ctx)[2] == 9.0


def test_a_value_older_than_max_age_is_nan() -> None:
    ctx = _ctx(6)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=4.0)
    vals = _vals(ctx)
    assert vals[:4] == [4.0, 4.0, 4.0, 4.0]  # ages 0h..3h, max_age 3h
    assert np.isnan(vals[4:]).all()


def test_a_symbol_feature_does_not_leak_to_another_symbol() -> None:
    ctx = _ctx(2)
    for i in range(2):
        c = Decimal(1)
        ctx.add_bar("ETH/USDT", _TF, OHLCV(c, c, c, c, c, _T0 + timedelta(hours=i)))
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=5.0)
    assert np.isnan(_vals(ctx, symbol="ETH/USDT")).all()


def test_a_market_feature_reaches_every_symbol() -> None:
    ctx = _ctx(2)
    for i in range(2):
        c = Decimal(1)
        ctx.add_bar("ETH/USDT", _TF, OHLCV(c, c, c, c, c, _T0 + timedelta(hours=i)))
    ctx.add_feature("test.vix", "", ts=_close(0), available_at=_close(0), value=20.0)
    assert _vals(ctx, _VIX) == [20.0, 20.0]
    assert _vals(ctx, _VIX, symbol="ETH/USDT") == [20.0, 20.0]


def test_add_feature_drops_the_cached_values() -> None:
    ctx = _ctx(2)
    assert np.isnan(_vals(ctx)).all()
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=5.0)
    assert _vals(ctx) == [5.0, 5.0]


def test_add_bar_extends_the_feature_values() -> None:
    ctx = _ctx(1)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=5.0)
    assert _vals(ctx) == [5.0]
    c = Decimal(1)
    ctx.add_bar(_SYM, _TF, OHLCV(c, c, c, c, c, _T0 + timedelta(hours=1)))
    assert _vals(ctx) == [5.0, 5.0]


def _series(ctx: StrategyContext, values: list[float]) -> None:
    for i, v in enumerate(values):
        ctx.add_feature("test.oi", _SYM, ts=_close(i), available_at=_close(i), value=v)


def test_diff_transform() -> None:
    ctx = _ctx(4)
    _series(ctx, [1.0, 2.0, 4.0, 7.0])
    vals = _vals(ctx, diff=2)
    assert np.isnan(vals[:2]).all()
    assert vals[2:] == [3.0, 5.0]


def test_pct_change_transform() -> None:
    ctx = _ctx(3)
    _series(ctx, [100.0, 110.0, 99.0])
    vals = _vals(ctx, pct_change=1)
    assert np.isnan(vals[0])
    assert vals[1:] == pytest.approx([0.10, -0.10])


def test_zscore_transform_uses_only_the_window_up_to_each_bar() -> None:
    ctx = _ctx(5)
    _series(ctx, [1.0, 2.0, 3.0, 4.0, 100.0])
    vals = _vals(ctx, zscore=3)
    assert np.isnan(vals[:2]).all()
    window = np.array([2.0, 3.0, 4.0])
    assert vals[3] == pytest.approx((4.0 - window.mean()) / window.std())
    window = np.array([3.0, 4.0, 100.0])
    assert vals[4] == pytest.approx((100.0 - window.mean()) / window.std())


def test_a_transform_over_a_stale_gap_is_nan() -> None:
    ctx = _ctx(6)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=1.0)
    ctx.add_feature("test.oi", _SYM, ts=_close(5), available_at=_close(5), value=2.0)
    assert np.isnan(_vals(ctx, diff=1)[5])  # bar 4 is stale, so its diff is NaN


def test_resolve_refuses_an_unregistered_feature() -> None:
    with pytest.raises(ValueError, match="Unknown feature"):
        resolve_indicator("feature.nope.x", {})


def test_resolve_refuses_two_transforms() -> None:
    with pytest.raises(ValueError, match="one transform"):
        resolve_indicator(_OI, {"zscore": 10, "diff": 1})


def test_resolve_refuses_an_unknown_parameter() -> None:
    with pytest.raises(ValueError, match="period"):
        resolve_indicator(_OI, {"period": 10})


def test_resolve_range_checks_the_window() -> None:
    with pytest.raises(ValueError, match=r"\[1, 200\]"):
        resolve_indicator(_OI, {"zscore": 500})


# -- Rule strategies ------------------------------------------------------------------------------


def _strategy(
    ctx: StrategyContext, entry: dict[str, object], required_history: int = 2
) -> RuleBasedStrategy:
    rules = {"entry_long": {"operator": "AND", "conditions": [entry]}}
    cfg = StrategyConfig(
        id="feat",
        name="Feature rule",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=[_SYM],
        parameters={"rules": rules, "required_history": required_history},
    )
    return RuleBasedStrategy(config=cfg, context=ctx)


async def _entries(ctx: StrategyContext, entry: dict[str, object]) -> int:
    strategy = _strategy(ctx, entry)
    last = ctx.latest_bar(_SYM, _TF)
    assert last is not None
    signals = await strategy.on_bar(BarEvent(symbol=Symbol(_SYM), timeframe=_TF, ohlcv=last))
    return len(signals)


_ABOVE_5 = {"indicator": _OI, "params": {}, "comparator": "greater_than", "value": 5.0}


async def test_a_feature_condition_gates_an_entry() -> None:
    ctx = _ctx(3)
    _series(ctx, [1.0, 2.0, 9.0])
    assert await _entries(ctx, _ABOVE_5) == 1
    ctx.add_feature(
        "test.oi", _SYM, ts=_close(2), available_at=_close(2) + timedelta(minutes=1), value=1.0
    )
    assert (
        await _entries(ctx, _ABOVE_5) == 1
    )  # a later revision of bar 2 is not yet knowable at its close


async def test_a_stale_feature_blocks_an_entry() -> None:
    ctx = _ctx(6)
    ctx.add_feature("test.oi", _SYM, ts=_close(0), available_at=_close(0), value=9.0)
    assert await _entries(ctx, _ABOVE_5) == 0


def test_an_unregistered_feature_fails_the_build() -> None:
    entry = {
        "indicator": "feature.nope.x",
        "params": {},
        "comparator": "greater_than",
        "value": 1.0,
    }
    with pytest.raises(ValueError, match="Unknown feature"):
        _strategy(_ctx(2), entry)


def test_a_feature_compares_against_another_feature_reference() -> None:
    ctx = _ctx(3)
    _series(ctx, [1.0, 2.0, 9.0])
    ctx.add_feature("test.vix", "", ts=_close(0), available_at=_close(0), value=4.0)
    cond = Condition(indicator=_OI, params={}, comparator="crosses_above", value=_VIX)
    assert evaluate_condition(cond, ctx, _SYM, _TF)


def test_lookback_counts_the_transform_window() -> None:
    assert indicator_lookback(_OI, {}) == 2
    assert indicator_lookback(_OI, {"zscore": 50}) == 52
    assert indicator_lookback(_OI, {"diff": 3}) == 5


def test_a_bound_name_tunes_a_feature_transform_window() -> None:
    entry = {
        "indicator": _OI,
        "params": {"zscore": 20},
        "param_overrides": {"oi_window": "zscore"},
        "comparator": "greater_than",
        "value": 2.0,
    }
    rules = RuleSet.model_validate({"entry_long": {"operator": "AND", "conditions": [entry]}})
    assert list(param_bindings(rules)) == ["oi_window"]
