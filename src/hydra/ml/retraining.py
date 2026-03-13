"""Drift detection and automated retraining pipeline.

Monitors feature distributions via Population Stability Index (PSI) and
triggers retraining when drift is detected.  Includes champion-challenger
evaluation for safe model replacement.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy import ndarray

from hydra.ml.features import FeatureMatrix
from hydra.ml.training import ModelTrainer, TrainedModel, _evaluate_predictions

# ---------------------------------------------------------------------------
# DriftResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    """Result of a drift check across features."""

    psi_scores: dict[str, float]
    max_psi: float
    drifted_features: list[str]
    action: str  # "none", "alert", "retrain"


# ---------------------------------------------------------------------------
# PSI computation
# ---------------------------------------------------------------------------

_PSI_ALERT_THRESHOLD = 0.2
_PSI_RETRAIN_THRESHOLD = 0.25


def _compute_psi(
    reference: ndarray,
    current: ndarray,
    n_bins: int = 10,
) -> float:
    """Compute Population Stability Index between two 1-D distributions.

    Uses equal-width bins derived from the reference distribution.
    A small epsilon is added to avoid ``log(0)``.
    """
    eps = 1e-8

    ref_clean = reference[~np.isnan(reference)]
    cur_clean = current[~np.isnan(current)]

    if len(ref_clean) == 0 or len(cur_clean) == 0:
        return 0.0

    min_val = float(np.min(ref_clean))
    max_val = float(np.max(ref_clean))

    if min_val == max_val:
        # No variation in reference
        if len(cur_clean) == 0:
            return 0.0
        if np.all(cur_clean == min_val):
            return 0.0
        return 0.1

    # Build bin edges covering both distributions so no data falls outside
    combined_min = min(float(np.min(ref_clean)), float(np.min(cur_clean)))
    combined_max = max(float(np.max(ref_clean)), float(np.max(cur_clean)))
    margin = (combined_max - combined_min) * 0.01 + eps
    edges = np.linspace(combined_min - margin, combined_max + margin, n_bins + 1)

    ref_counts = np.histogram(ref_clean, bins=edges)[0].astype(np.float64)
    cur_counts = np.histogram(cur_clean, bins=edges)[0].astype(np.float64)

    ref_sum = ref_counts.sum()
    cur_sum = cur_counts.sum()
    if ref_sum == 0 or cur_sum == 0:
        return 0.0

    ref_pct = ref_counts / ref_sum + eps
    cur_pct = cur_counts / cur_sum + eps

    psi = float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))
    return psi


# ---------------------------------------------------------------------------
# RetrainingPipeline
# ---------------------------------------------------------------------------


class RetrainingPipeline:
    """Automated drift detection, retraining, and champion-challenger evaluation."""

    def __init__(self, trainer: ModelTrainer | None = None) -> None:
        self._trainer = trainer or ModelTrainer()

    def check_drift(
        self,
        current_features: ndarray,
        reference_features: ndarray,
        feature_names: list[str] | None = None,
    ) -> DriftResult:
        """Check feature drift between current and reference distributions.

        Parameters
        ----------
        current_features:
            Recent feature matrix ``(n_samples, n_features)``.
        reference_features:
            Training/reference feature matrix ``(n_samples, n_features)``.
        feature_names:
            Optional names for each feature column.

        Returns
        -------
        DriftResult
            PSI scores per feature and recommended action.
        """
        n_features = current_features.shape[1] if current_features.ndim > 1 else 1

        if feature_names is None:
            feature_names = [f"feature_{i}" for i in range(n_features)]

        psi_scores: dict[str, float] = {}
        drifted_features: list[str] = []

        for i, name in enumerate(feature_names):
            ref_col = (
                reference_features[:, i] if reference_features.ndim > 1 else reference_features
            )
            cur_col = current_features[:, i] if current_features.ndim > 1 else current_features
            psi = _compute_psi(ref_col, cur_col)
            psi_scores[name] = psi
            if psi > _PSI_ALERT_THRESHOLD:
                drifted_features.append(name)

        max_psi = max(psi_scores.values()) if psi_scores else 0.0

        if max_psi > _PSI_RETRAIN_THRESHOLD:
            action = "retrain"
        elif max_psi > _PSI_ALERT_THRESHOLD:
            action = "alert"
        else:
            action = "none"

        return DriftResult(
            psi_scores=psi_scores,
            max_psi=max_psi,
            drifted_features=drifted_features,
            action=action,
        )

    def schedule_retrain(
        self,
        model_name: str,
        features: FeatureMatrix,
        target: ndarray,
    ) -> TrainedModel:
        """Retrain a model on fresh data.

        Currently delegates to the XGBoost trainer.  The *model_name*
        parameter is reserved for future use with the model registry.
        """
        return self._trainer.train_xgboost(features, target)

    def champion_challenger(
        self,
        champion: TrainedModel,
        challenger: TrainedModel,
        test_features: FeatureMatrix,
        test_target: ndarray,
    ) -> str:
        """Evaluate champion vs challenger on held-out test data.

        Both models predict on *test_features* and are scored against
        *test_target*.  The model with a higher Sharpe ratio wins.

        Returns
        -------
        str
            ``"champion"`` or ``"challenger"``.
        """
        feat = test_features.features
        n_samples = min(len(feat), len(test_target))
        feat = feat[:n_samples]
        y = test_target[:n_samples]

        champion_preds = np.asarray(champion.model.predict(feat))  # type: ignore[union-attr]
        challenger_preds = np.asarray(challenger.model.predict(feat))  # type: ignore[union-attr]

        champion_metrics = _evaluate_predictions(y, champion_preds)
        challenger_metrics = _evaluate_predictions(y, challenger_preds)

        if challenger_metrics["sharpe"] > champion_metrics["sharpe"]:
            return "challenger"
        return "champion"
