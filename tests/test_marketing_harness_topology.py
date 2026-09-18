"""Current live acceptance must account for every normal base service."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml  # type: ignore[import-untyped]


def test_live_harness_includes_all_normal_base_services() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    expected = {
        name for name, service in compose["services"].items() if not service.get("profiles")
    }
    harness = ast.parse(
        (root / "tests/integration/phase10_upgrade_harness.py").read_text(encoding="utf-8")
    )
    assignment = next(
        node
        for node in harness.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "EXPECTED_ROOTFUL_SERVICES"
            for target in node.targets
        )
    )
    assert expected <= ast.literal_eval(assignment.value)
