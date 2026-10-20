"""Module-level third-party imports in src/hydra must be declared runtime dependencies.

Function-level lazy imports (optional ML extras, in-route imports) are deliberately
out of scope: only imports executed when a module is imported are checked.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "hydra"

# Import name -> reason it may be imported without a direct declaration.
_ALLOWED_TRANSITIVE = {"starlette": "provided by fastapi"}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, f"unparseable requirement: {requirement!r}"
    return _normalize(match.group(1))


def _project() -> dict[str, object]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    assert isinstance(project, dict)
    return project


def _runtime_requirements() -> set[str]:
    deps = _project()["dependencies"]
    assert isinstance(deps, list)
    return {_requirement_name(d) for d in deps}


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names - set(sys.stdlib_module_names) - {"hydra", "__future__"}


def test_module_level_third_party_imports_are_declared() -> None:
    declared = _runtime_requirements()
    dists_by_import = packages_distributions()
    undeclared: dict[str, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for name in _module_level_imports(path):
            if name in _ALLOWED_TRANSITIVE:
                continue
            dists = {_normalize(d) for d in dists_by_import.get(name, [])}
            if not dists & declared:
                undeclared.setdefault(name, []).append(str(path.relative_to(_REPO_ROOT)))
    assert undeclared == {}, (
        f"imported at module level but not in [project].dependencies: {undeclared}"
    )


def test_python_telegram_bot_not_declared() -> None:
    project = _project()
    optional = project.get("optional-dependencies", {})
    assert isinstance(optional, dict)
    groups: list[list[str]] = [list(project["dependencies"]), *map(list, optional.values())]
    all_names = {_requirement_name(req) for group in groups for req in group}
    assert "python-telegram-bot" not in all_names
    assert "httpx" in _runtime_requirements()


def test_telegram_module_removed() -> None:
    assert importlib.util.find_spec("hydra.dashboard.telegram") is None
