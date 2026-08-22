"""Executable repository-wide logger and bootstrap audit regressions."""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest

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


def test_dynamic_and_extracted_logger_methods_fail_closed(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "service.py"
    candidates = (
        "from jhin_observability import get_logger\n"
        "logger = get_logger(__name__)\ngetattr(logger, 'info')('api.started')\n",
        "from jhin_observability import get_logger\n"
        "logger = get_logger(__name__)\nemit = logger.info\nemit('api.started')\n",
    )
    for candidate in candidates:
        source.write_text(candidate)
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
        "_READY_OUTPUT = 'workflow-poller-ready'\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n"
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
            "_READY_OUTPUT = 'workflow-poller-ready'\n"
            "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n" + candidate
        )
        assert "direct_print" in _failure_codes(audit.audit_paths((source,)))


def test_poller_health_rejects_mutated_or_rebound_output_constants(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    print_shapes = (
        "def main(ok):\n print(_READY_OUTPUT if ok else _UNAVAILABLE_OUTPUT)\n"
        "def run():\n print(_UNAVAILABLE_OUTPUT)\n"
    )
    candidates = (
        "_READY_OUTPUT = 'secret-canary'\n_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n",
        "import os\n_READY_OUTPUT = os.environ['SECRET_CANARY']\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n",
        "import os\n_READY_OUTPUT = 'workflow-poller-ready'\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n"
        "_UNAVAILABLE_OUTPUT = os.environ['SECRET_CANARY']\n",
    )
    for candidate in candidates:
        source.write_text(candidate + print_shapes)
        assert "direct_print" in _failure_codes(audit.audit_paths((source,)))


@pytest.mark.parametrize(
    "rebind",
    (
        "from provider import value as _READY_OUTPUT\n",
        "import provider as _READY_OUTPUT\n",
        "def _READY_OUTPUT():\n pass\n",
        "async def _READY_OUTPUT():\n pass\n",
        "class _READY_OUTPUT:\n pass\n",
        "try:\n raise RuntimeError\nexcept RuntimeError as _READY_OUTPUT:\n pass\n",
        "del _READY_OUTPUT\n",
    ),
    ids=(
        "from-import",
        "import",
        "function",
        "async-function",
        "class",
        "except-target",
        "delete",
    ),
)
def test_poller_health_rejects_every_module_rebinding_form(
    tmp_path: Path,
    rebind: str,
) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "_READY_OUTPUT = 'workflow-poller-ready'\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n" + rebind + "def main(ok):\n"
        " print(_READY_OUTPUT if ok else _UNAVAILABLE_OUTPUT)\n"
    )
    assert "direct_print" in _failure_codes(audit.audit_paths((source,)))


def test_poller_health_ignores_nested_scope_output_bindings(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "_READY_OUTPUT = 'workflow-poller-ready'\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n"
        "def helper():\n"
        " _READY_OUTPUT = 'function-local'\n"
        " return _READY_OUTPUT\n"
        "class Namespace:\n"
        " _READY_OUTPUT = 'class-local'\n"
        "values = [_READY_OUTPUT for _READY_OUTPUT in ()]\n"
        "def main(ok):\n"
        " print(_READY_OUTPUT if ok else _UNAVAILABLE_OUTPUT)\n"
    )
    assert audit.audit_paths((source,)) == []


def test_poller_health_rejects_nested_class_global_output_rebinding(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "packages/workflows/src/jhin_workflows/poller_health.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "_READY_OUTPUT = 'workflow-poller-ready'\n"
        "_UNAVAILABLE_OUTPUT = 'workflow-poller-unavailable'\n"
        "class Mutator:\n"
        " global _READY_OUTPUT\n"
        " _READY_OUTPUT = object()\n"
        "def main(ok):\n"
        " print(_READY_OUTPUT if ok else _UNAVAILABLE_OUTPUT)\n"
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
        "import aiodocker\nclass JobManager:\n async def start(self, validated_url, foreign):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  client = foreign\n  await client.system.info()\n",
        "import aiodocker\nclass JobManager:\n async def start(self, validated_url, use_it):\n"
        "  if use_it:\n   client = aiodocker.Docker(url=validated_url)\n"
        "  await client.system.info()\n",
    )
    for candidate in candidates:
        source.write_text(candidate)
        assert "unresolved_logger_receiver" in _failure_codes(audit.audit_paths((source,)))


