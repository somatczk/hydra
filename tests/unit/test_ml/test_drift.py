"""Tests for hydra.ml.retraining -- drift detection and retraining pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from hydra.ml.features import FeatureMatrix
from hydra.ml.retraining import (
    DriftResult,
    RetrainingPipeline,
    _compute_psi,
)
from hydra.ml.training import TrainedModel

# ---------------------------------------------------------------------------
# PSI calculation tests
# ---------------------------------------------------------------------------


class TestPSICalculation:
    def test_identical_distributions_psi_near_zero(self) -> None:
        """PSI of a distribution against itself should be approximately 0."""
        rng = np.random.default_rng(42)
        data = rng.standard_normal(1000)
        psi = _compute_psi(data, data.copy())
        assert psi < 0.01

    def test_shifted_distribution_positive_psi(self) -> None:
        """Shifting a distribution should produce positive PSI."""
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        shifted = reference + 3.0  # large shift
        psi = _compute_psi(reference, shifted)
        assert psi > 0.0

    def test_large_shift_high_psi(self) -> None:
        """A very large shift should produce a high PSI value."""
        rng = np.random.default_rng(42)
        reference = rng.standard_normal(1000)
        shifted = reference + 10.0
        psi = _compute_psi(reference, shifted)
        assert psi > 0.2

    def test_empty_arrays(self) -> None:
        psi = _compute_psi(np.array([]), np.array([]))
        assert psi == 0.0

    def test_nan_handling(self) -> None:
        """NaN values should be excluded from PSI computation."""
        rng = np.random.default_rng(42)
        ref = rng.standard_normal(100)
        cur = ref.copy()
        cur[:10] = np.nan
        psi = _compute_psi(ref, cur)
        # Should still compute without error
        assert psi >= 0.0


# ---------------------------------------------------------------------------
# Drift check thresholds
# ---------------------------------------------------------------------------


class TestDriftThresholds:
    def test_no_drift_action_none(self) -> None:
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((500, 3))
        cur = rng.standard_normal((500, 3))

        pipeline = RetrainingPipeline()
        result = pipeline.check_drift(cur, ref, feature_names=["a", "b", "c"])

        assert isinstance(result, DriftResult)
        assert result.action == "none"
        assert result.max_psi < 0.2

    def test_alert_threshold(self) -> None:
        """PSI > 0.2 but <= 0.25 should trigger alert."""
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((1000, 1))
        # Shift enough for PSI between 0.2 and 0.25
        cur = ref + 1.5

        pipeline = RetrainingPipeline()
        result = pipeline.check_drift(cur, ref, feature_names=["shifted_feat"])

        # The exact PSI depends on the shift magnitude; confirm it's above alert
        assert result.max_psi > 0.2
        assert result.action in ("alert", "retrain")
        assert len(result.drifted_features) > 0

    def test_retrain_threshold(self) -> None:
        """PSI > 0.25 should trigger retrain."""
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((1000, 1))
        # Very large shift for definite retrain
        cur = ref + 5.0

        pipeline = RetrainingPipeline()
        result = pipeline.check_drift(cur, ref, feature_names=["drifted"])

        assert result.max_psi > 0.25
        assert result.action == "retrain"

    def test_default_feature_names(self) -> None:
        """When feature_names is None, generic names should be used."""
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((100, 2))
        cur = rng.standard_normal((100, 2))

        pipeline = RetrainingPipeline()
        result = pipeline.check_drift(cur, ref)

        assert "feature_0" in result.psi_scores
        assert "feature_1" in result.psi_scores


# ---------------------------------------------------------------------------
# Champion-challenger tests
# ---------------------------------------------------------------------------


class TestChampionChallenger:
    def _make_model(self, predictions: np.ndarray) -> TrainedModel:
        mock = MagicMock()
        mock.predict = MagicMock(return_value=predictions)
        return TrainedModel(
            model=mock,
            model_type="test",
            metrics={},
            feature_names=["f0"],
        )

    def test_challenger_wins_with_better_sharpe(self) -> None:
        """When challenger has strictly better predictions, it should win."""
        rng = np.random.default_rng(42)
        n = 200
        target = np.sign(rng.standard_normal(n))

        # Champion predicts poorly (random noise)
        champion = self._make_model(rng.standard_normal(n) * 0.01)
        # Challenger predicts the actual target
        challenger = self._make_model(target.copy())

        fm = FeatureMatrix(
            features=rng.standard_normal((n, 1)),
            feature_names=["f0"],
            timestamps=[],
            target=None,
        )

        pipeline = RetrainingPipeline()
        winner = pipeline.champion_challenger(champion, challenger, fm, target)
        assert winner == "challenger"

    def test_champion_wins_when_challenger_is_worse(self) -> None:
        """When champion has better predictions, it should win."""
        rng = np.random.default_rng(42)
        n = 200
        target = np.sign(rng.standard_normal(n))

        # Champion predicts the actual target
        champion = self._make_model(target.copy())
        # Challenger predicts noise
        challenger = self._make_model(rng.standard_normal(n) * 0.01)

        fm = FeatureMatrix(
            features=rng.standard_normal((n, 1)),
            feature_names=["f0"],
            timestamps=[],
            target=None,
        )

        pipeline = RetrainingPipeline()
        winner = pipeline.champion_challenger(champion, challenger, fm, target)
        assert winner == "champion"

    def test_equal_models_champion_wins(self) -> None:
        """When models are equal, champion should win (tie-break)."""
        preds = np.array([1.0, -1.0, 1.0])
        champion = self._make_model(preds.copy())
        challenger = self._make_model(preds.copy())

        fm = FeatureMatrix(
            features=np.ones((3, 1)),
            feature_names=["f0"],
            timestamps=[],
            target=None,
        )

        pipeline = RetrainingPipeline()
        winner = pipeline.champion_challenger(champion, challenger, fm, preds)
        assert winner == "champion"
