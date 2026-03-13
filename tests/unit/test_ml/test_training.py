"""Tests for hydra.ml.training -- model training module.

All ML libraries (xgboost, lightgbm, sklearn, optuna) are mocked so tests
run without them installed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hydra.ml.features import FeatureMatrix
from hydra.ml.training import (
    ModelTrainer,
    TrainedModel,
    _evaluate_predictions,
    _walk_forward_split,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def feature_matrix() -> FeatureMatrix:
    """Create a simple feature matrix for testing."""
    rng = np.random.default_rng(42)
    n_samples = 200
    n_features = 5
    return FeatureMatrix(
        features=rng.standard_normal((n_samples, n_features)),
        feature_names=[f"f{i}" for i in range(n_features)],
        timestamps=[],
        target=None,
    )


@pytest.fixture()
def target() -> np.ndarray:
    rng = np.random.default_rng(42)
    return np.sign(rng.standard_normal(200))


# ---------------------------------------------------------------------------
# Walk-forward split tests
# ---------------------------------------------------------------------------


class TestWalkForwardSplit:
    def test_produces_splits(self) -> None:
        splits = _walk_forward_split(500)
        assert len(splits) > 0
        for train_idx, test_idx in splits:
            assert len(train_idx) > 0
            assert len(test_idx) > 0

    def test_no_overlap(self) -> None:
        splits = _walk_forward_split(500)
        for train_idx, test_idx in splits:
            assert len(np.intersect1d(train_idx, test_idx)) == 0

    def test_expanding_window(self) -> None:
        splits = _walk_forward_split(500)
        if len(splits) > 1:
            assert len(splits[1][0]) > len(splits[0][0])

    def test_small_dataset_fallback(self) -> None:
        splits = _walk_forward_split(50, min_train_size=100)
        assert len(splits) == 1


# ---------------------------------------------------------------------------
# Evaluation metrics tests
# ---------------------------------------------------------------------------


class TestEvaluatePredictions:
    def test_perfect_predictions(self) -> None:
        y = np.array([1.0, -1.0, 1.0, -1.0])
        metrics = _evaluate_predictions(y, y)
        assert metrics["accuracy"] == 1.0

    def test_empty_arrays(self) -> None:
        metrics = _evaluate_predictions(np.array([]), np.array([]))
        assert metrics["accuracy"] == 0.0

    def test_metrics_keys(self) -> None:
        y = np.array([1.0, -1.0])
        metrics = _evaluate_predictions(y, y)
        assert "accuracy" in metrics
        assert "sharpe" in metrics
        assert "mean_return" in metrics


# ---------------------------------------------------------------------------
# XGBoost training (mocked)
# ---------------------------------------------------------------------------


class TestTrainXGBoost:
    def test_returns_trained_model(self, feature_matrix: FeatureMatrix, target: np.ndarray) -> None:
        mock_model = MagicMock()
        mock_model.fit = MagicMock()
        mock_model.predict = MagicMock(
            side_effect=lambda x: np.sign(np.random.default_rng(0).standard_normal(len(x)))
        )

        mock_xgb = MagicMock()
        mock_xgb.XGBRegressor = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"xgboost": mock_xgb}):
            trainer = ModelTrainer()
            result = trainer.train_xgboost(feature_matrix, target)

        assert isinstance(result, TrainedModel)
        assert result.model_type == "xgboost"
        assert isinstance(result.metrics, dict)
        assert isinstance(result.feature_names, list)
        assert len(result.feature_names) == 5
        assert result.train_timestamp is not None
        assert isinstance(result.params, dict)

    def test_custom_params(self, feature_matrix: FeatureMatrix, target: np.ndarray) -> None:
        mock_model = MagicMock()
        mock_model.predict = MagicMock(side_effect=lambda x: np.zeros(len(x)))

        mock_xgb = MagicMock()
        mock_xgb.XGBRegressor = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"xgboost": mock_xgb}):
            trainer = ModelTrainer()
            result = trainer.train_xgboost(feature_matrix, target, params={"max_depth": 3})

        assert result.params["max_depth"] == 3

    def test_metrics_populated(self, feature_matrix: FeatureMatrix, target: np.ndarray) -> None:
        mock_model = MagicMock()
        mock_model.predict = MagicMock(side_effect=lambda x: np.ones(len(x)))

        mock_xgb = MagicMock()
        mock_xgb.XGBRegressor = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"xgboost": mock_xgb}):
            trainer = ModelTrainer()
            result = trainer.train_xgboost(feature_matrix, target)

        assert "accuracy" in result.metrics
        assert "sharpe" in result.metrics
        assert "mean_return" in result.metrics


# ---------------------------------------------------------------------------
# LightGBM training (mocked)
# ---------------------------------------------------------------------------


class TestTrainLightGBM:
    def test_returns_trained_model(self, feature_matrix: FeatureMatrix, target: np.ndarray) -> None:
        mock_model = MagicMock()
        mock_model.predict = MagicMock(
            side_effect=lambda x: np.sign(np.random.default_rng(0).standard_normal(len(x)))
        )

        mock_lgb = MagicMock()
        mock_lgb.LGBMRegressor = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"lightgbm": mock_lgb}):
            trainer = ModelTrainer()
            result = trainer.train_lightgbm(feature_matrix, target)

        assert isinstance(result, TrainedModel)
        assert result.model_type == "lightgbm"
        assert isinstance(result.metrics, dict)
        assert isinstance(result.feature_names, list)


# ---------------------------------------------------------------------------
# Hyperparameter search (mocked)
# ---------------------------------------------------------------------------


class TestHyperparameterSearch:
    def test_returns_best_params(self, feature_matrix: FeatureMatrix, target: np.ndarray) -> None:
        mock_model = MagicMock()
        mock_model.predict = MagicMock(side_effect=lambda x: np.ones(len(x)))

        mock_xgb = MagicMock()
        mock_xgb.XGBRegressor = MagicMock(return_value=mock_model)

        # Build a real-ish Optuna mock
        mock_trial = MagicMock()
        mock_trial.suggest_int = MagicMock(return_value=5)
        mock_trial.suggest_float = MagicMock(return_value=0.1)

        mock_study = MagicMock()
        mock_study.best_params = {"max_depth": 5, "learning_rate": 0.1}

        def fake_optimize(objective, n_trials):
            # Call objective once to exercise the code path
            objective(mock_trial)

        mock_study.optimize = MagicMock(side_effect=fake_optimize)

        mock_optuna = MagicMock()
        mock_optuna.create_study = MagicMock(return_value=mock_study)
        mock_optuna.logging = MagicMock()
        mock_optuna.logging.WARNING = 30
        mock_optuna.Trial = MagicMock

        with patch.dict(
            "sys.modules",
            {"xgboost": mock_xgb, "optuna": mock_optuna},
        ):
            trainer = ModelTrainer()
            best_params = trainer.hyperparameter_search(
                "xgboost", feature_matrix, target, n_trials=1
            )

        assert isinstance(best_params, dict)
        assert "max_depth" in best_params
        assert "learning_rate" in best_params
