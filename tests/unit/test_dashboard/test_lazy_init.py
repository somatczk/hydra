"""Tests for lazy ``__init__.py`` modules using ``__getattr__`` + ``_IMPORT_MAP``.

Tests cover:
- Each symbol listed in ``__all__`` is importable via ``__getattr__``
- ``__getattr__`` raises ``AttributeError`` for unknown names
- Lazy imports do not trigger RuntimeWarning
- The ``_IMPORT_MAP`` is consistent with ``__all__``
"""

from __future__ import annotations

import importlib
import warnings

import pytest

# ---------------------------------------------------------------------------
# Parameterised lazy-import modules
# ---------------------------------------------------------------------------

_LAZY_MODULES = [
    "hydra.backtest",
    "hydra.execution",
    "hydra.risk",
    "hydra.portfolio",
]


@pytest.mark.parametrize("module_path", _LAZY_MODULES)
class TestLazyInitModules:
    def test_all_symbols_importable(self, module_path: str) -> None:
        """Every name in ``__all__`` should be resolvable via ``__getattr__``."""
        mod = importlib.import_module(module_path)
        for name in mod.__all__:
            attr = getattr(mod, name)
            assert attr is not None, f"{module_path}.{name} resolved to None"

    def test_import_map_matches_all(self, module_path: str) -> None:
        """``_IMPORT_MAP`` keys should be exactly the same as ``__all__``."""
        mod = importlib.import_module(module_path)
        assert set(mod._IMPORT_MAP.keys()) == set(mod.__all__)

    def test_unknown_attribute_raises(self, module_path: str) -> None:
        """Accessing an unknown name should raise ``AttributeError``."""
        mod = importlib.import_module(module_path)
        bogus = "__definitely_not_a_real_attribute__"
        with pytest.raises(AttributeError, match="has no attribute"):
            getattr(mod, bogus)

    def test_no_runtime_warning(self, module_path: str) -> None:
        """Importing symbols should not emit RuntimeWarning about lazy imports."""
        mod = importlib.import_module(module_path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for name in mod.__all__:
                getattr(mod, name)
            runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
            assert len(runtime_warnings) == 0, f"RuntimeWarnings emitted: {runtime_warnings}"

    def test_getattr_returns_correct_type(self, module_path: str) -> None:
        """Each lazy-loaded attribute should be a class, function, or enum."""
        mod = importlib.import_module(module_path)
        for name in mod.__all__:
            attr = getattr(mod, name)
            assert callable(attr) or isinstance(attr, type), (
                f"{module_path}.{name} is {type(attr)}, expected callable or type"
            )
