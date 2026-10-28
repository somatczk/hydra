"""Honest training of the up/down classifier.

The objective is classification on the sign of the forward return (rows whose
target is zero or undefined are dropped); ``predict_proba`` is the confidence the
serving overlay consumes. Evaluation is walk-forward with an embargo of one
horizon between each train end and test start, and it reports only what the data
can honestly support: per-fold OOS accuracy next to the OOS base rate, and the
mean signed forward return of trading the predicted direction. The ML library
imports are lazy so the module imports without the ml-training extra installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy import ndarray

from hydra.ml.features import (
    FeatureMatrix,
    FeatureSpec,
    reference_deciles,
)

DEFAULT_PARAMS: dict[str, object] = {
    "max_depth": 6,
    "learning_rate": 0.1,
    "n_estimators": 100,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "random_state": 42,
}
"""The XGBClassifier defaults; callers may override any of them."""


@runtime_checkable
class SupportsPredict(Protocol):
    """The fitted-model contract: class predictions on a feature matrix."""

    def predict(self, x: Any) -> Any: ...


@dataclass
class TrainedModel:
    """A fitted classifier, its honest OOS metrics and the spec it was trained on."""

    model: SupportsPredict
    model_type: str
    metrics: dict[str, Any]
    feature_spec: FeatureSpec
    train_timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    params: dict[str, object] = field(default_factory=dict)

    @property
    def feature_names(self) -> list[str]:
        return list(self.feature_spec.feature_names)


# ---------------------------------------------------------------------------
# Walk-forward split with embargo
# ---------------------------------------------------------------------------


def _walk_forward_split(
    n_samples: int,
    n_splits: int = 5,
    min_train_size: int = 100,
    embargo: int = 0,
) -> list[tuple[ndarray, ndarray]]:
    """Expanding-window train/test splits with ``embargo`` quarantined rows.

    Each fold trains on everything up to a cutoff and tests on the next block,
    skipping ``embargo`` rows after the cutoff so no test row's forward-return
    window overlaps a train row's.
    """
    if embargo < 0:
        msg = f"embargo must be >= 0, got {embargo}"
        raise ValueError(msg)
    if n_samples < min_train_size + n_splits:
        cut = int(n_samples * 0.8)
        train_idx = np.arange(cut)
        test_idx = np.arange(cut + embargo, n_samples)
        return [(train_idx, test_idx)] if cut > 0 and len(test_idx) > 0 else []

    test_size = (n_samples - min_train_size) // n_splits
    splits: list[tuple[ndarray, ndarray]] = []
    for i in range(n_splits):
        train_end = min_train_size + i * test_size
        test_start = train_end + embargo
        test_end = min(train_end + test_size, n_samples)
        if train_end >= n_samples or test_start >= test_end:
            break
        splits.append((np.arange(train_end), np.arange(test_start, test_end)))
    return splits


# ---------------------------------------------------------------------------
# Honest evaluation
# ---------------------------------------------------------------------------


def _evaluate_predictions(
    y_true: ndarray,
    y_pred: ndarray,
    forward_returns: ndarray,
) -> dict[str, float]:
    """OOS sign accuracy next to the base rate, and the mean signed forward return."""
    if len(y_true) == 0:
        return {"accuracy": 0.0, "base_rate": 0.0, "mean_signed_return": 0.0}
    return {
        "accuracy": float(np.mean(y_true == y_pred)),
        "base_rate": float(np.mean(y_true == 1.0)),
        "mean_signed_return": float(np.mean(y_pred * forward_returns)),
    }


def _summarize_folds(fold_metrics: list[dict[str, float]], embargo: int) -> dict[str, Any]:
    """Average the per-fold metrics and keep the per-fold values."""
    if not fold_metrics:
        return {"folds": 0, "embargo": embargo}
    return {
        "folds": len(fold_metrics),
        "embargo": embargo,
        "oos_accuracy": float(np.mean([m["accuracy"] for m in fold_metrics])),
        "oos_base_rate": float(np.mean([m["base_rate"] for m in fold_metrics])),
        "oos_mean_signed_return": float(np.mean([m["mean_signed_return"] for m in fold_metrics])),
        "fold_accuracy": [m["accuracy"] for m in fold_metrics],
        "fold_base_rate": [m["base_rate"] for m in fold_metrics],
        "fold_mean_signed_return": [m["mean_signed_return"] for m in fold_metrics],
    }


# ---------------------------------------------------------------------------
# ModelTrainer
# ---------------------------------------------------------------------------


class ModelTrainer:
    """Walk-forward trainer of the up/down classifier."""

    def train_xgboost(
        self,
        features: FeatureMatrix,
        params: dict[str, object] | None = None,
    ) -> TrainedModel:
        """Train an XGBClassifier on the up/down target with embargoed walk-forward OOS metrics.

        ``predict_proba`` on the returned model is the confidence in the up
        class; the OOS metrics come from the folds only, never from the final
        in-sample fit.
        """
        import xgboost as xgb

        if features.target is None or features.forward_returns is None:
            msg = (
                "training needs a matrix with target and forward_returns "
                "(build it via the DatasetBuilder)"
            )
            raise ValueError(msg)

        merged_params = {**DEFAULT_PARAMS, **(params or {})}
        rows = np.flatnonzero((features.target == 1.0) | (features.target == -1.0))
        if len(rows) == 0:
            msg = "the target has no up/down rows to train on"
            raise ValueError(msg)

        feat = features.features[rows]
        signs = features.target[rows]
        forward = features.forward_returns[rows]
        y = (signs + 1.0) / 2.0  # classes {0, 1}; 1 = up
        if np.unique(y).size < 2:
            msg = "the target has a single class; nothing to classify"
            raise ValueError(msg)

        embargo = features.spec.horizon
        splits = _walk_forward_split(len(y), embargo=embargo)
        model = xgb.XGBClassifier(**merged_params)

        fold_metrics: list[dict[str, float]] = []
        for train_idx, test_idx in splits:
            model.fit(feat[train_idx], y[train_idx])
            preds = np.asarray(model.predict(feat[test_idx]), dtype=np.float64)
            pred_signs = 2.0 * preds - 1.0
            fold_metrics.append(
                _evaluate_predictions(signs[test_idx], pred_signs, forward[test_idx])
            )

        model.fit(feat, y)
        trained_spec = replace(
            features.spec,
            reference_quantiles=tuple(reference_deciles(feat[:, i]) for i in range(feat.shape[1])),
        )
        return TrainedModel(
            model=model,
            model_type="xgboost",
            metrics=_summarize_folds(fold_metrics, embargo),
            feature_spec=trained_spec,
            params=dict(merged_params),
        )
