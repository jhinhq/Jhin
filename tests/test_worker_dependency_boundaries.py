"""Distribution metadata and import boundaries for isolated workers."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _project(path: str) -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def _imports_under(root: str, prefix: str) -> bool:
    for path in (REPO_ROOT / root).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if any(name == prefix or name.startswith(f"{prefix}.") for name in imported):
            return True
    return False


def test_distribution_dependencies_and_imports_are_one_way() -> None:
    agent = _project("services/agent_worker/pyproject.toml")
    tool = _project("services/tool_worker/pyproject.toml")
    agent_dependencies = set(agent["project"]["dependencies"])
    tool_dependencies = set(tool["project"]["dependencies"])

    assert "jhin-connectors" not in agent_dependencies
    assert "jhin-connectors" not in agent["tool"]["uv"]["sources"]
    assert "jhin-agents" not in tool_dependencies
    assert "jhin-models" not in tool_dependencies
    assert "jhin-observability" not in tool_dependencies
    assert not _imports_under("services/agent_worker/src", "jhin_connectors")
    assert not _imports_under("services/tool_worker/src", "jhin_agents")
    assert not _imports_under("services/tool_worker/src", "jhin_models")
    assert not _imports_under("services/tool_worker/src", "jhin_observability")


def test_agent_worker_source_contains_no_local_tool_or_runner_effect_path() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "services/agent_worker/src").rglob("*.py")
    )
    for forbidden in (
        "build_default_catalog",
        "ToolGateway",
        "delete_sandbox_workspace",
        "PHASE9_SYNC_BEFORE_EFFECT",
        "PHASE9_CLEANUP_BEFORE_EFFECT",
    ):
        assert forbidden not in source


def test_tool_worker_never_imports_or_queries_agent_reasoning_authority() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "services/tool_worker/src").rglob("*.py")
    )
    for forbidden in (
        "AgentStepReasoningRecord",
        "agent.step.reasoning",
        "completion_sanitized",
        "provider_call_ids",
        "transitions",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    ):
        assert forbidden not in source


def test_worker_console_scripts_and_docker_metadata_are_installed() -> None:
    tool = _project("services/tool_worker/pyproject.toml")
    workflows = _project("packages/workflows/pyproject.toml")
    dockerfile = (REPO_ROOT / "docker/python.Dockerfile").read_text(encoding="utf-8")

    assert tool["project"]["scripts"] == {"jhin-tool-worker": "jhin_tool_worker.main:run"}
    assert workflows["project"]["scripts"] == {
        "jhin-temporal-poller-check": "jhin_workflows.poller_health:run"
    }
    assert "COPY services/tool_worker/pyproject.toml services/tool_worker/" in dockerfile
