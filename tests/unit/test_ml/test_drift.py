"""Tests for hydra.ml.retraining -- the quantile-PSI fix and champion-challenger."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from hydra.ml.features import DEFAULT_FEATURE_SPEC, FeatureMatrix, reference_deciles
from hydra.ml.retraining import (
    PSI_ALERT_THRESHOLD,
    PSI_RETRAIN_THRESHOLD,
    DriftResult,
    RetrainingPipeline,
    _compute_psi,
    feature_psi,
)
from hydra.ml.training import TrainedModel

# ---------------------------------------------------------------------------
# PSI calculation
# ---------------------------------------------------------------------------


class TestPSICalculation:
    def test_identical_distributions_psi_is_zero(self) -> None:
        rng = np.random.default_rng(42)
        data = rng.standard_normal(1000)
        score = _compute_psi(data, data.copy())
        assert score.psi == 0.0
        assert score.nan_rate == 0.0

    def test_sm13_repro_one_sigma_shift_with_outliers(self) -> None:
        """The exact shape the old combined-range bins read as 0.066.

        A 1-sigma shift plus 0.5% extreme outliers must read far above the
        retrain threshold: the outliers clip into the edge bins instead of
        stretching them.
        """
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        current = reference + 1.0
        current[:5] = 50.0  # 0.5% extreme outliers
        score = _compute_psi(reference, current)
        assert score.psi == pytest.approx(0.9853, abs=1e-3)
        assert score.psi > PSI_RETRAIN_THRESHOLD

    def test_zero_point_four_five_sigma_shift_lands_in_the_alert_band(self) -> None:
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        score = _compute_psi(reference, reference + 0.45)
        assert score.psi == pytest.approx(0.2251, abs=1e-3)
        assert PSI_ALERT_THRESHOLD < score.psi <= PSI_RETRAIN_THRESHOLD

    def test_thresholds_stay_shared_constants(self) -> None:
        assert (PSI_ALERT_THRESHOLD, PSI_RETRAIN_THRESHOLD) == (0.2, 0.25)

    def test_nan_rate_is_a_separate_score(self) -> None:
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        current = reference.copy()
        current[:100] = np.nan  # a 10% NaN window
        score = _compute_psi(reference, current)
        assert score.nan_rate == pytest.approx(0.1)
        # the PSI itself is computed on the remaining 90% and stays low
        assert score.psi < PSI_ALERT_THRESHOLD

    def test_all_nan_current_flags_never_none(self) -> None:
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        score = _compute_psi(reference, np.full(1000, np.nan))
        assert score.nan_rate == 1.0
        assert score.psi == 0.0

    def test_stored_quantiles_path_matches_the_matrix_path(self) -> None:
        """The registry-stored deciles are exactly the bins the PSI runs on."""
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        current = reference + 1.0
        current[:5] = 50.0
        current[100:137] = np.nan
        from_matrix = _compute_psi(reference, current)
        from_stored = feature_psi(current, reference_deciles(reference))
        assert from_stored.psi == from_matrix.psi
        assert from_stored.nan_rate == from_matrix.nan_rate

    def test_missing_reference_quantiles_score_zero_psi(self) -> None:
        """A feature with no stored reference cannot be scored; its NaN rate still is."""
        current = np.array([1.0, 2.0, np.nan, 4.0])
        score = feature_psi(current, None)
        assert score.psi == 0.0
        assert score.nan_rate == 0.25


# ---------------------------------------------------------------------------
# Drift check thresholds
# ---------------------------------------------------------------------------


class TestDriftThresholds:
    def test_no_drift_action_none(self) -> None:
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((500, 3))
        cur = rng.standard_normal((500, 3))
        result = RetrainingPipeline().check_drift(cur, ref, feature_names=["a", "b", "c"])
        assert isinstance(result, DriftResult)
        assert result.action == "none"
        assert result.max_psi < PSI_ALERT_THRESHOLD
        assert result.drifted_features == []
        assert all(rate == 0.0 for rate in result.nan_rates.values())

    def test_retrain_threshold(self) -> None:
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((1000, 1))
        cur = ref + 5.0
        result = RetrainingPipeline().check_drift(cur, ref, feature_names=["drifted"])
        assert result.max_psi > PSI_RETRAIN_THRESHOLD
        assert result.action == "retrain"
        assert result.drifted_features == ["drifted"]

    def test_alert_threshold(self) -> None:
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((1000, 1))
        cur = ref + 0.45
        result = RetrainingPipeline().check_drift(cur, ref, feature_names=["shifted"])
        assert PSI_ALERT_THRESHOLD < result.max_psi <= PSI_RETRAIN_THRESHOLD
        assert result.action == "alert"
        assert result.drifted_features == ["shifted"]

    def test_all_nan_feature_is_flagged_and_never_none(self) -> None:
        """A feature whose current window is entirely NaN sets the flag."""
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((500, 2))
        cur = rng.standard_normal((500, 2))
        cur[:, 1] = np.nan
        result = RetrainingPipeline().check_drift(cur, ref, feature_names=["ok", "broken"])
        assert result.action == "alert"
        assert result.drifted_features == ["broken"]
        assert result.nan_rates["broken"] == 1.0
        assert result.nan_rates["ok"] == 0.0
        assert result.psi_scores["broken"] == 0.0

    def test_default_feature_names(self) -> None:
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((100, 2))
        cur = rng.standard_normal((100, 2))
        result = RetrainingPipeline().check_drift(cur, ref)
        assert set(result.psi_scores) == {"feature_0", "feature_1"}
        assert set(result.nan_rates) == {"feature_0", "feature_1"}


# ---------------------------------------------------------------------------
# Champion-challenger tests
# ---------------------------------------------------------------------------


def _spec() -> Any:
    return replace(
        DEFAULT_FEATURE_SPEC,
        spec_version=3,
        horizon=1,
        feature_names=("f0",),
    )


def _test_matrix(n: int = 200) -> FeatureMatrix:
    """A +/-1% alternating target: a perfect predictor earns exactly 1% per trade."""
    closes = np.empty(n, dtype=np.float64)
    closes[0] = 100.0
    for i in range(1, n):
        closes[i] = closes[i - 1] * (1.01 if (i - 1) % 2 == 0 else 0.99)
    fwd = closes[1:] / closes[:-1] - 1.0
    signs = np.sign(fwd)
    padded_signs = np.full(n, np.nan)
    padded_signs[:-1] = signs
    padded_fwd = np.full(n, np.nan)
    padded_fwd[:-1] = fwd
    return FeatureMatrix(
        features=np.random.default_rng(3).standard_normal((n, 1)),
        feature_names=["f0"],
        timestamps=[datetime(2026, 1, 1, tzinfo=UTC)] * n,
        spec=_spec(),
        target=padded_signs,
        forward_returns=padded_fwd,
    )


def _constant_up_model() -> TrainedModel:
    """A model that always predicts the up class: earns the mean forward return."""
    mock = MagicMock()
    mock.predict = MagicMock(side_effect=lambda x: np.ones(len(x)))
    return TrainedModel(model=mock, model_type="test", metrics={}, feature_spec=_spec())


def _perfect_model(matrix: FeatureMatrix) -> TrainedModel:
    """A model that predicts each row's true class: earns mean |forward return|."""
    classes = ((matrix.target + 1.0) / 2.0)[~np.isnan(matrix.target)]
    mock = MagicMock()
    mock.predict = MagicMock(side_effect=lambda x: classes[: len(x)])
    return TrainedModel(model=mock, model_type="test", metrics={}, feature_spec=_spec())


