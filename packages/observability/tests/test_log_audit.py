"""Executable repository-wide logger and bootstrap audit regressions."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from jhin_observability import EVENT_FIELD_RULES

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_phase10_logging.py"


class AuditModule(Protocol):
    def application_python_paths(self, root: Path) -> tuple[Path, ...]: ...

    def audit_paths(self, paths: tuple[Path, ...]) -> list[AuditFailureLike]: ...

    def collect_logging_method_calls(
        self, paths: tuple[Path, ...]
    ) -> tuple[LoggingCallLike, ...]: ...


class AuditFailureLike(Protocol):
    code: str


class LoggingCallLike(Protocol):
    method: str
    receiver: str


def _load_audit() -> AuditModule:
    spec = importlib.util.spec_from_file_location("phase10_logging_audit", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(cast(ModuleType, module))
    return cast(AuditModule, module)


def _failure_codes(failures: list[AuditFailureLike]) -> list[str]:
    return [failure.code for failure in failures]


def test_repository_logger_calls_are_all_registered() -> None:
    audit = _load_audit()
    paths = audit.application_python_paths(REPO_ROOT)
    assert audit.audit_paths(paths) == []
    assert audit.collect_logging_method_calls(paths)


def test_every_registered_event_is_covered_by_the_closed_registry() -> None:
    assert EVENT_FIELD_RULES
    assert "health.heartbeat_write_failed" in EVENT_FIELD_RULES
    assert "rootless_transport.ready" in EVENT_FIELD_RULES
    assert "rootless_transport.failed" in EVENT_FIELD_RULES


def test_reviewed_non_logger_receivers_are_semantically_narrow(tmp_path: Path) -> None:
    audit = _load_audit()
    jobs = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(
        "from typing import Any\n"
        "class C:\n"
        " async def _collect_logs(self, container: Any):\n"
        "  await container.log(stdout=True)\n"
        " async def current_logs(self):\n"
        "  container = self.docker.containers.container('id')\n"
        "  await container.log(stderr=True)\n"
    )
    database = tmp_path / "packages/connectors/src/jhin_connectors/supabase/database_tools.py"
    database.parent.mkdir(parents=True)
    database.write_text(
        "import asyncio\nfrom typing import Any\n"
        "def consume_result(completed: asyncio.Future[Any]):\n"
        " return completed.exception()\n"
    )
    assert audit.collect_logging_method_calls((jobs, database)) == ()

    jobs.write_text("def current_logs(logger):\n logger.log('raw')\n")
    calls = audit.collect_logging_method_calls((jobs,))
    assert [(call.method, call.receiver) for call in calls] == [
        ("log", "unresolved_logger_receiver")
    ]


def test_unresolved_actual_logger_shape_still_fails_closed(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "service.py"
    source.write_text("def emit(audit_logger):\n audit_logger.exception('raw')\n")
    assert _failure_codes(audit.audit_paths((source,))) == ["unresolved_logger_receiver"]


def test_bound_stdlib_logger_is_always_foreign_logging(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "service.py"
    source.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\nlogger.warning('api.started')\n"
    )
    assert _failure_codes(audit.audit_paths((source,))) == ["foreign_logging"]


def test_poller_health_allows_only_closed_protocol_prints(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "_READY_OUTPUT = 'ready'\n_UNAVAILABLE_OUTPUT = 'unavailable'\n"
        "def main(ok):\n"
        " print(_READY_OUTPUT if ok else _UNAVAILABLE_OUTPUT)\n"
        "def run():\n print(_UNAVAILABLE_OUTPUT)\n"
    )
    assert audit.audit_paths((source,)) == []


def test_poller_health_rejects_print_near_misses(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    candidates = (
        "def main():\n print('ready')\n",
        "def main(value):\n print(f'{value}')\n",
        "def main():\n print(_READY_OUTPUT, _UNAVAILABLE_OUTPUT)\n",
        "def helper():\n print(_UNAVAILABLE_OUTPUT)\n",
    )
    for candidate in candidates:
        source.write_text(
            "_READY_OUTPUT = 'ready'\n_UNAVAILABLE_OUTPUT = 'unavailable'\n" + candidate
        )
        assert "direct_print" in _failure_codes(audit.audit_paths((source,)))


def test_job_manager_allows_only_awaited_validated_docker_info(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  await client.system.info()\n"
    )
    assert audit.audit_paths((source,)) == []


def test_job_manager_rejects_docker_info_near_misses(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    candidates = (
        "class JobManager:\n async def other(self, client):\n  await client.system.info()\n",
        "class JobManager:\n async def start(self, client):\n  await client.system.info()\n",
        "import aiodocker\nclass JobManager:\n async def start(self, validated_url):\n"
        "  client = aiodocker.Docker(url=validated_url)\n  client.system.info()\n",
        "import aiodocker\nclass JobManager:\n async def start(self, validated_url):\n"
        "  client = object()\n  await client.system.info()\n",
    )
    for candidate in candidates:
        source.write_text(candidate)
        assert "unresolved_logger_receiver" in _failure_codes(audit.audit_paths((source,)))


def _entrypoint_function(path: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )


def _configured_call(function: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Call:
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "configure_json_logging"
    ]
    assert len(calls) == 1
    return calls[0]


def test_every_entrypoint_bootstraps_exact_json_contract() -> None:
    entrypoints = {
        "apps/api/src/jhin_api/main.py": ("create_app", "api", "FastAPI"),
        "services/agent_worker/src/jhin_agent_worker/main.py": (
            "main",
            "agent-worker",
            "connect_with_retry",
        ),
        "services/tool_worker/src/jhin_tool_worker/main.py": (
            "main",
            "tool-worker",
            "connect_with_retry",
        ),
        "services/event_worker/src/jhin_event_worker/main.py": (
            "main",
            "event-worker",
            "connect_with_retry",
        ),
        "services/workflow_worker/src/jhin_workflow_worker/main.py": (
            "main",
            "workflow-worker",
            "connect_with_retry",
        ),
        "services/sandbox_runner/src/jhin_sandbox_runner/main.py": (
            "main",
            "sandbox-runner",
            "uvicorn.run",
        ),
        "services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py": (
            "main",
            "rootless-docker-transport",
            "asyncio.run",
        ),
    }
    for relative, (function_name, service, first_action) in entrypoints.items():
        function = _entrypoint_function(REPO_ROOT / relative, function_name)
        call = _configured_call(function)
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert isinstance(keywords["service"], ast.Constant)
        assert keywords["service"].value == service
        assert ast.unparse(keywords["environment"]).startswith("normalize_environment(")
        assert "level" in keywords
        action = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == first_action
        )
        assert call.lineno < action.lineno
