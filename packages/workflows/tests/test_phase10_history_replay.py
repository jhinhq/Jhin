from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from jhin_observability import noop_tracer
from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    ExecuteBoundToolInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
    RunStepInput,
)
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.heartbeat import HeartbeatWorkflow
from jhin_workflows.tool_compat import (
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
)
from jhin_workflows.triggered_task import TriggeredTaskWorkflow

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "phase9_temporal"
EXPECTED_PHASE9_REF = "6318781b57692bf39f37cd428d73de115d7458e2"
EXPECTED_TEMPORAL_SDK_VERSION = "1.31.0"
_TERMINAL_WORKFLOW_EVENTS = frozenset(
    {
        "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW",
    }
)
_EVIDENCE_PATTERN = re.compile(
    r"<!-- phase9-evidence:start -->\s*```json\s*(\{.*\})\s*```\s*"
    r"<!-- phase9-evidence:end -->",
    re.DOTALL,
)

EXPECTED_OLD_ACTIVITIES = {
    "agent-tool-step.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "agent-post-bind-pre-effect.json": {"resolve_snapshot", "run_agent_step"},
    "agent-parked-approval.json": {"resolve_snapshot", "run_agent_step"},
    "agent-finalization.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "triggered-sync.json": {"prepare_triggered_task", "sync_external"},
    "engineering-sync.json": {"prepare_triggered_task", "sync_external"},
}
_PHASE10_PATCH_MARKERS = (
    "phase10-tool-worker-boundary-v1",
    "phase10-trigger-sync-tool-routing-v1",
    "phase10-engineering-sync-tool-routing-v1",
)


def _copy_fixture_root(tmp_path: Path) -> Path:
    destination = tmp_path / "phase9_temporal"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _fixture_history(fixture: Path, *, workflow_id: str) -> WorkflowHistory:
    return WorkflowHistory.from_json(
        workflow_id,
        fixture.read_text(encoding="utf-8"),
    )


