"""Distribution metadata and import boundaries for isolated workers."""

from __future__ import annotations

import ast
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _project(path: str) -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def _workspace_dependencies() -> dict[str, tuple[str, ...]]:
    """Each workspace member's declared dependencies, keyed by distribution name.

    Members are read from the root workspace list rather than globbed, so a
    package added to the workspace and forgotten here cannot leave a hole in
    the graph. Every intra-repo edge is a local workspace source, which is why
    the declared names alone are a faithful graph and no resolver is needed.
    """
    graph: dict[str, tuple[str, ...]] = {}
    for member in _project("pyproject.toml")["tool"]["uv"]["workspace"]["members"]:
        project = _project(f"{member}/pyproject.toml")["project"]
        graph[project["name"]] = tuple(project.get("dependencies", ()))
    return graph


def _dependency_path(root: str, target: str) -> tuple[str, ...] | None:
    """The shortest declared chain from ``root`` to ``target``, or ``None``.

    The whole chain is returned rather than a yes/no, because when a boundary
    is breached the expensive question is never *whether* — it is which
    intermediary started carrying the package.
    """
    graph = _workspace_dependencies()
    queue: deque[tuple[str, ...]] = deque([(root,)])
    seen = {root}
    while queue:
        path = queue.popleft()
        for dependency in graph.get(path[-1], ()):
            if dependency == target:
                return (*path, dependency)
            if dependency in graph and dependency not in seen:
                seen.add(dependency)
                queue.append((*path, dependency))
    return None


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
    workflows = _project("packages/workflows/pyproject.toml")
    agent_dependencies = set(agent["project"]["dependencies"])
    tool_dependencies = set(tool["project"]["dependencies"])

    assert "jhin-connectors" not in agent_dependencies
    assert "jhin-connectors" not in agent["tool"]["uv"]["sources"]
    assert "jhin-agents" not in tool_dependencies
    assert "jhin-models" not in tool_dependencies
    assert "jhin-observability" in tool_dependencies
    assert tool["tool"]["uv"]["sources"]["jhin-observability"] == {"workspace": True}
    assert not _imports_under("services/agent_worker/src", "jhin_connectors")
    assert not _imports_under("services/tool_worker/src", "jhin_agents")
    assert not _imports_under("services/tool_worker/src", "jhin_models")
    assert _imports_under("services/tool_worker/src", "jhin_observability")
    assert workflows["project"]["dependencies"].count("jhin-observability") == 1
    assert workflows["tool"]["uv"]["sources"]["jhin-observability"] == {"workspace": True}


def test_no_intermediary_carries_connectors_back_into_the_agent_worker() -> None:
    """The boundary is the transitive closure, not the direct dependency list.

    Direct edges are the easy half. ``jhin-oauth`` is a legitimate agent worker
    dependency — background token refresh — and it once depended on
    ``jhin-connectors``, so the image shipped the executable connector catalog
    and the credentials it resolves into the one process whose whole guarantee
    is that model reasoning structurally cannot execute a connector. Every
    direct-dependency assertion above passed the entire time.
    """
    reintroduced = _dependency_path("jhin-agent-worker", "jhin-connectors")
    assert reintroduced is None, (
        "jhin-connectors is reachable from the agent worker again via "
        + " -> ".join(reintroduced or ())
    )
    assert not _imports_under("packages/oauth/src", "jhin_connectors")
    assert _dependency_path("jhin-tool-worker", "jhin-connectors") is not None


def test_worker_settings_store_closed_environment_defaults(
    monkeypatch: Any,
) -> None:
    from jhin_agent_worker.settings import Settings as AgentSettings
    from jhin_event_worker.settings import Settings as EventSettings
    from jhin_sandbox_runner.settings import Settings as SandboxSettings
    from jhin_tool_worker.settings import ToolWorkerSettings
    from jhin_workflow_worker.settings import Settings as WorkflowSettings

    monkeypatch.delenv("APP_ENV", raising=False)
    assert AgentSettings().app_env == "dev"
    assert ToolWorkerSettings().app_env == "dev"
    assert EventSettings().app_env == "dev"
    assert WorkflowSettings().app_env == "dev"
    sandbox = SandboxSettings(
        sandbox_docker_mode="rootful",
        sandbox_docker_socket=Path("/run/docker.sock"),
        sandbox_docker_gid=1,
    )
    assert sandbox.app_env == "dev"


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


def test_root_pytest_discovers_each_observability_worker_suite_exactly_once() -> None:
    root = _project("pyproject.toml")
    testpaths = root["tool"]["pytest"]["ini_options"]["testpaths"]
    for expected in (
        "packages/observability/tests",
        "services/tool_worker/tests",
        "services/workflow_worker/tests",
    ):
        assert testpaths.count(expected) == 1