@pytest.mark.parametrize(
    "rebind",
    (
        "  from provider import client\n",
        "  import provider as client\n",
        "  def client():\n   return None\n",
        "  async def client():\n   return None\n",
        "  class client:\n   pass\n",
        "  try:\n   raise RuntimeError\n  except RuntimeError as client:\n   pass\n",
        "  del client\n",
    ),
    ids=(
        "from-import",
        "import",
        "function",
        "async-function",
        "class",
        "except-target",
        "delete",
    ),
)
def test_job_manager_rejects_every_local_rebinding_form(
    tmp_path: Path,
    rebind: str,
) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        + rebind
        + "  await client.system.info()\n"
    )
    assert "unresolved_logger_receiver" in _failure_codes(audit.audit_paths((source,)))


def test_job_manager_ignores_nested_scope_client_bindings(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  def helper():\n"
        "   client = object()\n"
        "   return client\n"
        "  class Namespace:\n"
        "   client = object()\n"
        "  class GlobalNamespace:\n"
        "   global client\n"
        "   client = object()\n"
        "  values = [client for client in ()]\n"
        "  await client.system.info()\n"
    )
    assert audit.audit_paths((source,)) == []


def test_job_manager_rejects_nested_class_nonlocal_client_rebinding(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url, foreign):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  class Mutator:\n"
        "   nonlocal client\n"
        "   client = foreign\n"
        "  await client.system.info()\n"
    )
    assert "unresolved_logger_receiver" in _failure_codes(audit.audit_paths((source,)))


def test_job_manager_nonlocal_targets_nearest_enclosing_binding(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url, foreign):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  def intermediate():\n"
        "   client = foreign\n"
        "   def inner():\n"
        "    nonlocal client\n"
        "    client = foreign\n"
        "   return inner\n"
        "  await client.system.info()\n"
    )
    assert audit.audit_paths((source,)) == []


def test_job_manager_nonlocal_skips_unbound_intermediate_scope(tmp_path: Path) -> None:
    audit = _load_audit()
    source = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiodocker\n"
        "class JobManager:\n"
        " async def start(self, validated_url, foreign):\n"
        "  client = aiodocker.Docker(url=validated_url)\n"
        "  def intermediate():\n"
        "   def inner():\n"
        "    nonlocal client\n"
        "    client = foreign\n"
        "   return inner\n"
        "  await client.system.info()\n"
    )
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


_BOOTSTRAP_CALLS = frozenset(
    {
        "jhin_observability.initialize_observability",
        "jhin_observability.configure_json_logging",
        "jhin_observability.configure_logging",
        "jhin_observability.logging.configure_json_logging",
        "jhin_observability.logging.configure_logging",
    }
)
_BOOTSTRAP_LEAVES = frozenset(name.rsplit(".", 1)[-1] for name in _BOOTSTRAP_CALLS)


@dataclasses.dataclass(frozen=True)
class _BootstrapBinding:
    qualified: str | None
    direct: bool
    initialized: bool
    rebound: bool
    tainted: bool


@dataclasses.dataclass(frozen=True)
class _BootstrapCall:
    node: ast.Call
    qualified: str | None
    direct: bool
    unresolved: bool
    spelling: str


def _bootstrap_spelling(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _bootstrap_spelling(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _bootstrap_module_name(path: Path) -> str:
    parts = path.as_posix().split("/src/", 1)
    if len(parts) != 2:
        return "fixture"
    relative = parts[1]
    if relative.endswith("/__init__.py"):
        relative = relative[: -len("/__init__.py")]
    elif relative.endswith(".py"):
        relative = relative[:-3]
    return relative.replace("/", ".")


def _bootstrap_pattern_capture_names(pattern: ast.pattern) -> frozenset[str]:
    names: set[str] = set()
    pending = [pattern]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.MatchAs):
            if current.pattern is not None:
                pending.append(current.pattern)
            if current.name not in {None, "_"}:
                names.add(current.name)
        elif isinstance(current, ast.MatchStar):
            if current.name not in {None, "_"}:
                names.add(current.name)
        elif isinstance(current, ast.MatchSequence):
            pending.extend(current.patterns)
        elif isinstance(current, ast.MatchMapping):
            pending.extend(current.patterns)
            if current.rest not in {None, "_"}:
                names.add(current.rest)
        elif isinstance(current, ast.MatchClass):
            pending.extend(current.patterns)
            pending.extend(current.kwd_patterns)
        elif isinstance(current, ast.MatchOr):
            pending.extend(current.patterns)
    return frozenset(names)


class _BootstrapLocals(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if not node.type_params:
            for parameter in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.args.vararg is not None and node.args.vararg.annotation is not None:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
                self.visit(node.args.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.names.add(node.name.id)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_bootstrap_pattern_capture_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.AST, ...],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))


