"""Only the tool worker may construct the executable connector catalog."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def find_python_calls(root: Path, *, imported_name: str) -> set[Path]:
    callers: set[Path] = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == imported_name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == imported_name)
            )
            for node in ast.walk(tree)
        ):
            callers.add(relative)
    return callers


def test_executable_catalog_builder_has_one_runtime_caller() -> None:
    callers = find_python_calls(REPO_ROOT, imported_name="build_default_catalog")
    runtime_callers = {
        path
        for path in callers
        if "tests" not in path.parts
        and path != Path("packages/connectors/src/jhin_connectors/registry.py")
    }
    assert runtime_callers == {Path("services/tool_worker/src/jhin_tool_worker/main.py")}
