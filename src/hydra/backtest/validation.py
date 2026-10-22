"""Statistical validation of backtest results: PSR/DSR and the CSCV probability of overfitting.

Pure module: numpy and the stdlib ``statistics.NormalDist`` only — no scipy, and no
annualisation: everything is computed on per-period (daily) returns, so the results are
correct whatever the bar size or P2-B's annualisation constants. Formulas follow Bailey &
Lopez de Prado, "The Deflated Sharpe Ratio" (Journal of Portfolio Management, 2014) and
"The Probability of Backtest Overfitting" (Journal of Computational Finance, 2014).

Conventions: the per-period Sharpe ratio is ``mean / std(ddof=1)``; skewness and kurtosis
are population (biased) moments with kurtosis non-excess (3 for a normal). Undefined
results are ``None``, never 0.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from statistics import NormalDist

import numpy as np

_EULER_MASCHERONI = 0.5772156649015329
BLOCKS = 16
_DEGENERATE = 1e-12

_ND = NormalDist()


def daily_returns(equity: Sequence[Decimal | float], timestamps: Sequence[datetime]) -> np.ndarray:
    """Per-day simple returns: the last equity of each UTC date, pct-changed in Decimal.

    Bars coarser than one day give one value per date, i.e. per-bar returns. Fewer than
    two distinct dates give an empty array. Division happens in Decimal, so round numbers
    stay exact (110 after 100 is 0.1, not 0.10000000000000009).
    """
    if len(equity) != len(timestamps):
        msg = f"equity has {len(equity)} points, timestamps has {len(timestamps)}"
        raise ValueError(msg)
    last_by_date: dict[datetime, Decimal] = {}
    for eq, ts in zip(equity, timestamps, strict=True):
        last_by_date[ts.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)] = (
            Decimal(eq)
        )
    values = [last_by_date[day] for day in sorted(last_by_date)]
    if len(values) < 2:
        return np.array([], dtype=np.float64)
    returns = [
        float(cur / prev - 1) if prev != 0 else 0.0 for prev, cur in itertools.pairwise(values)
    ]
    return np.asarray(returns, dtype=np.float64)


def per_period_sharpe(returns: Sequence[float] | np.ndarray) -> float:
    """``mean / std(ddof=1)`` of per-period returns; 0.0 when the dispersion is float noise."""
    r = np.asarray(returns, dtype=np.float64)
    if len(r) < 2:
        return 0.0
    std = float(np.std(r, ddof=1))
    if std < _DEGENERATE:
        return 0.0
    return float(np.mean(r)) / std


def probabilistic_sharpe_from_moments(
    sr: float, sr0: float, t: int, skew: float, kurtosis: float
) -> float | None:
    """PSR from already-computed moments: ``Phi((SR - SR0) sqrt(T-1) / sd(SR))``.

    ``sd(SR) = sqrt(1 - skew * SR + (kurtosis - 1) / 4 * SR^2)`` is the standard error of
    the Sharpe estimate; a non-positive variance means the moments are too extreme for the
    estimator, and the result is None.
    """
    if t < 2:
        return None
    variance = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if variance <= 0.0:
        return None
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(variance)
    return _ND.cdf(z)


def probabilistic_sharpe(returns: Sequence[float] | np.ndarray, sr0: float = 0.0) -> float | None:
    """Probability that the true Sharpe ratio exceeds ``sr0``, from the returns themselves.

    None when there are fewer than two returns or the dispersion is float noise.
    """
    r = np.asarray(returns, dtype=np.float64)
    if len(r) < 2:
        return None
    std = float(np.std(r, ddof=1))
    if std < _DEGENERATE:
        return None
    mean = float(np.mean(r))
    sr = mean / std
    centered = r - mean
    m2 = float(np.mean(centered**2))
    if m2 <= 0.0:
        skew, kurtosis = 0.0, 3.0
    else:
        skew = float(np.mean(centered**3)) / m2**1.5
        kurtosis = float(np.mean(centered**4)) / m2**2
    return probabilistic_sharpe_from_moments(sr, sr0, len(r), skew, kurtosis)


def expected_max_sharpe(n_trials: int, trial_sr_var: float) -> float:
    """Expected maximum per-period Sharpe of ``n_trials`` independent trials.

    ``sqrt(V) * ((1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N * e)))`` with the
    Euler-Mascheroni constant g; 0.0 when there is a single trial.
    """
    if n_trials <= 1:
        return 0.0
    z_first = _ND.inv_cdf(1.0 - 1.0 / n_trials)
    z_last = _ND.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(trial_sr_var) * (
        (1.0 - _EULER_MASCHERONI) * z_first + _EULER_MASCHERONI * z_last
    )


def deflated_sharpe(
    returns: Sequence[float] | np.ndarray, n_trials: int, trial_sr_var: float
) -> float | None:
    """PSR against the expected maximum Sharpe of ``n_trials`` trials (the DSR)."""
    return probabilistic_sharpe(returns, expected_max_sharpe(n_trials, trial_sr_var))


def _combo_sharpe(sums: np.ndarray, sumsqs: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Per-config Sharpe from sums, squared sums and counts (sample variance, ddof=1)."""
    mean = sums / counts
    var = (sumsqs - counts * mean**2) / np.maximum(counts - 1.0, 1.0)
    np.maximum(var, 0.0, out=var)
    safe_var = np.where(var > 0.0, var, 1.0)
    return np.where(var > 0.0, mean / np.sqrt(safe_var), 0.0)