class _BootstrapScanner(ast.NodeVisitor):
    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.scopes: list[dict[str, _BootstrapBinding]] = [{}]
        self.scope_kinds: list[str] = ["module"]
        self.hidden_class_scope_indices: set[int] = set()
        self.class_scope_post_bindings: dict[int, tuple[str, int]] = {}
        self.deferred_post_class_bindings: list[tuple[str, int]] = []
        self.code_scope_indices: list[int] = [0]
        self.calls: list[_BootstrapCall] = []
        self.declared_scope_names: set[str] = set()

    def visit_Module(self, node: ast.Module) -> None:
        self.declared_scope_names = {
            name
            for descendant in ast.walk(node)
            if isinstance(descendant, (ast.Global, ast.Nonlocal))
            for name in descendant.names
        }
        self.generic_visit(node)

    def _push_scope(self, scope: dict[str, _BootstrapBinding], kind: str) -> int:
        self.scopes.append(scope)
        self.scope_kinds.append(kind)
        return len(self.scopes) - 1

    def _pop_scope(self) -> dict[str, _BootstrapBinding]:
        self.scope_kinds.pop()
        return self.scopes.pop()

    def _hide_active_class_scopes(
        self,
        *,
        deferred: bool,
    ) -> tuple[set[int], list[tuple[str, int]]]:
        previous = set(self.hidden_class_scope_indices)
        previous_post_bindings = list(self.deferred_post_class_bindings)
        active_class_indices = {
            index for index, kind in enumerate(self.scope_kinds) if kind == "class"
        }
        self.hidden_class_scope_indices.update(active_class_indices)
        if deferred:
            for index in sorted(active_class_indices):
                post_binding = self.class_scope_post_bindings.get(index)
                if (
                    post_binding is not None
                    and post_binding not in self.deferred_post_class_bindings
                ):
                    self.deferred_post_class_bindings.append(post_binding)
        return previous, previous_post_bindings

    def _lookup(self, name: str) -> _BootstrapBinding:
        binding_scope_index: int | None = None
        for index in range(len(self.scopes) - 1, -1, -1):
            if index in self.hidden_class_scope_indices:
                continue
            scope = self.scopes[index]
            if name in scope:
                binding = scope[name]
                binding_scope_index = index
                break
        else:
            binding = _BootstrapBinding(None, True, False, False, name in _BOOTSTRAP_LEAVES)
        post_binding_scope_indices = [
            scope_index
            for pending_name, scope_index in self.deferred_post_class_bindings
            if pending_name == name
        ]
        if post_binding_scope_indices and (
            binding_scope_index is None or binding_scope_index <= max(post_binding_scope_indices)
        ):
            return _BootstrapBinding(
                None,
                False,
                True,
                True,
                binding.tainted or name in _BOOTSTRAP_LEAVES,
            )
        if name in self.declared_scope_names:
            return _BootstrapBinding(
                None,
                False,
                True,
                True,
                binding.tainted or name in _BOOTSTRAP_LEAVES,
            )
        return binding

    def _resolve(self, node: ast.AST) -> _BootstrapBinding:
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            qualified = f"{base.qualified}.{node.attr}" if base.qualified is not None else None
            return _BootstrapBinding(
                qualified,
                base.direct,
                base.initialized,
                base.rebound,
                base.tainted or node.attr in _BOOTSTRAP_LEAVES,
            )
        return _BootstrapBinding(None, True, True, False, False)

    def _bind(
        self,
        name: str,
        binding: _BootstrapBinding,
        *,
        scope_index: int = -1,
    ) -> None:
        scope = self.scopes[scope_index]
        previous = scope.get(name)
        scope[name] = dataclasses.replace(
            binding,
            initialized=True,
            rebound=binding.rebound or bool(previous and previous.initialized),
            tainted=(
                binding.tainted or name in _BOOTSTRAP_LEAVES or bool(previous and previous.tainted)
            ),
        )

    def _assign(
        self,
        target: ast.AST,
        value: ast.AST | None,
        *,
        scope_index: int = -1,
    ) -> None:
        if isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                self._assign(item, None, scope_index=scope_index)
            return
        if isinstance(target, ast.Starred):
            self._assign(target.value, None, scope_index=scope_index)
            return
        if not isinstance(target, ast.Name):
            return
        resolved = (
            self._resolve(value)
            if value is not None
            else _BootstrapBinding(None, True, True, False, False)
        )
        binding = (
            dataclasses.replace(resolved, direct=False)
            if resolved.qualified is not None and not resolved.qualified.startswith("local:")
            else _BootstrapBinding(
                f"local:{target.id}",
                True,
                True,
                False,
                resolved.tainted,
            )
        )
        self._bind(target.id, binding, scope_index=scope_index)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            qualified = alias.name if alias.asname else alias.name.split(".", 1)[0]
            self._bind(
                name,
                _BootstrapBinding(
                    qualified,
                    alias.asname is None,
                    True,
                    False,
                    False,
                ),
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            if node.level:
                self._bind(
                    name,
                    _BootstrapBinding(
                        None,
                        False,
                        True,
                        False,
                        alias.name in _BOOTSTRAP_LEAVES or name in _BOOTSTRAP_LEAVES,
                    ),
                )
                continue
            qualified = f"{node.module}.{alias.name}"
            self._bind(
                name,
                _BootstrapBinding(
                    qualified,
                    alias.asname is None,
                    True,
                    False,
                    qualified in _BOOTSTRAP_CALLS,
                ),
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._assign(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._assign(node.target, node.value)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._assign(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._assign(
            node.target,
            node.value,
            scope_index=self.code_scope_indices[-1],
        )

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._assign(target, None)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._assign(node.target, None)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._assign(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._assign(ast.Name(id=node.name, ctx=ast.Store()), None)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.visit(case.pattern)
            for name in sorted(_bootstrap_pattern_capture_names(case.pattern)):
                self._assign(ast.Name(id=name, ctx=ast.Store()), None)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        type_bindings = {
            type_parameter.name: _BootstrapBinding(
                f"local:{type_parameter.name}",
                True,
                True,
                False,
                type_parameter.name in _BOOTSTRAP_LEAVES,
            )
            for type_parameter in node.type_params
        }
        if type_bindings:
            self._push_scope(dict(type_bindings), "type_params")
            for type_parameter in node.type_params:
                self.visit(type_parameter)
            for parameter in parameters:
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.returns is not None:
                self.visit(node.returns)
            self._pop_scope()
        else:
            for parameter in parameters:
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.returns is not None:
                self.visit(node.returns)
        self._bind(
            node.name,
            _BootstrapBinding(f"{self.module_name}.{node.name}", True, True, False, False),
        )
        collector = _BootstrapLocals()
        for statement in node.body:
            collector.visit(statement)
        scope = {
            name: _BootstrapBinding(None, True, False, False, name in _BOOTSTRAP_LEAVES)
            for name in collector.names
        }
        scope.update(type_bindings)
        for parameter in parameters:
            scope[parameter.arg] = _BootstrapBinding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                parameter.arg in _BOOTSTRAP_LEAVES,
            )
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        body_scope_index = self._push_scope(scope, "function")
        self.code_scope_indices.append(body_scope_index)
        for statement in node.body:
            self.visit(statement)
        self.code_scope_indices.pop()
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        collector = _BootstrapLocals()
        collector.visit(node.body)
        scope = {
            name: _BootstrapBinding(
                None,
                True,
                False,
                False,
                name in _BOOTSTRAP_LEAVES,
            )
            for name in collector.names
        }
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        for parameter in parameters:
            scope[parameter.arg] = _BootstrapBinding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                parameter.arg in _BOOTSTRAP_LEAVES,
            )
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        body_scope_index = self._push_scope(scope, "lambda")
        self.code_scope_indices.append(body_scope_index)
        self.visit(node.body)
        self.code_scope_indices.pop()
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.AST, ...],
    ) -> None:
        first, *remaining = generators
        self.visit(first.iter)
        collector = _BootstrapLocals()
        for generator in generators:
            collector.visit(generator.target)
        scope = {
            name: _BootstrapBinding(
                None,
                True,
                False,
                False,
                name in _BOOTSTRAP_LEAVES,
            )
            for name in collector.names
        }
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        self._push_scope(scope, "comprehension")
        self._assign(first.target, None)
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator.iter)
            self._assign(generator.target, None)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        type_scope = {
            parameter.name: _BootstrapBinding(
                f"local:{parameter.name}",
                True,
                True,
                False,
                parameter.name in _BOOTSTRAP_LEAVES,
            )
            for parameter in node.type_params
        }
        scope_index = self._push_scope(type_scope, "type_params")
        self.code_scope_indices.append(scope_index)
        for type_parameter in node.type_params:
            self.visit(type_parameter)
        self.visit(node.value)
        self.code_scope_indices.pop()
        self._pop_scope()
        self._assign(node.name, None)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent_scope_index = len(self.scopes) - 1
        post_binding = (
            None
            if self.scope_kinds[parent_scope_index] == "class"
            else (node.name, parent_scope_index)
        )
        for decorator in node.decorator_list:
            self.visit(decorator)
        type_bindings = {
            parameter.name: _BootstrapBinding(
                f"local:{parameter.name}",
                True,
                True,
                False,
                parameter.name in _BOOTSTRAP_LEAVES,
            )
            for parameter in node.type_params
        }
        if type_bindings:
            self._push_scope(dict(type_bindings), "type_params")
            for type_parameter in node.type_params:
                self.visit(type_parameter)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=False)
        class_scope_index = self._push_scope({}, "class")
        if post_binding is not None:
            self.class_scope_post_bindings[class_scope_index] = post_binding
        self.code_scope_indices.append(class_scope_index)
        for statement in node.body:
            self.visit(statement)
        self.code_scope_indices.pop()
        self.class_scope_post_bindings.pop(class_scope_index, None)
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings
        if type_bindings:
            self._pop_scope()
        self._bind(
            node.name,
            _BootstrapBinding(f"{self.module_name}.{node.name}", True, True, False, False),
        )

    def visit_Call(self, node: ast.Call) -> None:
        binding = self._resolve(node.func)
        self.calls.append(
            _BootstrapCall(
                node,
                binding.qualified,
                binding.direct,
                binding.tainted
                and (binding.qualified is None or binding.qualified.startswith("local:")),
                _bootstrap_spelling(node.func),
            )
        )
        self.generic_visit(node)


