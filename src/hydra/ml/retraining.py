"""Drift detection and champion-challenger evaluation.

The PSI fix (SM-13): the bins are the reference deciles — the reference
quantiles stored on the registry row — with current values clipped to the edge
bins, so a shifted distribution reads high no matter how extreme its outliers
are (the old combined-range bins stretched and read 0.066 for a 1-sigma shift).
The NaN rate of the current window is a separate score, never conflated with
the PSI: a feature that is entirely NaN is flagged on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy import ndarray

from hydra.ml.features import FeatureMatrix, reference_deciles
from hydra.ml.training import SupportsPredict, TrainedModel, _evaluate_predictions

# ---------------------------------------------------------------------------
# DriftResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    """Result of a drift check across features."""

    psi_scores: dict[str, float]
    nan_rates: dict[str, float]
    max_psi: float
    drifted_features: list[str]
    action: str  # "none", "alert", "retrain"


# ---------------------------------------------------------------------------
# PSI computation
# ---------------------------------------------------------------------------

PSI_ALERT_THRESHOLD = 0.2
PSI_RETRAIN_THRESHOLD = 0.25
"""Shared thresholds: P5-D's alert consumes them; promotion stays a human button."""


@dataclass(frozen=True)
class FeaturePsi:
    """One feature's drift scores: the quantile-PSI and the current NaN rate."""

    psi: float
    nan_rate: float


def _nan_rate(current: ndarray) -> float:
    """The fraction of NaN values in the current window."""
    if len(current) == 0:
        return 0.0
    return float(np.mean(np.isnan(current)))


def _quantile_psi(current: ndarray, edges: ndarray) -> float:
    """PSI of the current values against equal-population reference decile bins.

    Values outside the reference range clip into the edge bins instead of
    stretching the bin edges (the SM-13 bug).
    """
    eps = 1e-8
    cur = current[~np.isnan(current)]
    if len(cur) == 0:
        return 0.0
    n_bins = len(edges) + 1
    idx = np.clip(np.searchsorted(edges, cur, side="right"), 0, n_bins - 1)
    counts = np.bincount(idx, minlength=n_bins).astype(np.float64)
    cur_pct = counts / len(cur) + eps
    ref_pct = np.full(n_bins, 1.0 / n_bins) + eps
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def feature_psi(current: ndarray, reference_quantiles: tuple[float, ...] | None) -> FeaturePsi:
    """Score one feature's current window against its stored reference deciles."""
    rate = _nan_rate(current)
    if reference_quantiles is None:
        return FeaturePsi(psi=0.0, nan_rate=rate)
    return FeaturePsi(
        psi=_quantile_psi(current, np.asarray(reference_quantiles, dtype=np.float64)),
        nan_rate=rate,
    )


def _compute_psi(reference: ndarray, current: ndarray) -> FeaturePsi:
    """The reference-matrix convenience: deciles from the reference column."""
    return feature_psi(current, reference_deciles(reference))


# ---------------------------------------------------------------------------
# RetrainingPipeline
# ---------------------------------------------------------------------------


class RetrainingPipeline:
    """Drift detection and champion-challenger evaluation."""

    def check_drift(
        self,
        current_features: ndarray,
        reference_features: ndarray,
        feature_names: list[str] | None = None,
    ) -> DriftResult:
        """Check feature drift between current and reference distributions.

        A feature is drifted when its PSI exceeds the alert threshold or its
        current window is entirely NaN; the action is "retrain" past the
        retrain threshold, "alert" at the alert threshold or on an all-NaN
        feature, never "none" while a flag is set.
        """
        n_features = current_features.shape[1] if current_features.ndim > 1 else 1
        if feature_names is None:
            feature_names = [f"feature_{i}" for i in range(n_features)]

        psi_scores: dict[str, float] = {}
        nan_rates: dict[str, float] = {}
        drifted_features: list[str] = []
        for i, name in enumerate(feature_names):
            ref_col = (
                reference_features[:, i] if reference_features.ndim > 1 else reference_features
            )
            cur_col = current_features[:, i] if current_features.ndim > 1 else current_features
            score = _compute_psi(ref_col, cur_col)
            psi_scores[name] = score.psi
            nan_rates[name] = score.nan_rate
            if score.psi > PSI_ALERT_THRESHOLD or score.nan_rate >= 1.0:
                drifted_features.append(name)

        max_psi = max(psi_scores.values()) if psi_scores else 0.0
        any_all_nan = any(rate >= 1.0 for rate in nan_rates.values())
        if max_psi > PSI_RETRAIN_THRESHOLD:
            action = "retrain"
        elif max_psi > PSI_ALERT_THRESHOLD or any_all_nan:
            action = "alert"
        else:
            action = "none"

        return DriftResult(
            psi_scores=psi_scores,
            nan_rates=nan_rates,
            max_psi=max_psi,
            drifted_features=drifted_features,
            action=action,
        )

    def champion_challenger(
        self,
        champion: TrainedModel,
        challenger: TrainedModel,
        test: FeatureMatrix,
    ) -> str:
        """The higher OOS mean signed forward return wins; the champion keeps the crown on a tie."""
        if test.target is None or test.forward_returns is None:
            msg = "champion-challenger needs a matrix with target and forward_returns"
            raise ValueError(msg)
        rows = np.flatnonzero((test.target == 1.0) | (test.target == -1.0))
        if len(rows) == 0:
            msg = "the test matrix has no up/down rows to evaluate on"
            raise ValueError(msg)

        feat = test.features[rows]
        signs = test.target[rows]
        forward = test.forward_returns[rows]

        def oos_mean_signed_return(model: SupportsPredict) -> float:
            preds = np.asarray(model.predict(feat), dtype=np.float64)
            pred_signs = 2.0 * preds - 1.0
            return _evaluate_predictions(signs, pred_signs, forward)["mean_signed_return"]

        challenger_return = oos_mean_signed_return(challenger.model)
        champion_return = oos_mean_signed_return(champion.model)
        return "challenger" if challenger_return > champion_return else "champion"
