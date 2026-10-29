"""The P5-C seam ownership, enforced on the AST.

The critic's hunk rule is a structural fact, so it is pinned structurally:
``session_wiring`` touches ``hydra.ml`` only inside ``_on_signal`` (the veto
seam — nothing else in the file may know ML exists), and the strategy package
never imports ``hydra.ml`` at all (the ML-native strategy consumes predictions
through ``hydra.strategy.ml_access`` — the import-linter layers make the
strategy layer the wrong place for the serving stack).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "hydra"


def _ml_imports(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Every import statement whose target starts with ``hydra.ml``."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("hydra.ml"):
                found.append((node, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("hydra.ml"):
                    found.append((node, alias.name))
    return found


def _enclosing_functions(tree: ast.Module, target: ast.AST) -> list[str]:
    """The names of every function definition containing *target*."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    names.append(node.name)
    return names


def test_session_wiring_imports_hydra_ml_only_inside_the_signal_seam() -> None:
    path = SRC / "execution" / "session_wiring.py"
    tree = ast.parse(path.read_text())
    imports = _ml_imports(tree)
    assert imports, "the veto seam must wire hydra.ml (it is gone)"
    for node, module in imports:
        enclosing = _enclosing_functions(tree, node)
        assert enclosing == ["_on_signal"], (
            f"session_wiring imports {module} outside _on_signal ({enclosing}); "
            "P5-C owns only the _on_signal seam in this file"
        )


def test_the_strategy_package_never_imports_hydra_ml() -> None:
    offenders: list[str] = []
    for path in sorted((SRC / "strategy").rglob("*.py")):
        for _node, module in _ml_imports(ast.parse(path.read_text())):
            offenders.append(f"{path.relative_to(SRC)} imports {module}")
    assert not offenders, (
        "strategies consume model predictions through hydra.strategy.ml_access, "
        f"never directly: {offenders}"
    )


def test_ml_ensemble_is_a_real_model_consumer_not_a_heuristic() -> None:
    source = (SRC / "strategy" / "builtin" / "ml_ensemble.py").read_text()
    assert "momentum-based" not in source
    assert "strategy.ml_access" in source
