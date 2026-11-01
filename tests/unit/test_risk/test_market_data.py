"""The correlation map the correlation gate would read: daily log returns, not price levels."""

from __future__ import annotations

import numpy as np

from hydra.risk.market_data import fetch_risk_market_data


class _FakeExchange:
    """ccxt's fetch_ohlcv shape over fixed daily closes; no network."""

    def __init__(self, closes: dict[str, np.ndarray]) -> None:
        self._closes = closes

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        assert (timeframe, limit) == ("1d", 30)
        return [
            [i * 86_400_000.0, c, c, c, c, 1_000.0]
            for i, c in enumerate(self._closes[symbol][-limit:].tolist())
        ]


def _trending(returns: np.ndarray) -> np.ndarray:
    return 100.0 * np.exp(np.concatenate(([0.0], np.cumsum(returns))))


def _returns(closes: np.ndarray) -> np.ndarray:
    return np.diff(np.log(closes))


rng = np.random.default_rng(3)
_A = 0.02 + rng.normal(0.0, 0.02, 29)
SERIES = {
    "AAAUSDT": _trending(_A),
    "BBBUSDT": _trending(0.02 + rng.normal(0.0, 0.02, 29)),  # same uptrend, its own daily moves
    "CCCUSDT": _trending(_A + rng.normal(0.0, 0.003, 29)),  # A's daily moves plus a little noise
}


async def test_two_uptrends_with_independent_daily_moves_are_not_flagged() -> None:
    a, b = SERIES["AAAUSDT"], SERIES["BBBUSDT"]
    assert np.corrcoef(a, b)[0, 1] > 0.9  # what the level correlation used to flag
    assert abs(np.corrcoef(_returns(a), _returns(b))[0, 1]) < 0.3

    data = await fetch_risk_market_data(_FakeExchange(SERIES), list(SERIES))

    assert "BBBUSDT" not in data.correlation_map.get("AAAUSDT", [])
    assert "BBBUSDT" not in data.correlation_map


async def test_two_coins_with_correlated_daily_moves_are_flagged() -> None:
    assert np.corrcoef(_returns(SERIES["AAAUSDT"]), _returns(SERIES["CCCUSDT"]))[0, 1] > 0.95

    data = await fetch_risk_market_data(_FakeExchange(SERIES), list(SERIES))

    assert data.correlation_map == {"AAAUSDT": ["CCCUSDT"], "CCCUSDT": ["AAAUSDT"]}


async def test_fewer_than_20_daily_returns_correlate_nothing() -> None:
    short = {s: closes[-20:] for s, closes in SERIES.items()}  # 19 returns

    data = await fetch_risk_market_data(_FakeExchange(short), list(short))

    assert data.correlation_map == {}