def _bootstrap_calls(path: Path) -> tuple[_BootstrapCall, ...]:
    scanner = _BootstrapScanner(_bootstrap_module_name(path))
    scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return tuple(scanner.calls)


def _call_keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _bootstrap_production_paths() -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for root in (REPO_ROOT / "apps", REPO_ROOT / "packages", REPO_ROOT / "services")
                for path in root.rglob("*.py")
                if "src" in path.parts
                and "tests" not in path.parts
                and "__pycache__" not in path.parts
            ),
            key=lambda path: path.as_posix(),
        )
    )


def test_every_entrypoint_uses_one_runtime_bootstrap_and_only_rootless_logs_directly() -> None:
    expected_initializations = {
        "apps/api/src/jhin_api/main.py": ["api"],
        "services/agent_worker/src/jhin_agent_worker/main.py": ["agent-worker"],
        "services/tool_worker/src/jhin_tool_worker/main.py": ["tool-worker"],
        "services/event_worker/src/jhin_event_worker/main.py": ["event-worker"],
        "services/workflow_worker/src/jhin_workflow_worker/main.py": ["workflow-worker"],
        "services/sandbox_runner/src/jhin_sandbox_runner/main.py": [
            "sandbox-runner",
            "sandbox-runner",
        ],
        "packages/workflows/src/jhin_workflows/poller_health.py": ["temporal-poller-check"],
    }
    initializations: dict[str, list[ast.Call]] = {}
    configurations: dict[str, list[_BootstrapCall]] = {}
    failures: list[str] = []
    for path in _bootstrap_production_paths():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for scanned in _bootstrap_calls(path):
            if scanned.unresolved:
                failures.append(f"{relative}:{scanned.node.lineno}:unresolved:{scanned.spelling}")
            if scanned.qualified in _BOOTSTRAP_CALLS and not scanned.direct:
                failures.append(f"{relative}:{scanned.node.lineno}:indirect-alias")
            if scanned.qualified == "jhin_observability.initialize_observability":
                initializations.setdefault(relative, []).append(scanned.node)
            if scanned.qualified in {
                "jhin_observability.configure_json_logging",
                "jhin_observability.configure_logging",
                "jhin_observability.logging.configure_json_logging",
                "jhin_observability.logging.configure_logging",
            }:
                configurations.setdefault(relative, []).append(scanned)

    assert {path: len(calls) for path, calls in initializations.items()} == {
        path: len(services) for path, services in expected_initializations.items()
    }
    for relative, expected_services in expected_initializations.items():
        calls = initializations[relative]
        actual_services: list[str] = []
        for call in calls:
            assert len(call.args) == 1
            config_call = call.args[0]
            assert isinstance(config_call, ast.Call)
            service = _call_keyword(config_call, "service_name")
            assert isinstance(service, ast.Constant) and isinstance(service.value, str)
            actual_services.append(service.value)
            if relative != "packages/workflows/src/jhin_workflows/poller_health.py":
                processors = _call_keyword(config_call, "extra_log_processors")
                assert isinstance(processors, ast.Tuple)
                assert len(processors.elts) == 1
                assert isinstance(processors.elts[0], ast.Name)
                assert processors.elts[0].id == "redact_event_dict"
        assert actual_services == expected_services

    assert {path: len(calls) for path, calls in configurations.items()} == {
        "packages/observability/src/jhin_observability/bootstrap.py": 1,
        "services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py": 1,
    }
    rootless_call = configurations[
        "services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py"
    ][0].node
    service = _call_keyword(rootless_call, "service")
    assert isinstance(service, ast.Constant)
    assert service.value == "rootless-docker-transport"
    assert failures == []


