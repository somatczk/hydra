"""Tests for hydra.backtest.validation: daily returns, PSR/DSR and the CSCV PBO.

The PSR/DSR numbers are the worked example of Bailey & Lopez de Prado, "The Deflated
Sharpe Ratio" (2014): annual SR 2.5 over T=1250 daily observations, skewness -3,
kurtosis 10, N=100 trials, trial return variance V=0.5/250. The paper's answers are
E[max SR] = 0.1132 and DSR = 0.9004.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.backtest.validation import (
    daily_returns,
    deflated_sharpe,
    expected_max_sharpe,
    pbo_cscv,
    per_period_sharpe,
    probabilistic_sharpe,
    probabilistic_sharpe_from_moments,
)

_SR = 2.5 / math.sqrt(250)  # the paper's annual SR 2.5 as a per-day Sharpe
PBO_SEED0_320X10_B8 = 0.34285714285714286


def test_dsr_reproduces_bailey_lopez_de_prado_example() -> None:
    sr0 = expected_max_sharpe(100, 0.5 / 250)
    assert sr0 == pytest.approx(0.11317, abs=1e-4)
    psr = probabilistic_sharpe_from_moments(_SR, sr0, 1250, -3.0, 10.0)
    assert psr == pytest.approx(0.90040, abs=1e-4)


def test_dsr_with_one_trial_is_psr_against_zero() -> None:
    assert expected_max_sharpe(1, 0.5 / 250) == 0.0
    psr = probabilistic_sharpe_from_moments(_SR, 0.0, 1250, -3.0, 10.0)
    assert psr == pytest.approx(0.9999969, abs=1e-6)


def test_psr_matches_the_reference_value_on_the_d1_fixture() -> None:
    """Returns [0.1, -0.1, 0, 0.1]: the textbook PSR against zero is 0.6640978512094959."""
    assert per_period_sharpe([0.1, -0.1, 0.0, 0.1]) == pytest.approx(
        0.025 / 0.0957427107756338, rel=1e-12
    )
    assert probabilistic_sharpe([0.1, -0.1, 0.0, 0.1]) == pytest.approx(
        0.6640978512094959, rel=1e-12
    )


def test_psr_is_none_without_dispersion() -> None:
    assert probabilistic_sharpe([0.01]) is None
    assert probabilistic_sharpe([0.01, 0.01, 0.01]) is None


def test_deflated_sharpe_shrinks_with_the_trial_count() -> None:
    returns = [0.1, -0.1, 0.0, 0.1]
    one = deflated_sharpe(returns, 1, 0.0)
    many = deflated_sharpe(returns, 100, 0.5 / 250)
    assert one == pytest.approx(0.6640978512094959, rel=1e-12)
    assert many is not None and many < one


def test_pbo_perfect_reversal_is_one() -> None:
    matrix = np.array([[0.01, -0.01], [0.02, -0.02], [-0.01, 0.01], [-0.02, 0.02]])
    assert pbo_cscv(matrix, blocks=2) == 1.0


def test_pbo_planted_skill_is_zero() -> None:
    rng = np.random.default_rng(7)
    matrix = rng.normal(0.0, 0.01, size=(640, 20))
    matrix[:, 3] += 0.003
    assert pbo_cscv(matrix, blocks=16) == 0.0


def test_pbo_of_noise_is_centred() -> None:
    values = []
    for seed in range(60):
        rng = np.random.default_rng(seed)
        values.append(pbo_cscv(rng.normal(0.0, 0.01, size=(320, 10)), blocks=8))
    assert 0.45 <= float(np.mean(values)) <= 0.55
    # one fixed realisation as a regression number: a single draw varies widely
    rng = np.random.default_rng(0)
    assert pbo_cscv(rng.normal(0.0, 0.01, size=(320, 10)), blocks=8) == pytest.approx(
        PBO_SEED0_320X10_B8, abs=1e-9
    )


def test_pbo_needs_two_configs_and_enough_rows() -> None:
    assert pbo_cscv(np.zeros((100, 1)), blocks=16) is None
    assert pbo_cscv(np.zeros((31, 4)), blocks=16) is None
    assert pbo_cscv(np.zeros((32, 4)), blocks=16) is not None


def test_daily_returns_from_hourly_equity() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    equity = [Decimal(100)] * 72
    equity[23] = Decimal(100)
    equity[47] = Decimal(110)
    equity[71] = Decimal(99)
    timestamps = [start + timedelta(hours=i) for i in range(72)]
    assert daily_returns(equity, timestamps).tolist() == [0.1, -0.1]


def test_daily_returns_pad_free_but_length_checked() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert daily_returns([Decimal(100)], [start]).tolist() == []
    with pytest.raises(ValueError, match="equity has"):
        daily_returns([Decimal(100), Decimal(101)], [start])