def _committed_evidence(root: Path) -> dict[str, Any]:
    match = _EVIDENCE_PATTERN.search((root / "README.md").read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("committed Phase 9 evidence manifest is missing")
    evidence = json.loads(match.group(1))
    if not isinstance(evidence, dict):
        raise ValueError("committed Phase 9 evidence manifest is malformed")
    return cast(dict[str, Any], evidence)


def assert_frozen_history_evidence(root: Path) -> None:
    evidence = _committed_evidence(root)
    source_ref = evidence.get("source_ref")
    if source_ref != EXPECTED_PHASE9_REF:
        raise ValueError("committed source ref does not match the Phase 9 barrier ref")
    if (root / "phase9-ref.txt").read_text(encoding="utf-8") != f"{source_ref}\n":
        raise ValueError("phase9-ref.txt source ref does not match committed evidence")
    sdk_version = evidence.get("temporal_sdk_version")
    if sdk_version != EXPECTED_TEMPORAL_SDK_VERSION:
        raise ValueError("committed Temporal SDK version is incorrect")

    expected_fixtures = evidence.get("fixtures")
    if not isinstance(expected_fixtures, dict):
        raise ValueError("committed fixture evidence is malformed")
    actual_names = {path.name for path in root.glob("*.json")}
    if actual_names != set(expected_fixtures):
        raise ValueError("committed fixture set does not match exact evidence")

    for filename, raw_expected in expected_fixtures.items():
        if not isinstance(filename, str) or not isinstance(raw_expected, dict):
            raise ValueError("committed fixture entry is malformed")
        fixture = root / filename
        raw = fixture.read_bytes()
        document = json.loads(raw)
        if set(document) != {"events"} or not isinstance(document["events"], list):
            raise ValueError(f"{filename} is not an SDK-only history document")
        events = document["events"]
        if not events:
            raise ValueError(f"{filename} has no history events")

        started = events[0].get("workflowExecutionStartedEventAttributes", {})
        workflow_type = started.get("workflowType", {}).get("name")
        task_queue = started.get("taskQueue", {}).get("name")
        if workflow_type != raw_expected.get("workflow_type"):
            raise ValueError(f"{filename} workflow type metadata drifted")
        if task_queue != raw_expected.get("task_queue"):
            raise ValueError(f"{filename} task queue metadata drifted")

        sdk_versions = {
            event.get("workflowTaskCompletedEventAttributes", {})
            .get("sdkMetadata", {})
            .get("sdkVersion")
            for event in events
            if event.get("workflowTaskCompletedEventAttributes", {})
            .get("sdkMetadata", {})
            .get("sdkVersion")
        }
        if sdk_versions != {sdk_version}:
            raise ValueError(f"{filename} SDK version metadata drifted")
        if len(events) != raw_expected.get("event_count"):
            raise ValueError(f"{filename} exact event count drifted")
        if events[-1].get("eventType") != raw_expected.get("last_event_type"):
            raise ValueError(f"{filename} exact end state drifted")
        closed = any(event.get("eventType") in _TERMINAL_WORKFLOW_EVENTS for event in events)
        if closed is not raw_expected.get("closed"):
            raise ValueError(f"{filename} closed/open end state drifted")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != raw_expected.get("sha256"):
            raise ValueError(f"{filename} digest drifted")


def test_tool_queue_name_is_stable() -> None:
    assert TOOL_TASK_QUEUE == "jhin-tool-queue"


def test_tool_worker_contracts_are_dependency_light_and_preserve_caller_fields() -> None:
    advertised = AdvertisedTool(
        name="linear.issue.get",
        description="Fetch one issue",
        parameters={"type": "object"},
    )
    base = RunStepInput(
        workspace_id="workspace",
        task_id="task",
        run_id="run",
        agent_id="agent",
        snapshot_json="{}",
        step_index=2,
    )

    assert ResolveAdvertisedToolsInput("workspace", "agent").agent_id == "agent"
    assert ReasonAgentStepInput(**vars(base), advertised_tools=[advertised]).advertised_tools == [
        advertised
    ]
    assert ReasonAgentStepResult(call_count=1).call_count == 1
    assert ExecuteBoundToolInput("workspace", "run", 2, 0).ordinal == 0
    assert BoundToolResult("tool-call", "completed").approval_id is None
    commit = CommitAgentStepInput("workspace", "task", "run", "agent", 2)
    assert commit.gateway_tool_call_ids == []
    assert commit.cancelled_after_tool_call_id is None
    approval = ResolveBoundToolApprovalInput("workspace", "task", "run", "agent", "approval")
    assert approval.approval_id == "approval"
    assert (
        CommitApprovalProjectionInput(
            "workspace", "task", "run", "agent", "approval", "tool-call"
        ).tool_call_id
        == "tool-call"
    )
    assert CleanupRunWorkspaceInput("workspace", "run").run_id == "run"
    assert CleanupRunWorkspaceResult(deleted=True).deleted is True


def test_frozen_histories_have_only_phase9_commands() -> None:
    for filename, names in EXPECTED_OLD_ACTIVITIES.items():
        text = (FIXTURE_ROOT / filename).read_text(encoding="utf-8")
        recorded = set(re.findall(r'"activityType"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', text))
        assert names.issubset(recorded)
        assert all(marker not in text for marker in _PHASE10_PATCH_MARKERS)


def test_committed_frozen_history_evidence_matches_exact_bytes_and_metadata() -> None:
    assert_frozen_history_evidence(FIXTURE_ROOT)


def test_committed_evidence_rejects_mutated_fixture(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    fixture = root / "agent-tool-step.json"
    fixture.write_text(
        fixture.read_text(encoding="utf-8").replace('"eventId": "1"', '"eventId": "99"', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="digest"):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_replaced_fixture(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    (root / "agent-tool-step.json").write_bytes((root / "agent-finalization.json").read_bytes())

    with pytest.raises(ValueError):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_mutated_phase9_ref(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    (root / "phase9-ref.txt").write_text("0" * 40 + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source ref"):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_mutated_sdk_metadata(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    fixture = root / "agent-tool-step.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    completed = next(
        event
        for event in document["events"]
        if "sdkMetadata" in event.get("workflowTaskCompletedEventAttributes", {})
    )
    completed["workflowTaskCompletedEventAttributes"]["sdkMetadata"]["sdkVersion"] = "9.9.9"
    fixture.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SDK version"):
        assert_frozen_history_evidence(root)


@pytest.mark.parametrize("fixture", sorted(FIXTURE_ROOT.glob("*.json")), ids=lambda p: p.name)
async def test_phase9_history_replays_with_phase10_workflows(fixture: Path) -> None:
    temporal_module = importlib.import_module("jhin_observability.temporal")
    workflow_id = f"phase9-replay-{fixture.stem}"
    history = _fixture_history(fixture, workflow_id=workflow_id)

    assert history.workflow_id == workflow_id
    await Replayer(
        workflows=[AgentTaskWorkflow, TriggeredTaskWorkflow, EngineeringTicketWorkflow],
        interceptors=[temporal_module.SafeTemporalTracingInterceptor(noop_tracer(), role="worker")],
    ).replay_workflow(history)


def _registered_workflow_import_closure() -> set[Path]:
    source_root = Path(__file__).parents[1] / "src" / "jhin_workflows"
    pending: list[Path] = []
    for workflow_type in (
        AgentTaskWorkflow,
        TriggeredTaskWorkflow,
        DelegatedTaskWorkflow,
        EngineeringTicketWorkflow,
        AdvertisedToolsCompatibilityWorkflow,
        ToolStepCompatibilityWorkflow,
        ApprovalCompatibilityWorkflow,
        SyncExternalCompatibilityWorkflow,
        CleanupCompatibilityWorkflow,
        HeartbeatWorkflow,
    ):
        module = inspect.getmodule(workflow_type)
        assert module is not None and module.__file__ is not None
        pending.append(Path(module.__file__).resolve())
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited or path.name == "poller_health.py":
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        for module_name in modules:
            if not module_name.startswith("jhin_workflows"):
                continue
            relative = module_name.removeprefix("jhin_workflows").lstrip(".")
            candidate = source_root.joinpath(*relative.split(".")) if relative else source_root
            if candidate.is_dir():
                candidate = candidate / "__init__.py"
            else:
                candidate = candidate.with_suffix(".py")
            if candidate.exists() and "activities.py" not in candidate.name:
                pending.append(candidate.resolve())
    return visited


_DATETIME_MODULE = "datetime_module"
_DATETIME_CLASS = "datetime_class"
_DATETIME_OTHER = "other"
_FORBIDDEN_WORKFLOW_IMPORTS = frozenset({"jhin_observability", "opentelemetry", "random", "time"})


def _determinism_pattern_names(pattern: ast.pattern) -> frozenset[str]:
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


class _DeterminismLocalNames(ast.NodeVisitor):
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

    def _visit_function_definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if not node.type_params:
            parameters = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            for parameter in parameters:
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.args.vararg is not None and node.args.vararg.annotation is not None:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
                self.visit(node.args.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_definition(node)

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
            self.names.update(_determinism_pattern_names(case.pattern))
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


class _WorkflowDeterminismScanner(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.failures: list[str] = []
        self.scopes: list[dict[str, str]] = [{}]
        self.scope_kinds: list[str] = ["module"]
        self.hidden_class_scope_indices: set[int] = set()
        self.class_scope_post_bindings: dict[int, tuple[str, int]] = {}
        self.deferred_post_class_bindings: list[tuple[str, int]] = []
        self.code_scope_indices: list[int] = [0]

    def _record(self, node: ast.AST, kind: str) -> None:
        self.failures.append(f"{self.path}:{node.lineno}:{kind}")

    def _push_scope(self, scope: dict[str, str], kind: str) -> int:
        self.scopes.append(scope)
        self.scope_kinds.append(kind)
        return len(self.scopes) - 1

    def _pop_scope(self) -> dict[str, str]:
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

    def _lookup(self, name: str) -> str:
        binding = _DATETIME_OTHER
        binding_scope_index: int | None = None
        for index in range(len(self.scopes) - 1, -1, -1):
            if index in self.hidden_class_scope_indices:
                continue
            if name in self.scopes[index]:
                binding = self.scopes[index][name]
                binding_scope_index = index
                break
        post_binding_scope_indices = [
            scope_index
            for pending_name, scope_index in self.deferred_post_class_bindings
            if pending_name == name
        ]
        if post_binding_scope_indices and (
            binding_scope_index is None or binding_scope_index <= max(post_binding_scope_indices)
        ):
            return _DATETIME_OTHER
        return binding

    def _resolve(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            if self._resolve(node.value) == _DATETIME_MODULE and node.attr == "datetime":
                return _DATETIME_CLASS
            return _DATETIME_OTHER
        if isinstance(node, ast.NamedExpr):
            return self._resolve(node.value)
        return _DATETIME_OTHER

    def _bind_target(
        self,
        target: ast.AST,
        value: ast.AST | None,
        *,
        scope_index: int = -1,
    ) -> None:
        actual_scope_index = scope_index if scope_index >= 0 else len(self.scopes) - 1
        if isinstance(target, ast.Name):
            self.scopes[actual_scope_index][target.id] = self._resolve(value)
            return
        if isinstance(target, ast.Starred):
            self._bind_target(target.value, None, scope_index=actual_scope_index)
            return
        if not isinstance(target, (ast.Tuple, ast.List)):
            return
        target_items = list(target.elts)
        value_items = list(value.elts) if isinstance(value, (ast.Tuple, ast.List)) else []
        starred_indices = [
            index for index, item in enumerate(target_items) if isinstance(item, ast.Starred)
        ]
        if not starred_indices and len(target_items) == len(value_items):
            for target_item, value_item in zip(target_items, value_items, strict=True):
                self._bind_target(
                    target_item,
                    value_item,
                    scope_index=actual_scope_index,
                )
            return
        if len(starred_indices) == 1 and len(value_items) >= len(target_items) - 1:
            starred_index = starred_indices[0]
            suffix_count = len(target_items) - starred_index - 1
            for index in range(starred_index):
                self._bind_target(
                    target_items[index],
                    value_items[index],
                    scope_index=actual_scope_index,
                )
            self._bind_target(
                target_items[starred_index],
                None,
                scope_index=actual_scope_index,
            )
            for offset in range(1, suffix_count + 1):
                self._bind_target(
                    target_items[-offset],
                    value_items[-offset],
                    scope_index=actual_scope_index,
                )
            return
        for target_item in target_items:
            self._bind_target(target_item, None, scope_index=actual_scope_index)

    @staticmethod
    def _single_iter_value(iterable: ast.AST) -> ast.AST | None:
        if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)) and len(iterable.elts) == 1:
            return iterable.elts[0]
        return None

    def visit_Import(self, node: ast.Import) -> None:
        roots = {alias.name.split(".", 1)[0] for alias in node.names}
        if roots & _FORBIDDEN_WORKFLOW_IMPORTS:
            self._record(node, "import")
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            binding = _DATETIME_MODULE if alias.name == "datetime" else _DATETIME_OTHER
            self.scopes[-1][name] = binding

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is not None and (
            node.module.split(".", 1)[0] in _FORBIDDEN_WORKFLOW_IMPORTS
        ):
            self._record(node, "from")
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            binding = (
                _DATETIME_CLASS
                if node.level == 0 and node.module == "datetime" and alias.name == "datetime"
                else _DATETIME_OTHER
            )
            self.scopes[-1][name] = binding

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._bind_target(node.target, node.value)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind_target(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target(
            node.target,
            node.value,
            scope_index=self.code_scope_indices[-1],
        )

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._bind_target(target, None)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_target(node.target, self._single_iter_value(node.iter))
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self.scopes[-1][node.name] = _DATETIME_OTHER
        for statement in node.body:
            self.visit(statement)
        if node.name is not None:
            self.scopes[-1][node.name] = _DATETIME_OTHER

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            for name in _determinism_pattern_names(case.pattern):
                self.scopes[-1][name] = _DATETIME_OTHER
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
        type_bindings = {parameter.name: _DATETIME_OTHER for parameter in node.type_params}
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
        self.scopes[-1][node.name] = _DATETIME_OTHER
        collector = _DeterminismLocalNames()
        for statement in node.body:
            collector.visit(statement)
        scope = dict.fromkeys(collector.names, _DATETIME_OTHER)
        scope.update(type_bindings)
        scope.update({parameter.arg: _DATETIME_OTHER for parameter in parameters})
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
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        collector = _DeterminismLocalNames()
        collector.visit(node.body)
        scope = dict.fromkeys(collector.names, _DATETIME_OTHER)
        scope.update({parameter.arg: _DATETIME_OTHER for parameter in parameters})
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
        collector = _DeterminismLocalNames()
        for generator in generators:
            collector.visit(generator.target)
        scope = dict.fromkeys(collector.names, _DATETIME_OTHER)
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        self._push_scope(scope, "comprehension")
        self._bind_target(first.target, self._single_iter_value(first.iter))
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator.iter)
            self._bind_target(generator.target, self._single_iter_value(generator.iter))
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
        type_scope = {parameter.name: _DATETIME_OTHER for parameter in node.type_params}
        scope_index = self._push_scope(type_scope, "type_params")
        self.code_scope_indices.append(scope_index)
        for type_parameter in node.type_params:
            self.visit(type_parameter)
        self.visit(node.value)
        self.code_scope_indices.pop()
        self._pop_scope()
        self._bind_target(node.name, None)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent_scope_index = len(self.scopes) - 1
        post_binding = (
            None
            if self.scope_kinds[parent_scope_index] == "class"
            else (node.name, parent_scope_index)
        )
        for decorator in node.decorator_list:
            self.visit(decorator)
        type_bindings = {parameter.name: _DATETIME_OTHER for parameter in node.type_params}
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
        self.scopes[-1][node.name] = _DATETIME_OTHER

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "now"
            and self._resolve(node.func.value) == _DATETIME_CLASS
        ):
            self._record(node, "datetime.now")
        self.generic_visit(node)


def _workflow_determinism_failures(paths: set[Path]) -> list[str]:
    failures: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = _WorkflowDeterminismScanner(path)
        scanner.visit(tree)
        failures.extend(scanner.failures)
    return failures


def test_registered_workflow_import_closure_is_deterministic_and_telemetry_free() -> None:
    closure = _registered_workflow_import_closure()
    assert closure
    assert all(path.name != "poller_health.py" for path in closure)
    assert _workflow_determinism_failures(closure) == []


@pytest.mark.parametrize(
    "source",
    [
        "import jhin_observability as telemetry\ntelemetry = object()\n",
        "from opentelemetry import trace as tracing\ntracing = object()\n",
        "import random as deterministic_random\ndeterministic_random = object()\n",
        "from time import monotonic as clock\nclock = object()\n",
        "from datetime import datetime\ndatetime.now()\n",
        "from datetime import datetime as clock\nclock.now()\n",
        "from datetime import datetime\nclock = datetime\nclock.now()\n",
        "from datetime import datetime\n(alias := datetime)\nalias.now()\n",
        "import datetime as dt\nclock = dt.datetime\nclock.now()\n",
        "import datetime as dt\nmodule = dt\nmodule.datetime.now()\n",
        "from datetime import datetime\nclock, = (datetime,)\nclock.now()\n",
        (
            "from datetime import datetime\n"
            "head, *discard, clock = object(), object(), datetime\n"
            "clock.now()\n"
        ),
        ("from datetime import datetime\nfor clock in (datetime,):\n pass\nclock.now()\n"),
        ("from datetime import datetime\nalias = datetime\ndef workflow():\n return alias.now()\n"),
        (
            "from datetime import datetime\n"
            "class Workflow:\n"
            " datetime = object()\n"
            " def run(self):\n  return datetime.now()\n"
        ),
        "from datetime import datetime\n[datetime.now() for _ in ()]\n",
        ("from datetime import datetime\n[(clock := datetime) for _ in ()]\nclock.now()\n"),
    ],
)
def test_workflow_determinism_audit_rejects_forbidden_authority_mutations(
    source: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.py"
    path.write_text(source, encoding="utf-8")
    assert _workflow_determinism_failures({path})


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\ndatetime = object()\ndatetime.now()\n",
        "def workflow(datetime):\n return datetime.now()\n",
        (
            "from datetime import datetime\n"
            "def workflow():\n"
            " datetime = object()\n"
            " return datetime.now()\n"
        ),
        ("from datetime import datetime\n(datetime := object())\ndatetime.now()\n"),
        ("from datetime import datetime\nfor datetime in values:\n pass\ndatetime.now()\n"),
        (
            "from datetime import datetime\n"
            "with manager() as datetime:\n datetime.now()\n"
            "datetime.now()\n"
        ),
        (
            "from datetime import datetime\n"
            "try:\n pass\n"
            "except Exception as datetime:\n datetime.now()\n"
            "datetime.now()\n"
        ),
        (
            "from datetime import datetime\n"
            "match value:\n"
            " case datetime:\n  datetime.now()\n"
            "datetime.now()\n"
        ),
        ("from datetime import datetime\n[datetime.now() for datetime in values]\n"),
        (
            "from datetime import datetime\n"
            "class datetime:\n"
            " def run(self):\n  return datetime.now()\n"
        ),
        ("from datetime import datetime\n(lambda datetime: datetime.now())(object())\n"),
        "from datetime import datetime\ndatetime.utcnow()\n",
        "class Clock:\n def now(self):\n  return None\nclock = Clock()\nclock.now()\n",
    ],
)
def test_workflow_determinism_audit_allows_rebound_and_near_miss_calls(
    source: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.py"
    path.write_text(source, encoding="utf-8")
    assert _workflow_determinism_failures({path}) == []


def test_workflow_determinism_comprehension_target_does_not_leak(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.py"
    path.write_text(
        "from datetime import datetime\n[value for datetime in values]\ndatetime.now()\n",
        encoding="utf-8",
    )
    failures = _workflow_determinism_failures({path})
    assert len(failures) == 1
    assert failures[0].endswith(":datetime.now")
