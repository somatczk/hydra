"""Smoke test — verifies package imports correctly."""

import pytest


@pytest.mark.unit
def test_hydra_imports() -> None:
    import hydra
    assert hydra.__version__ == "0.1.0"
