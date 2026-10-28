"""Tests for hydra.ml.training -- honest walk-forward training of the classifier.

XGBoost is mocked (CI installs only the dev extra); the synthetic dataset's
feature column 0 carries each row's true class, so the mocks behave like real
lookup models and every asserted number is computable by hand.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hydra.ml.features import DEFAULT_FEATURE_SPEC, FeatureMatrix, _all_feature_names
from hydra.ml.training import (
    ModelTrainer,
    TrainedModel,
    _evaluate_predictions,
    _walk_forward_split,
)

N_BARS = 256  # 255 valid rows at horizon 1 -> five even 30-row test folds
FEATURE_NAMES = [f"f{i}" for i in range(4)]


def _matrix_from_steps(steps: list[float], horizon: int) -> FeatureMatrix:
    """A matrix from multiplicative price steps; column 0 carries each row's
    true class ({0, 1}; 1 = up), column 1 its row index, so a mock model can
    act as a lookup table."""
    n = len(steps) + 1
    closes = np.empty(n, dtype=np.float64)
    closes[0] = 100.0
    for i, step in enumerate(steps):
        closes[i + 1] = closes[i] * (1.0 + step)

    signs = np.full(n, np.nan)
    fwd = np.full(n, np.nan)
    for i in range(n - horizon):
        fwd[i] = closes[i + horizon] / closes[i] - 1.0
        signs[i] = np.sign(fwd[i])

    features = np.zeros((n, len(FEATURE_NAMES)), dtype=np.float64)
    features[:, 0] = (signs + 1.0) / 2.0  # NaN rows get a sentinel the trainer drops
    features[:, 1] = np.arange(n, dtype=np.float64)
    features[:, 2] = 0.5
    features[:, 3] = -0.5

    spec = replace(
        DEFAULT_FEATURE_SPEC,
        spec_version=3,
        horizon=horizon,
        feature_names=tuple(FEATURE_NAMES),
    )
    return FeatureMatrix(
        features=features,
        feature_names=list(FEATURE_NAMES),
        timestamps=[datetime(2026, 1, 1, tzinfo=UTC)] * n,
        spec=spec,
        target=signs,
        forward_returns=fwd,
    )


def _alternating_matrix(horizon: int = 1, n: int = N_BARS) -> FeatureMatrix:
    """Prices stepping +1%/-1% alternately: forward returns are exactly +/-0.01.

    At horizon 1 the signs alternate perfectly (a balanced 50/50 target); the
    4-step product is negative, so larger horizons use seeded random steps
    instead (``_random_matrix``).
    """
    steps = [0.01 if i % 2 == 0 else -0.01 for i in range(n - 1)]
    return _matrix_from_steps(steps, horizon)


def _random_matrix(horizon: int, n: int = N_BARS, seed: int = 11) -> FeatureMatrix:
    """Seeded random +/-1% steps: both classes at any horizon."""
    rng = np.random.default_rng(seed)
    steps = [float(s) for s in rng.choice([0.01, -0.01], size=n - 1)]
    return _matrix_from_steps(steps, horizon)


def _mock_xgb_module(model: MagicMock) -> MagicMock:
    mock_xgb = MagicMock()
    mock_xgb.XGBClassifier = MagicMock(return_value=model)
    return mock_xgb


def _lookup_model() -> MagicMock:
    """A mock model that predicts column 0: the true class of every row."""
    model = MagicMock()
    model.fit = MagicMock()
    model.predict = MagicMock(side_effect=lambda x: np.asarray(x)[:, 0].copy())
    return model


def _constant_up_model() -> MagicMock:
    """A mock model that always predicts the up class (1)."""
    model = MagicMock()
    model.fit = MagicMock()
    model.predict = MagicMock(side_effect=lambda x: np.ones(len(x)))
    return model


# ---------------------------------------------------------------------------
# Walk-forward split with embargo
# ---------------------------------------------------------------------------


class TestWalkForwardSplit:
    def test_produces_expanding_splits_without_overlap(self) -> None:
        splits = _walk_forward_split(500, embargo=4)
        assert len(splits) == 5
        for train_idx, test_idx in splits:
            assert len(train_idx) > 0 and len(test_idx) > 0
            assert len(np.intersect1d(train_idx, test_idx)) == 0
        assert len(splits[1][0]) > len(splits[0][0])

    def test_embargo_quarantines_rows_between_train_and_test(self) -> None:
        splits = _walk_forward_split(500, embargo=4)
        for train_idx, test_idx in splits:
            assert int(test_idx[0]) == int(train_idx[-1]) + 1 + 4

    def test_zero_embargo_keeps_folds_contiguous(self) -> None:
        for train_idx, test_idx in _walk_forward_split(500, embargo=0):
            assert int(test_idx[0]) == int(train_idx[-1]) + 1

    def test_small_dataset_falls_back_to_one_embargoed_split(self) -> None:
        splits = _walk_forward_split(50, min_train_size=100, embargo=2)
        assert len(splits) == 1
        train_idx, test_idx = splits[0]
        assert int(train_idx[-1]) == 39
        assert int(test_idx[0]) == 42

    def test_negative_embargo_is_refused(self) -> None:
        with pytest.raises(ValueError, match="embargo"):
            _walk_forward_split(500, embargo=-1)


# ---------------------------------------------------------------------------
# Honest evaluation
# ---------------------------------------------------------------------------


class TestEvaluatePredictions:
    def test_perfect_predictions(self) -> None:
        y = np.array([1.0, -1.0, 1.0, -1.0])
        fwd = np.array([0.01, -0.01, 0.02, -0.02])
        metrics = _evaluate_predictions(y, y, fwd)
        assert metrics["accuracy"] == 1.0
        assert metrics["base_rate"] == 0.5
        assert metrics["mean_signed_return"] == pytest.approx(0.015)

    def test_constant_up_predictor_matches_the_base_rate(self) -> None:
        y = np.array([1.0, -1.0, -1.0, 1.0, 1.0])
        fwd = np.array([0.01, -0.01, -0.02, 0.02, 0.01])
        metrics = _evaluate_predictions(y, np.ones(5), fwd)
        assert metrics["accuracy"] == metrics["base_rate"] == 0.6
        assert metrics["mean_signed_return"] == pytest.approx(0.01 / 5)

    def test_empty_arrays(self) -> None:
        metrics = _evaluate_predictions(np.array([]), np.array([]), np.array([]))
        assert metrics == {"accuracy": 0.0, "base_rate": 0.0, "mean_signed_return": 0.0}

    def test_no_fake_sharpe(self) -> None:
        y = np.array([1.0, -1.0])
        assert "sharpe" not in _evaluate_predictions(y, y, np.array([0.01, -0.01]))


# ---------------------------------------------------------------------------
# Training (XGBoost mocked)
# ---------------------------------------------------------------------------


class TestTrainXGBoost:
    def test_perfect_predictor_scores_full_accuracy_and_positive_return(
        self,
    ) -> None:
        model = _lookup_model()
        with patch.dict("sys.modules", {"xgboost": _mock_xgb_module(model)}):
            result = ModelTrainer().train_xgboost(_alternating_matrix())

        assert isinstance(result, TrainedModel)
        assert result.model_type == "xgboost"
        assert result.model is model
        assert result.feature_names == FEATURE_NAMES
        # every OOS fold is perfect: accuracy 1.0, mean signed return = mean |fwd| = 1%
        assert result.metrics["folds"] == 5
        assert result.metrics["oos_accuracy"] == 1.0
        assert result.metrics["fold_accuracy"] == [1.0] * 5
        assert result.metrics["oos_mean_signed_return"] == pytest.approx(0.01, abs=1e-12)
        assert result.metrics["oos_base_rate"] == 0.5
        # the embargo equals the horizon and there is no fake sharpe anywhere
        assert result.metrics["embargo"] == 1
        assert "sharpe" not in result.metrics

    def test_constant_predictor_is_the_base_rate_with_zero_return(self) -> None:
        model = _constant_up_model()
        with patch.dict("sys.modules", {"xgboost": _mock_xgb_module(model)}):
            result = ModelTrainer().train_xgboost(_alternating_matrix())

        assert result.metrics["oos_accuracy"] == 0.5
        assert result.metrics["oos_base_rate"] == 0.5
        assert result.metrics["fold_accuracy"] == [0.5] * 5
        # always trading up on a balanced +/-1% dataset earns exactly nothing
        assert result.metrics["oos_mean_signed_return"] == pytest.approx(0.0, abs=1e-12)
        assert "sharpe" not in result.metrics

    def test_embargo_is_the_horizon_between_train_end_and_test_start(self) -> None:
        """The rows the mock sees prove the embargo flows from the spec's horizon."""
        model = _lookup_model()
        with patch.dict("sys.modules", {"xgboost": _mock_xgb_module(model)}):
            result = ModelTrainer().train_xgboost(_random_matrix(horizon=4))

        assert result.metrics["embargo"] == 4
        fit_calls = [c.args[0] for c in model.fit.call_args_list]
        predict_calls = [c.args[0] for c in model.predict.call_args_list]
        # the final fit is on all valid rows; fold fits precede fold predicts
        assert len(fit_calls) == 6
        assert len(predict_calls) == 5
        for fold in range(5):
            train_rows = fit_calls[fold][:, 1]
            test_rows = predict_calls[fold][:, 1]
            assert int(test_rows[0]) == int(train_rows[-1]) + 1 + 4

    def test_zero_and_undefined_targets_are_dropped_from_training(self) -> None:
        matrix = _alternating_matrix()
        # rows 10 and 11: a zero forward return (flat) and a NaN target (no forward bar)
        matrix.target[10] = 0.0
        matrix.forward_returns[10] = 0.0
        matrix.target[11] = np.nan
        matrix.forward_returns[11] = np.nan
        model = _lookup_model()
        with patch.dict("sys.modules", {"xgboost": _mock_xgb_module(model)}):
            result = ModelTrainer().train_xgboost(matrix)

        seen: set[float] = set()
        for call in [*model.fit.call_args_list, *model.predict.call_args_list]:
            seen.update(float(i) for i in call.args[0][:, 1])
        assert 10.0 not in seen and 11.0 not in seen
        assert result.metrics["folds"] == 5

    def test_reference_quantiles_are_filled_into_the_trained_spec(self) -> None:
        matrix = _alternating_matrix()
        model = _lookup_model()
        with patch.dict("sys.modules", {"xgboost": _mock_xgb_module(model)}):
            result = ModelTrainer().train_xgboost(matrix)

        quantiles = result.feature_spec.reference_quantiles
        assert quantiles is not None
        assert len(quantiles) == len(FEATURE_NAMES)
        # column 2 is constant 0.5: its deciles all collapse to 0.5
        assert quantiles[2] == (0.5,) * 9
        # the spec the model was trained on keeps its identity otherwise
        assert result.feature_spec.spec_version == matrix.spec.spec_version
        assert result.feature_spec.horizon == matrix.spec.horizon
        assert result.feature_spec.to_dict()["reference_quantiles"][2] == [0.5] * 9

    def test_custom_params_are_passed_through(self) -> None:
        model = MagicMock()
        model.predict = MagicMock(side_effect=lambda x: np.ones(len(x)))
        mock_xgb = _mock_xgb_module(model)
        with patch.dict("sys.modules", {"xgboost": mock_xgb}):
            result = ModelTrainer().train_xgboost(_alternating_matrix(), params={"max_depth": 3})
        assert result.params["max_depth"] == 3
        assert mock_xgb.XGBClassifier.call_args.kwargs["max_depth"] == 3

    def test_missing_target_is_refused(self) -> None:
        matrix = _alternating_matrix()
        broken = FeatureMatrix(
            features=matrix.features,
            feature_names=matrix.feature_names,
            timestamps=matrix.timestamps,
            spec=matrix.spec,
        )
        with (
            patch.dict("sys.modules", {"xgboost": _mock_xgb_module(MagicMock())}),
            pytest.raises(ValueError, match="target and forward_returns"),
        ):
            ModelTrainer().train_xgboost(broken)

    def test_single_class_target_is_refused(self) -> None:
        matrix = _alternating_matrix()
        matrix.target[:] = np.where(np.isnan(matrix.target), np.nan, 1.0)
        with (
            patch.dict("sys.modules", {"xgboost": _mock_xgb_module(MagicMock())}),
            pytest.raises(ValueError, match="single class"),
        ):
            ModelTrainer().train_xgboost(matrix)

    def test_all_flat_target_is_refused(self) -> None:
        matrix = _alternating_matrix()
        valid = ~np.isnan(matrix.target)
        matrix.target[valid] = 0.0
        with (
            patch.dict("sys.modules", {"xgboost": _mock_xgb_module(MagicMock())}),
            pytest.raises(ValueError, match="no up/down rows"),
        ):
            ModelTrainer().train_xgboost(matrix)

    def test_default_spec_names_are_pinned_by_the_matrix(self) -> None:
        """A spec whose names drift from the matrix is a training-time failure."""
        assert len(DEFAULT_FEATURE_SPEC.feature_names) == 67
        assert DEFAULT_FEATURE_SPEC.feature_names == tuple(_all_feature_names())


# ---------------------------------------------------------------------------
# The folds on the alternating dataset (real split numbers)
# ---------------------------------------------------------------------------


class TestAlternatingDatasetFolds:
    def test_five_even_folds_on_255_rows(self) -> None:
        splits = _walk_forward_split(255, embargo=1)
        assert len(splits) == 5
        assert [len(test) for _, test in splits] == [30, 30, 30, 30, 30]
        # every fold is exactly balanced: 15 up, 15 down steps of 1%
        for _, test_idx in splits:
            assert len(test_idx) % 2 == 0