class TestChampionChallenger:
    def test_challenger_wins_with_a_better_signed_return(self) -> None:
        matrix = _test_matrix()
        champion = _constant_up_model()  # earns the mean forward return ~ 0
        challenger = _perfect_model(matrix)  # earns mean |fwd| = 1%
        winner = RetrainingPipeline().champion_challenger(champion, challenger, matrix)
        assert winner == "challenger"

    def test_champion_wins_when_the_challenger_is_worse(self) -> None:
        matrix = _test_matrix()
        champion = _perfect_model(matrix)
        challenger = _constant_up_model()
        winner = RetrainingPipeline().champion_challenger(champion, challenger, matrix)
        assert winner == "champion"

    def test_equal_models_keep_the_champion(self) -> None:
        matrix = _test_matrix()
        winner = RetrainingPipeline().champion_challenger(
            _constant_up_model(), _constant_up_model(), matrix
        )
        assert winner == "champion"

    def test_missing_target_is_refused(self) -> None:
        matrix = _test_matrix()
        broken = FeatureMatrix(
            features=matrix.features,
            feature_names=matrix.feature_names,
            timestamps=matrix.timestamps,
            spec=matrix.spec,
        )
        with pytest.raises(ValueError, match="target and forward_returns"):
            RetrainingPipeline().champion_challenger(
                _constant_up_model(), _constant_up_model(), broken
            )
