"""hydra.services is transport-neutral: MCP tools call it without FastAPI in the import graph."""

from __future__ import annotations

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[3] / "src" / "hydra" / "services"
_FORBIDDEN = ("fastapi", "starlette", "hydra.dashboard")


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_services_import_no_http_framework() -> None:
    modules = sorted(SERVICES.rglob("*.py"))
    assert {p.stem for p in modules} >= {"errors", "backtest", "research", "strategies"}
    offenders = [
        f"{path.name}: {name}"
        for path in modules
        for name in _imports(ast.parse(path.read_text()))
        if name.split(".")[0] in _FORBIDDEN or name.startswith(_FORBIDDEN)
    ]
    assert offenders == []