def test_bootstrap_audit_rejects_rebinding_shadowing_suffixes_and_indirect_aliases(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    candidates = (
        "from jhin_observability import initialize_observability as initialize\n"
        "initialize(config)\n",
        "from jhin_observability import initialize_observability as initialize\n"
        "initialize = object()\ninitialize(config)\n",
        "import jhin_observability as observability\n"
        "observability.initialize_observability(config)\n",
        "import jhin_observability as observability\n"
        "observability = object()\nobservability.initialize_observability(config)\n",
        "from jhin_observability import initialize_observability\n"
        "def main(initialize_observability):\n initialize_observability(config)\n",
        "from jhin_observability import initialize_observability\n"
        "helper = initialize_observability\nhelper(config)\n",
        "def main():\n not_initialize_observability(config)\n",
        "from jhin_observability import configure_json_logging\n"
        "configure_json_logging(service='bad', environment='test')\n",
        "from jhin_observability import initialize_observability\n"
        "helper = (alias := initialize_observability)\n"
        "alias(config)\n",
        "from jhin_observability import initialize_observability\n"
        "del initialize_observability\n"
        "initialize_observability(config)\n",
        "from jhin_observability import initialize_observability\n"
        "for initialize_observability in (object(),):\n pass\n"
        "initialize_observability(config)\n",
        "from jhin_observability import initialize_observability\n"
        "with manager() as initialize_observability:\n pass\n"
        "initialize_observability(config)\n",
        "from jhin_observability import initialize_observability\n"
        "try:\n raise RuntimeError\n"
        "except RuntimeError as initialize_observability:\n pass\n"
        "initialize_observability(config)\n",
        "from jhin_observability import configure_json_logging as configure\n"
        "configure(service='bad', environment='test')\n",
        "from jhin_observability import initialize_observability\n"
        "def main(config):\n"
        " return (lambda initialize_observability: initialize_observability(config))(\n"
        "  initialize_observability\n"
        " )\n",
        "from jhin_observability import initialize_observability\n"
        "def main(config, values):\n"
        " return [initialize_observability(config)\n"
        "  for initialize_observability in values]\n",
        "from jhin_observability import initialize_observability\n"
        "def main(config, values):\n"
        " return {initialize_observability(config)\n"
        "  for initialize_observability in values}\n",
        "from jhin_observability import initialize_observability\n"
        "def main(config, values):\n"
        " return (initialize_observability(config)\n"
        "  for initialize_observability in values)\n",
        "from jhin_observability import initialize_observability\n"
        "def main(config, values):\n"
        " return {initialize_observability: initialize_observability(config)\n"
        "  for initialize_observability in values}\n",
        "from jhin_observability import initialize_observability\n"
        "(*initialize_observability,) = values\n"
        "initialize_observability(config)\n",
    )
    for candidate in candidates:
        source.write_text(candidate, encoding="utf-8")
        calls = _bootstrap_calls(source)
        canonical = [call for call in calls if call.qualified in _BOOTSTRAP_CALLS]
        unresolved = [call for call in calls if call.unresolved]
        indirect = [call for call in canonical if not call.direct]
        missing_initialize = not any(
            call.qualified == "jhin_observability.initialize_observability" and call.direct
            for call in calls
        )
        forbidden_config = any(
            call.qualified
            in {
                "jhin_observability.configure_json_logging",
                "jhin_observability.configure_logging",
            }
            for call in calls
        )
        assert unresolved or indirect or missing_initialize or forbidden_config

    source.write_text(
        "def main(config):\n"
        " from jhin_observability import initialize_observability\n"
        " return initialize_observability(config)\n",
        encoding="utf-8",
    )
    assert any(
        call.qualified == "jhin_observability.initialize_observability" and call.direct
        for call in _bootstrap_calls(source)
    )

    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "def main(config, values):\n"
        " [None for initialize_observability in values]\n"
        " return initialize_observability(config)\n",
        encoding="utf-8",
    )
    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[0].direct
    assert not initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "expression",
    [
        "[(initialize_observability := other) for other in values]",
        "{(initialize_observability := other) for other in values}",
        "{other: (initialize_observability := other) for other in values}",
        "((initialize_observability := other) for other in values)",
    ],
)
def test_bootstrap_audit_tracks_comprehension_walrus_in_enclosing_scope(
    expression: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def main(config, values):\n"
        " from jhin_observability import initialize_observability\n"
        f" {expression}\n"
        " return initialize_observability(config)\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "pattern",
    [
        "initialize_observability",
        "[*initialize_observability]",
        '{"business": _, **initialize_observability}',
        (
            "[Holder(value=initialize_observability)]"
            ' | {"runtime": Holder(value=initialize_observability)}'
        ),
    ],
)
def test_bootstrap_audit_rejects_structural_pattern_capture_aliases(
    pattern: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def main(config, value):\n"
        " from jhin_observability import initialize_observability\n"
        " match value:\n"
        f"  case {pattern}:\n"
        "   return initialize_observability(config)\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


def test_bootstrap_audit_does_not_bind_match_wildcard(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def main(config, value):\n"
        " from jhin_observability import initialize_observability\n"
        " match value:\n"
        "  case _:\n"
        "   return initialize_observability(config)\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[0].direct
    assert not initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "candidate",
    [
        (
            "from jhin_observability import initialize_observability\n"
            "def mutate():\n"
            " global initialize_observability\n"
            " initialize_observability = initialize_observability\n"
            "def main(config):\n"
            " mutate()\n"
            " return initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "def main(config):\n"
            " mutate()\n"
            " return initialize_observability(config)\n"
            "def mutate():\n"
            " global initialize_observability\n"
            " initialize_observability = initialize_observability\n"
        ),
        (
            "def main(config):\n"
            " from jhin_observability import initialize_observability\n"
            " def mutate():\n"
            "  nonlocal initialize_observability\n"
            "  initialize_observability = initialize_observability\n"
            " mutate()\n"
            " return initialize_observability(config)\n"
        ),
    ],
)
def test_bootstrap_audit_rejects_declared_scope_authority_mutation(
    candidate: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(candidate, encoding="utf-8")

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "candidate",
    [
        (
            "from jhin_observability import initialize_observability\n"
            "type initialize_observability = object\n"
            "initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "def shadow[initialize_observability](config):\n"
            " return initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "class Shadow[initialize_observability]:\n"
            " value = initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "type Alias[initialize_observability] = initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "type Alias[**initialize_observability] = initialize_observability(config)\n"
        ),
        (
            "from jhin_observability import initialize_observability\n"
            "type Alias[*initialize_observability] = initialize_observability(config)\n"
        ),
    ],
)
def test_bootstrap_audit_rejects_pep695_authority_shadowing(
    candidate: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(candidate, encoding="utf-8")

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "scoped_declaration",
    [
        (
            "def shadow[initialize_observability](config):\n"
            " return initialize_observability(config)\n"
        ),
        ("class Shadow[initialize_observability]:\n value = initialize_observability(config)\n"),
        "type Shadow[initialize_observability] = initialize_observability(config)\n",
    ],
)
def test_bootstrap_audit_pep695_type_parameters_do_not_leak(
    scoped_declaration: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        f"{scoped_declaration}"
        "initialize_observability(config)\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 2
    assert initialize_calls[0].unresolved
    assert initialize_calls[1].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[1].direct
    assert not initialize_calls[1].unresolved


def test_bootstrap_audit_rejects_relative_authority_import(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from .jhin_observability import initialize_observability\n"
        "initialize_observability(config)\n",
        encoding="utf-8",
    )
    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "member",
    [
        " def main(self, config):\n  return initialize_observability(config)\n",
        " value = (lambda config: initialize_observability(config))(config)\n",
        " value = [initialize_observability(config) for _ in ()]\n",
    ],
)
def test_bootstrap_audit_skips_class_namespace_for_nested_code_lookup(
    member: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "initialize_observability = object()\n"
        "class Container:\n"
        " from jhin_observability import initialize_observability\n" + member,
        encoding="utf-8",
    )
    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "definition",
    [
        ("@initialize_observability(config)\ndef initialize_observability():\n pass\n"),
        ("def initialize_observability(\n value=initialize_observability(config)\n):\n pass\n"),
        ("def initialize_observability(\n value: initialize_observability(config)\n):\n pass\n"),
        ("@initialize_observability(config)\nclass initialize_observability:\n pass\n"),
        ("class initialize_observability(initialize_observability(config)):\n pass\n"),
        (
            "class initialize_observability(\n"
            " metaclass=initialize_observability(config)\n"
            "):\n pass\n"
        ),
        ("class initialize_observability:\n value = initialize_observability(config)\n"),
    ],
)
def test_bootstrap_audit_rejects_same_name_definition_authority_calls(
    definition: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        f"from jhin_observability import initialize_observability\n{definition}",
        encoding="utf-8",
    )
    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[0].direct
    assert not initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "member",
    [
        " def invoke(self, config):\n  return initialize_observability(config)\n",
        " invoke = lambda config: initialize_observability(config)\n",
        " invocations = (initialize_observability(config) for _ in ())\n",
    ],
)
def test_bootstrap_audit_uses_post_class_binding_in_deferred_code(
    member: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "class initialize_observability:\n" + member,
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved


@pytest.mark.parametrize(
    "assignment",
    [
        "value: initialize_observability(config)\n",
        "value: initialize_observability(config) = None\n",
        "class Container:\n value: initialize_observability(config)\n",
        "class Container:\n value: initialize_observability(config) = None\n",
    ],
)
def test_bootstrap_audit_scans_annotated_assignment_authority_calls(
    assignment: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n" + assignment,
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"


@pytest.mark.parametrize(
    "body",
    [
        (
            "value: (initialize_observability := replacement) = "
            "initialize_observability(config)\n"
            "initialize_observability(config)\n"
        ),
        (
            "class Container:\n"
            " value: (initialize_observability := replacement) = "
            "initialize_observability(config)\n"
            " initialize_observability(config)\n"
        ),
    ],
)
def test_bootstrap_audit_applies_annassign_walrus_after_value(
    body: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\nreplacement = object()\n" + body,
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 2
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[0].direct
    assert initialize_calls[1].unresolved


@pytest.mark.parametrize(
    "hidden_definition",
    [
        "@initialize_observability(config)\ndef child():\n pass\n",
        "def child(value=initialize_observability(config)):\n pass\n",
        "class Child(metaclass=initialize_observability(config)):\n pass\n",
    ],
)
def test_bootstrap_audit_scans_definition_time_authority_calls(
    hidden_definition: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "initialize_observability(config)\n"
        f"{hidden_definition}",
        encoding="utf-8",
    )

    initialize_calls = [
        call
        for call in _bootstrap_calls(source)
        if call.qualified == "jhin_observability.initialize_observability"
    ]
    assert len(initialize_calls) == 2


def test_bootstrap_audit_predeclares_definition_time_walrus(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "def outer(config, replacement):\n"
        " initialize_observability(config)\n"
        " @(initialize_observability := replacement)\n"
        " def child():\n"
        "  pass\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _bootstrap_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved
