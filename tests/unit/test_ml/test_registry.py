"""Tests for hydra.ml.registry -- model registry module."""

from __future__ import annotations

import pytest

from hydra.ml.registry import ModelInfo, ModelRegistry
from hydra.ml.training import TrainedModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trained_model(model_type: str = "xgboost") -> TrainedModel:
    """Create a minimal TrainedModel for testing."""
    return TrainedModel(
        model=object(),
        model_type=model_type,
        metrics={"accuracy": 0.75, "sharpe": 1.2},
        feature_names=["f0", "f1", "f2"],
        params={"max_depth": 6},
    )


@pytest.fixture()
def registry() -> ModelRegistry:
    return ModelRegistry()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_returns_model_id(self, registry: ModelRegistry) -> None:
        model = _make_trained_model()
        model_id = registry.register(model, "alpha_model")
        assert model_id == "alpha_model/v1"

    def test_register_auto_increments_version(self, registry: ModelRegistry) -> None:
        m1 = _make_trained_model()
        m2 = _make_trained_model()
        m3 = _make_trained_model()

        id1 = registry.register(m1, "alpha_model")
        id2 = registry.register(m2, "alpha_model")
        id3 = registry.register(m3, "alpha_model")

        assert id1 == "alpha_model/v1"
        assert id2 == "alpha_model/v2"
        assert id3 == "alpha_model/v3"


# ---------------------------------------------------------------------------
# Retrieval tests
# ---------------------------------------------------------------------------


class TestGetModel:
    def test_get_model_roundtrip(self, registry: ModelRegistry) -> None:
        model = _make_trained_model()
        registry.register(model, "test_model")
        retrieved = registry.get_model("test_model")
        assert retrieved is model

    def test_get_model_by_version(self, registry: ModelRegistry) -> None:
        m1 = _make_trained_model()
        m2 = _make_trained_model()
        registry.register(m1, "test_model")
        registry.register(m2, "test_model")

        assert registry.get_model("test_model", "v1") is m1
        assert registry.get_model("test_model", "v2") is m2

    def test_get_latest_when_no_version(self, registry: ModelRegistry) -> None:
        m1 = _make_trained_model()
        m2 = _make_trained_model()
        registry.register(m1, "test_model")
        registry.register(m2, "test_model")

        assert registry.get_model("test_model") is m2

    def test_get_nonexistent_returns_none(self, registry: ModelRegistry) -> None:
        assert registry.get_model("nonexistent") is None

    def test_get_wrong_version_returns_none(self, registry: ModelRegistry) -> None:
        model = _make_trained_model()
        registry.register(model, "test_model")
        assert registry.get_model("test_model", "v99") is None


# ---------------------------------------------------------------------------
# List models tests
# ---------------------------------------------------------------------------


class TestListModels:
    def test_list_all_models(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "model_a")
        registry.register(_make_trained_model(), "model_b")
        registry.register(_make_trained_model(), "model_a")

        all_models = registry.list_models()
        assert len(all_models) == 3
        assert all(isinstance(m, ModelInfo) for m in all_models)

    def test_list_filtered_by_name(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "model_a")
        registry.register(_make_trained_model(), "model_b")
        registry.register(_make_trained_model(), "model_a")

        filtered = registry.list_models("model_a")
        assert len(filtered) == 2
        assert all(m.name == "model_a" for m in filtered)

    def test_list_empty_name(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "model_a")
        filtered = registry.list_models("nonexistent")
        assert len(filtered) == 0

    def test_model_info_fields(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        info_list = registry.list_models("test")
        info = info_list[0]
        assert info.model_id == "test/v1"
        assert info.name == "test"
        assert info.version == "v1"
        assert info.stage == "none"
        assert "accuracy" in info.metrics
        assert info.created_at is not None


# ---------------------------------------------------------------------------
# Promotion tests
# ---------------------------------------------------------------------------


class TestPromote:
    def test_promote_to_staging(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        registry.promote("test/v1", "staging")
        info = registry.list_models("test")[0]
        assert info.stage == "staging"

    def test_promote_to_production(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        registry.promote("test/v1", "production")
        info = registry.list_models("test")[0]
        assert info.stage == "production"

    def test_promote_demotes_previous_production(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        registry.register(_make_trained_model(), "test")

        registry.promote("test/v1", "production")
        registry.promote("test/v2", "production")

        infos = registry.list_models("test")
        v1_info = next(i for i in infos if i.version == "v1")
        v2_info = next(i for i in infos if i.version == "v2")

        assert v1_info.stage == "archived"
        assert v2_info.stage == "production"

    def test_promote_invalid_stage_raises(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        with pytest.raises(ValueError, match="Invalid stage"):
            registry.promote("test/v1", "invalid_stage")

    def test_promote_missing_model_raises(self, registry: ModelRegistry) -> None:
        with pytest.raises(ValueError, match="not found"):
            registry.promote("nonexistent/v1", "staging")


# ---------------------------------------------------------------------------
# Production model tests
# ---------------------------------------------------------------------------


class TestGetProductionModel:
    def test_get_production_model(self, registry: ModelRegistry) -> None:
        model = _make_trained_model()
        registry.register(model, "test")
        registry.promote("test/v1", "production")

        prod = registry.get_production_model("test")
        assert prod is model

    def test_no_production_returns_none(self, registry: ModelRegistry) -> None:
        registry.register(_make_trained_model(), "test")
        assert registry.get_production_model("test") is None

    def test_nonexistent_name_returns_none(self, registry: ModelRegistry) -> None:
        assert registry.get_production_model("nonexistent") is None
