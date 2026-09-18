"""The integration harness's embedded scripts are real Python.

Several fixtures ship as source inside string literals and are written to a
file and executed in a container. No linter, type checker or import reaches
them, so a syntax error there is invisible until a live job times out waiting
for a service that died on startup -- which is exactly how a stray newline
inside an SSE frame literal once took out three Phase 10 tests.

This reads the harness with ``ast`` instead of importing it, so it runs on any
platform: ``tests/integration`` imports ``fcntl`` and cannot be collected on
Windows, which is precisely where that gap hid.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "integration" / "phase10_upgrade_harness.py"


def _embedded_scripts() -> list[tuple[str, str]]:
    """Every ``*_SCRIPT`` constant in the harness, as (name, source)."""
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        name = getattr(node.targets[0], "id", "")
        if not name.endswith("_SCRIPT"):
            continue
        value = node.value
        # The constants are written as r"""...""" and usually .strip()ed.
        literal = value.func.value if isinstance(value, ast.Call) else value
        try:
            source = ast.literal_eval(literal)
        except ValueError:  # pragma: no cover - a form this test does not model
            continue
        if isinstance(source, str):
            found.append((name, source))
    return found


def test_the_harness_still_ships_embedded_scripts() -> None:
    """Guards the guard: a refactor that renames or inlines these constants
    would otherwise make every case below vacuously pass."""
    assert len(_embedded_scripts()) >= 3


@pytest.mark.parametrize(
    "name, source",
    _embedded_scripts(),
    ids=lambda v: v if isinstance(v, str) and v.isupper() else "",
)
def test_an_embedded_script_compiles(name: str, source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - the failure this exists for
        pytest.fail(f"{name} is not valid Python at line {exc.lineno}: {exc.msg}")