def pbo_cscv(matrix: np.ndarray | Sequence[Sequence[float]], blocks: int = BLOCKS) -> float | None:
    """Probability of backtest overfitting by CSCV rank-logit.

    ``matrix`` is T days x N configs of per-day returns. The rows are split into ``blocks``
    contiguous groups; every choice of half the blocks as in-sample ranks the configs by
    Sharpe, and the in-sample best is re-ranked (1 = worst, average ranks on ties) on the
    remaining out-of-sample blocks. With ``w = rank / (N + 1)`` and
    ``lambda = ln(w / (1 - w))``, PBO is the fraction of combinations with
    ``lambda <= 0`` — the in-sample best landing in the bottom half out-of-sample.
    None when there are fewer than 2 configs or fewer than ``2 * blocks`` rows.
    """
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim != 2:
        msg = "pbo matrix must be 2-dimensional (days x configs)"
        raise ValueError(msg)
    t_days, n_configs = m.shape
    if n_configs < 2 or t_days < 2 * blocks:
        return None
    parts = np.array_split(np.arange(t_days), blocks)
    sums = np.stack([m[part].sum(axis=0) for part in parts])
    sumsqs = np.stack([(m[part] ** 2).sum(axis=0) for part in parts])
    counts = np.array([len(part) for part in parts], dtype=np.float64)
    combos = np.array(list(itertools.combinations(range(blocks), blocks // 2)), dtype=np.int64)
    total_sum = sums.sum(axis=0)
    total_sumsq = sumsqs.sum(axis=0)
    overfit = 0
    # shortcut: chunked over combinations so a 500-config matrix stays well under 100 MB
    chunk = max(1, 2_000_000 // max(n_configs, 1))
    for start in range(0, len(combos), chunk):
        selected = combos[start : start + chunk]
        is_count = counts[selected].sum(axis=1, keepdims=True)
        is_sr = _combo_sharpe(sums[selected].sum(axis=1), sumsqs[selected].sum(axis=1), is_count)
        oos_sr = _combo_sharpe(
            total_sum - sums[selected].sum(axis=1),
            total_sumsq - sumsqs[selected].sum(axis=1),
            t_days - is_count,
        )
        best = np.argmax(is_sr, axis=1)
        picked = np.take_along_axis(oos_sr, best[:, None], axis=1)
        below = (oos_sr < picked).sum(axis=1)
        at_or_below = (oos_sr <= picked).sum(axis=1)
        rank = (below + at_or_below + 1.0) / 2.0
        w = rank / (n_configs + 1.0)
        lam = np.log(w / (1.0 - w))
        overfit += int(np.count_nonzero(lam <= 0.0))
    return overfit / len(combos)
