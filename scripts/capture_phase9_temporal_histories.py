"""Capture immutable Phase 9 Temporal histories before the Phase 10 patch.

This is an intentionally one-shot upgrade fixture generator. It runs the real
Phase 9 workflow definitions and activity implementations against one Temporal
test environment, while replacing only model/provider and external-effect
edges with deterministic in-process capture doubles.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import jhin_agent_worker.activities as agent_activities_module  # type: ignore[import-untyped]
import jhin_agent_worker.trigger_activities as trigger_activities_module  # type: ignore[import-untyped]
from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.engineering_activities import (  # type: ignore[import-untyped]
    EngineeringActivities,
)
from jhin_agent_worker.resources import Resources  # type: ignore[import-untyped]
from jhin_agent_worker.trigger_activities import TriggerActivities
from jhin_agents.snapshot import (  # type: ignore[import-untyped]
    AgentExecutionSnapshot,
    ModelProfileSnapshot,
    RunLimits,
)
from jhin_connectors.linear.schemas import CommentCreateOutput
from jhin_db import create_engine, create_session_factory
from jhin_db.migrate import upgrade_to_head
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    ModelProfile,
    ModelProvider,
    RunEvent,
    Task,
    ToolCall,
    Trigger,
    TriggerInvocation,
    Workspace,
)
from jhin_domain import ApprovalStatus, RunStatus, TaskState, new_uuid7
from jhin_events import EventEnvelope
from jhin_models import (  # type: ignore[import-untyped]
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools import (
    PHASE9_AFTER_MANIFEST,
    PHASE9_CLEANUP_BEFORE_EFFECT,
    PHASE9_SYNC_BEFORE_EFFECT,
    CrashBarrier,
    CrashBarrierConfig,
    ToolCatalog,
    ToolExecutionContext,
    stable_tool_invocation_id,
)
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import (
    ACTIVITY_RESOLVE_SNAPSHOT,
    AgentTaskInput,
    AgentTaskWorkflow,
    SnapshotResult,
)
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketInput, EngineeringTicketWorkflow
from jhin_workflows.triggered_task import TriggeredTaskInput, TriggeredTaskWorkflow

SCENARIOS = (
    "agent-tool-step",
    "agent-post-bind-pre-effect",
    "agent-parked-approval",
    "agent-finalization",
    "triggered-sync",
    "engineering-sync",
)

DEFAULT_CAPTURE_DATABASE_URL = (
    "postgresql+asyncpg://jhin:jhin@127.0.0.1:55432/jhin"
)
TASK0_PHASE9_REF = "6318781b57692bf39f37cd428d73de115d7458e2"
PHASE10_PATCH_MARKER = "PHASE10_TOOL_WORKER_PATCH"
FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "workflows"
    / "tests"
    / "fixtures"
    / "phase9_temporal"
)

_READ_TOOL = "capture.phase9.read"
_APPROVAL_TOOL = "capture.phase9.destructive"


@dataclass(frozen=True)
class CapturedWorkflow:
    workflow_id: str
    handle: Any


@dataclass(frozen=True)
class _FrozenHistoryHandle:
    history: WorkflowHistory

    async def fetch_history(self) -> WorkflowHistory:
        return self.history


@dataclass(frozen=True)
class _SeededCapture:
    workspace_id: UUID
    agent_id: UUID
    snapshot: AgentExecutionSnapshot
    direct_tasks: dict[str, UUID]
    direct_runs: dict[str, UUID]
    triggered_input: TriggeredTaskInput
    engineering_input: EngineeringTicketInput


class _CaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _CaptureOutput(BaseModel):
    receipt: str


class _NullPublisher:
    async def publish(self, envelope: EventEnvelope) -> None:
        assert envelope.event_type


@dataclass
class _CaptureResources:
    session_factory: async_sessionmaker[AsyncSession]
    publisher: _NullPublisher
    test_barrier: CrashBarrier
    crypto: Any = None


class _CaptureModelClient:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        transcript = "\n".join(message.content for message in request.messages)
        has_tool_result = any(message.role == "tool" for message in request.messages)
        call: ModelToolCall | None = None
        if not has_tool_result and "CAPTURE_APPROVAL" in transcript:
            call = ModelToolCall(
                id="phase9-provider-approval-call",
                name=_APPROVAL_TOOL,
                arguments_json='{"value":"requires-human-approval"}',
            )
        elif not has_tool_result and any(
            marker in transcript for marker in ("CAPTURE_READ", "CAPTURE_POST_BIND")
        ):
            call = ModelToolCall(
                id="phase9-provider-read-call",
                name=_READ_TOOL,
                arguments_json='{"value":"phase9-canonical-argument"}',
            )

        return ModelResponse(
            text="invoke the Phase 9 tool" if call is not None else "Phase 9 capture complete",
            finish_reason="tool_calls" if call is not None else "stop",
            model="phase9-capture-model",
            usage=ModelUsage(input_tokens=7, output_tokens=3),
            latency_ms=1,
            provider_request_id="phase9-capture-provider-request",
            tool_calls=(call,) if call is not None else (),
        )

    async def close(self) -> None:
        return None


class _CaptureResolveSnapshot:
    def __init__(
        self,
        real_activities: AgentActivities,
        seeded_results: dict[str, SnapshotResult],
    ) -> None:
        self._real_activities = real_activities
        self._seeded_results = seeded_results

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot_activity(self, params: AgentTaskInput) -> SnapshotResult:
        seeded = self._seeded_results.get(params.task_id)
        if seeded is not None:
            return seeded
        return cast(
            SnapshotResult,
            await self._real_activities.resolve_snapshot_activity(params),
        )


async def save_history(handle: Any, destination: Path, *, workflow_id: str) -> None:
    fetched = await handle.fetch_history()
    reconstructed = WorkflowHistory(workflow_id, fetched.events)
    await asyncio.to_thread(
        destination.write_text,
        reconstructed.to_json() + "\n",
        encoding="utf-8",
    )


def _readme(source_ref: str) -> str:
    lines = (
        "# Frozen Phase 9 Temporal histories",
        "",
        f"These immutable SDK JSON histories were captured from `{source_ref}` before any",
        "Phase 10 workflow patch was added. The generator used Temporal Python SDK",
        "1.31.0 and supplied each original workflow ID explicitly when reconstructing",
        "`WorkflowHistory`; the JSON files intentionally contain only the SDK `events`",
        "document.",
        "",
        "| Fixture | Workflow type | Database state at capture |",
        "| --- | --- | --- |",
        (
            "| `agent-tool-step.json` | `AgentTaskWorkflow` | normal tool step and "
            "finalization completed |"
        ),
        (
            "| `agent-post-bind-pre-effect.json` | `AgentTaskWorkflow` | one lossless "
            "manifest; no reasoning, `ToolCall`, or effect |"
        ),
        (
            "| `agent-parked-approval.json` | `AgentTaskWorkflow` | pending `Approval` "
            "and pending-approval `ToolCall`; workflow open |"
        ),
        (
            "| `agent-finalization.json` | `AgentTaskWorkflow` | `finalize_run` started "
            "and parked before sandbox cleanup |"
        ),
        (
            "| `triggered-sync.json` | `TriggeredTaskWorkflow` | `sync_external` started "
            "and parked before connector dispatch |"
        ),
        (
            "| `engineering-sync.json` | `EngineeringTicketWorkflow` | ticket finalized; "
            "`sync_external` parked before connector dispatch |"
        ),
        "",
        "Expected legacy activity names are `resolve_snapshot`, `run_agent_step`,",
        "`resolve_approval`, `finalize_run`, `prepare_triggered_task`, `sync_external`,",
        "`resolve_engineering_plan`, and `finalize_engineering_ticket`. No file contains",
        "the Phase 10 patch marker or Phase 10 activity command names.",
    )
    return "\n".join(lines) + "\n"


async def generate(destination: Path, *, source_ref: str) -> None:
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    captures: dict[str, CapturedWorkflow] = await capture_scenarios()
    if tuple(captures) != SCENARIOS:
        raise RuntimeError("Phase 9 capture scenarios are incomplete or reordered")
    for scenario, captured in captures.items():
        fixture = destination / f"{scenario}.json"
        await save_history(captured.handle, fixture, workflow_id=captured.workflow_id)
        fixture_text = await asyncio.to_thread(fixture.read_text, encoding="utf-8")
        restored = WorkflowHistory.from_json(captured.workflow_id, fixture_text)
        if restored.workflow_id != captured.workflow_id:
            raise RuntimeError(f"caller workflow ID was not preserved for {scenario}")
    await asyncio.to_thread(
        (destination / "README.md").write_text,
        _readme(source_ref),
        encoding="utf-8",
    )
    await asyncio.to_thread(
        (destination / "phase9-ref.txt").write_text,
        f"{source_ref}\n",
        encoding="utf-8",
    )


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return completed.stdout.strip()


def validate_phase9_source() -> str:
    source_ref = _git("rev-parse", "HEAD")
    if source_ref != TASK0_PHASE9_REF:
        raise RuntimeError(
            f"Phase 9 capture requires {TASK0_PHASE9_REF}, found {source_ref}"
        )
    dirty = _git("status", "--porcelain", "--", "packages/workflows/src")
    if dirty:
        raise RuntimeError("Phase 9 workflow source is dirty; refusing to capture")
    workflow_root = Path(__file__).resolve().parents[1] / "packages" / "workflows" / "src"
    if any(
        PHASE10_PATCH_MARKER in path.read_text(encoding="utf-8")
        for path in workflow_root.rglob("*.py")
    ):
        raise RuntimeError("Phase 10 workflow patch is already present; refusing to capture")
    return source_ref


async def _seed_database(sessions: async_sessionmaker[AsyncSession]) -> _SeededCapture:
    suffix = new_uuid7().hex[:12]
    async with sessions() as session:
        workspace = Workspace(
            name="Phase 9 temporal capture",
            slug=f"phase9-temporal-capture-{suffix}",
        )
        session.add(workspace)
        await session.flush()
        provider = ModelProvider(
            workspace_id=workspace.id,
            type="phase9-capture",
            display_name=f"Phase 9 capture provider {suffix}",
            enabled=True,
        )
        session.add(provider)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_id=provider.id,
            model_name="phase9-capture-model",
            display_name=f"Phase 9 capture model {suffix}",
            supports_tools=True,
        )
        session.add(profile)
        await session.flush()
        workspace.default_model_profile_id = profile.id
        agent = Agent(
            workspace_id=workspace.id,
            name="Phase 9 capture agent",
            slug=f"phase9-capture-agent-{suffix}",
            model_profile_id=profile.id,
            max_steps=2,
            max_run_minutes=5,
            max_concurrent_runs=20,
        )
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=name,
                    scope_json={},
                    effect="allow",
                )
                for name in (_READ_TOOL, _APPROVAL_TOOL)
            ]
        )

        descriptions = {
            "agent-tool-step": "CAPTURE_READ: complete a normal Phase 9 tool step",
            "agent-post-bind-pre-effect": (
                "CAPTURE_POST_BIND: bind a canonical manifest before any effect"
            ),
            "agent-parked-approval": "CAPTURE_APPROVAL: park for a human approval",
            "agent-finalization": "CAPTURE_FINAL: complete and begin finalization",
        }
        direct_tasks: dict[str, UUID] = {}
        direct_runs: dict[str, UUID] = {}
        for scenario, description in descriptions.items():
            task = Task(
                workspace_id=workspace.id,
                title=f"Phase 9 capture: {scenario}",
                description=description,
                state=TaskState.RUNNING.value,
                assigned_agent_id=agent.id,
                correlation_id=new_uuid7(),
            )
            session.add(task)
            await session.flush()
            run = AgentRun(
                workspace_id=workspace.id,
                agent_id=agent.id,
                task_id=task.id,
                status=RunStatus.RUNNING.value,
                model_profile_id=profile.id,
                started_at=None,
                snapshot_hash="phase9-capture-snapshot",
            )
            session.add(run)
            await session.flush()
            session.add(
                RunEvent(
                    workspace_id=workspace.id,
                    run_id=run.id,
                    task_id=task.id,
                    seq=0,
                    event_type="run.started",
                    payload_json={"capture": scenario},
                )
            )
            direct_tasks[scenario] = task.id
            direct_runs[scenario] = run.id

        trigger_inputs: dict[str, TriggeredTaskInput] = {}
        for scenario in ("triggered-sync", "engineering-sync"):
            trigger = Trigger(
                workspace_id=workspace.id,
                name=f"Phase 9 {scenario}",
                enabled=True,
                event_type="connector.linear.issue.updated",
                target_agent_id=agent.id,
                action_config_json={"comment_back": True},
            )
            session.add(trigger)
            await session.flush()
            event_id = new_uuid7()
            invocation = TriggerInvocation(
                workspace_id=workspace.id,
                trigger_id=trigger.id,
                idempotency_key=f"phase9-{scenario}-{suffix}",
                event_id=event_id,
                workflow_id=f"phase9-{scenario}-{suffix}",
                status="started",
            )
            session.add(invocation)
            await session.flush()
            trigger_inputs[scenario] = TriggeredTaskInput(
                workspace_id=str(workspace.id),
                trigger_id=str(trigger.id),
                trigger_name=trigger.name,
                invocation_id=str(invocation.id),
                connection_id=str(new_uuid7()),
                event_id=str(event_id),
                event_type="connector.linear.issue.updated",
                external_source="linear",
                external_id=f"PHASE9-{scenario}-{suffix}",
                title=f"Phase 9 capture {scenario}",
                description=f"Complete child work for {scenario}",
                external_url=f"https://linear.example/{scenario}",
                agent_id=str(agent.id),
                comment_back=True,
            )

        await session.commit()

    snapshot = AgentExecutionSnapshot(
        agent_id=agent.id,
        workspace_id=workspace.id,
        name=agent.name,
        role_title="",
        system_prompt="",
        autonomy_level="balanced",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        model_profile=ModelProfileSnapshot(
            profile_id=profile.id,
            provider_id=provider.id,
            provider_type=provider.type,
            base_url=None,
            secret_id=None,
            model_name=profile.model_name,
            display_name=profile.display_name,
            input_cost_micros_per_million=0,
            output_cost_micros_per_million=0,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=2, max_run_minutes=5),
    )
    return _SeededCapture(
        workspace_id=workspace.id,
        agent_id=agent.id,
        snapshot=snapshot,
        direct_tasks=direct_tasks,
        direct_runs=direct_runs,
        triggered_input=trigger_inputs["triggered-sync"],
        engineering_input=EngineeringTicketInput(base=trigger_inputs["engineering-sync"]),
    )


def _catalog(effect_runs: set[UUID]) -> ToolCatalog:
    catalog = ToolCatalog()

    async def execute_read(
        context: ToolExecutionContext,
        payload: BaseModel,
    ) -> BaseModel:
        effect_runs.add(context.run_id)
        value = cast(_CaptureInput, payload).value
        return _CaptureOutput(receipt=f"read:{value}")

    async def execute_destructive(
        context: ToolExecutionContext,
        payload: BaseModel,
    ) -> BaseModel:
        effect_runs.add(context.run_id)
        value = cast(_CaptureInput, payload).value
        return _CaptureOutput(receipt=f"destructive:{value}")

    catalog.register(
        ToolDefinition(
            name=_READ_TOOL,
            description="Deterministic read used only for a Phase 9 history capture.",
            risk=RiskLevel.READ,
            input_model=_CaptureInput,
            output_model=_CaptureOutput,
            required_capability=_READ_TOOL,
        ),
        execute_read,
    )
    catalog.register(
        ToolDefinition(
            name=_APPROVAL_TOOL,
            description="Deterministic destructive call parked for Phase 9 approval capture.",
            risk=RiskLevel.DESTRUCTIVE,
            input_model=_CaptureInput,
            output_model=_CaptureOutput,
            required_capability=_APPROVAL_TOOL,
            supports_approval=True,
        ),
        execute_destructive,
    )
    return catalog


async def _wait_for_marker(root: Path, barrier_name: str) -> Path:
    directory = root / barrier_name
    async with asyncio.timeout(30):
        while True:
            markers = tuple(directory.glob("*.arrived")) if directory.is_dir() else ()
            if markers:
                return markers[0]
            await asyncio.sleep(0.02)


async def _wait_for_pending_approval(
    sessions: async_sessionmaker[AsyncSession],
    run_id: UUID,
) -> None:
    async with asyncio.timeout(30):
        while True:
            async with sessions() as session:
                count = await session.scalar(
                    select(func.count(Approval.id)).where(
                        Approval.run_id == run_id,
                        Approval.status == ApprovalStatus.PENDING.value,
                    )
                )
            if count == 1:
                return
            await asyncio.sleep(0.02)


async def _assert_post_bind_database_state(
    sessions: async_sessionmaker[AsyncSession],
    run_id: UUID,
) -> None:
    expected_invocation = stable_tool_invocation_id(run_id, 0, 0)
    async with sessions() as session:
        manifests = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
        )
        reasoning_count = await session.scalar(
            select(func.count(RunEvent.id)).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "agent.step.reasoning",
            )
        )
        tool_count = await session.scalar(
            select(func.count(ToolCall.id)).where(ToolCall.run_id == run_id)
        )
        expected_tool = await session.get(ToolCall, expected_invocation)
    if len(manifests) != 1:
        raise RuntimeError("post-bind capture must contain exactly one tool manifest")
    manifest = manifests[0].payload_json.get("manifest")
    calls = manifest.get("calls") if isinstance(manifest, dict) else None
    if not isinstance(calls, list) or len(calls) != 1 or calls[0].get("lossless") is not True:
        raise RuntimeError("post-bind capture manifest is not one lossless ordered call")
    if reasoning_count != 0 or tool_count != 0 or expected_tool is not None:
        raise RuntimeError("post-bind capture crossed the Phase 9 pre-effect boundary")


async def _fetch_all_before_close(
    handles: dict[str, tuple[str, WorkflowHandle[Any, Any]]],
) -> dict[str, CapturedWorkflow]:
    frozen: dict[str, CapturedWorkflow] = {}
    for scenario in SCENARIOS:
        workflow_id, handle = handles[scenario]
        history = await handle.fetch_history()
        frozen[scenario] = CapturedWorkflow(
            workflow_id=workflow_id,
            handle=_FrozenHistoryHandle(WorkflowHistory(workflow_id, history.events)),
        )
    return frozen


async def capture_scenarios() -> dict[str, CapturedWorkflow]:
    database_url = os.environ.get(
        "PHASE9_CAPTURE_DATABASE_URL", DEFAULT_CAPTURE_DATABASE_URL
    )
    engine = create_engine(database_url)
    sessions = create_session_factory(engine)
    seeded = await _seed_database(sessions)
    effect_runs: set[UUID] = set()
    catalog = _catalog(effect_runs)
    resources = _CaptureResources(
        session_factory=sessions,
        publisher=_NullPublisher(),
        test_barrier=CrashBarrier(CrashBarrierConfig()),
    )
    phase9_resources = cast(Resources, resources)
    agent_activities = AgentActivities(phase9_resources)
    trigger_activities = TriggerActivities(phase9_resources)
    engineering_activities = EngineeringActivities(phase9_resources)
    seeded_results = {
        str(task_id): SnapshotResult(
            run_id=str(seeded.direct_runs[scenario]),
            snapshot_json=seeded.snapshot.model_dump_json(),
            snapshot_hash=seeded.snapshot.snapshot_hash(),
            max_steps=seeded.snapshot.run_limits.max_steps,
        )
        for scenario, task_id in seeded.direct_tasks.items()
    }
    resolver = _CaptureResolveSnapshot(agent_activities, seeded_results)

    original_catalog = agent_activities_module.build_default_catalog
    original_model_factory = agent_activities_module.build_model_client
    original_cleanup = agent_activities_module.delete_sandbox_workspace
    original_linear_tools = trigger_activities_module.LINEAR_TOOLS

    async def delete_capture_workspace(workspace_key: str) -> bool:
        assert workspace_key.startswith("run-")
        return True

    async def execute_sync(
        context: ToolExecutionContext,
        payload: BaseModel,
    ) -> BaseModel:
        effect_runs.add(context.run_id)
        assert payload
        return CommentCreateOutput(
            comment_id="phase9-capture-comment",
            url="https://linear.example/phase9-capture-comment",
        )

    sync_definition = next(
        definition
        for definition, _executor in original_linear_tools
        if definition.name == "linear.comment.create"
    )
    agent_activities_module.build_default_catalog = lambda: catalog
    agent_activities_module.build_model_client = lambda *_args, **_kwargs: _CaptureModelClient()
    agent_activities_module.delete_sandbox_workspace = delete_capture_workspace
    trigger_activities_module.LINEAR_TOOLS = ((sync_definition, execute_sync),)

    try:
        env = await WorkflowEnvironment.start_time_skipping()
        agent_activities._temporal_client = env.client
        handles: dict[str, tuple[str, WorkflowHandle[Any, Any]]] = {}
        async with env:
            worker = Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[
                    AgentTaskWorkflow,
                    TriggeredTaskWorkflow,
                    DelegatedTaskWorkflow,
                    EngineeringTicketWorkflow,
                ],
                activities=[
                    resolver.resolve_snapshot_activity,
                    agent_activities.run_agent_step_activity,
                    agent_activities.resolve_approval_activity,
                    agent_activities.finalize_run_activity,
                    agent_activities.summarize_delegation_activity,
                    agent_activities.deliver_delegation_result_activity,
                    trigger_activities.prepare_triggered_task_activity,
                    trigger_activities.sync_external_activity,
                    engineering_activities.resolve_engineering_plan_activity,
                    engineering_activities.create_engineering_child_task_activity,
                    engineering_activities.finalize_engineering_ticket_activity,
                ],
            )
            async with worker:
                normal_id = f"phase9-agent-tool-step-{new_uuid7()}"
                normal = await env.client.start_workflow(
                    AgentTaskWorkflow.run,
                    AgentTaskInput(
                        workspace_id=str(seeded.workspace_id),
                        task_id=str(seeded.direct_tasks["agent-tool-step"]),
                        agent_id=str(seeded.agent_id),
                    ),
                    id=normal_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await normal.result()
                handles["agent-tool-step"] = (normal_id, normal)

                post_root = Path(os.environ.get("TMPDIR", "/tmp")) / f"phase9-post-{new_uuid7()}"
                post_run = seeded.direct_runs["agent-post-bind-pre-effect"]
                resources.test_barrier = CrashBarrier(
                    CrashBarrierConfig(
                        root=post_root,
                        selected=PHASE9_AFTER_MANIFEST,
                        match_identity=post_run,
                    )
                )
                post_id = f"phase9-agent-post-bind-pre-effect-{new_uuid7()}"
                post = await env.client.start_workflow(
                    AgentTaskWorkflow.run,
                    AgentTaskInput(
                        workspace_id=str(seeded.workspace_id),
                        task_id=str(seeded.direct_tasks["agent-post-bind-pre-effect"]),
                        agent_id=str(seeded.agent_id),
                    ),
                    id=post_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await _wait_for_marker(post_root, PHASE9_AFTER_MANIFEST)
                await _assert_post_bind_database_state(sessions, post_run)
                if post_run in effect_runs:
                    raise RuntimeError("post-bind tool effect started before fixture capture")
                handles["agent-post-bind-pre-effect"] = (post_id, post)

                resources.test_barrier = CrashBarrier(CrashBarrierConfig())
                approval_run = seeded.direct_runs["agent-parked-approval"]
                approval_id = f"phase9-agent-parked-approval-{new_uuid7()}"
                approval = await env.client.start_workflow(
                    AgentTaskWorkflow.run,
                    AgentTaskInput(
                        workspace_id=str(seeded.workspace_id),
                        task_id=str(seeded.direct_tasks["agent-parked-approval"]),
                        agent_id=str(seeded.agent_id),
                    ),
                    id=approval_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await _wait_for_pending_approval(sessions, approval_run)
                handles["agent-parked-approval"] = (approval_id, approval)

                finalize_root = (
                    Path(os.environ.get("TMPDIR", "/tmp")) / f"phase9-finalize-{new_uuid7()}"
                )
                finalize_run = seeded.direct_runs["agent-finalization"]
                resources.test_barrier = CrashBarrier(
                    CrashBarrierConfig(
                        root=finalize_root,
                        selected=PHASE9_CLEANUP_BEFORE_EFFECT,
                        match_identity=finalize_run,
                    )
                )
                finalize_id = f"phase9-agent-finalization-{new_uuid7()}"
                finalize = await env.client.start_workflow(
                    AgentTaskWorkflow.run,
                    AgentTaskInput(
                        workspace_id=str(seeded.workspace_id),
                        task_id=str(seeded.direct_tasks["agent-finalization"]),
                        agent_id=str(seeded.agent_id),
                    ),
                    id=finalize_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await _wait_for_marker(finalize_root, PHASE9_CLEANUP_BEFORE_EFFECT)
                handles["agent-finalization"] = (finalize_id, finalize)

                triggered_root = (
                    Path(os.environ.get("TMPDIR", "/tmp")) / f"phase9-triggered-{new_uuid7()}"
                )
                resources.test_barrier = CrashBarrier(
                    CrashBarrierConfig(root=triggered_root, selected=PHASE9_SYNC_BEFORE_EFFECT)
                )
                triggered_id = f"phase9-triggered-sync-{new_uuid7()}"
                triggered = await env.client.start_workflow(
                    TriggeredTaskWorkflow.run,
                    seeded.triggered_input,
                    id=triggered_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await _wait_for_marker(triggered_root, PHASE9_SYNC_BEFORE_EFFECT)
                handles["triggered-sync"] = (triggered_id, triggered)

                engineering_root = (
                    Path(os.environ.get("TMPDIR", "/tmp")) / f"phase9-engineering-{new_uuid7()}"
                )
                resources.test_barrier = CrashBarrier(
                    CrashBarrierConfig(root=engineering_root, selected=PHASE9_SYNC_BEFORE_EFFECT)
                )
                engineering_id = f"phase9-engineering-sync-{new_uuid7()}"
                engineering = await env.client.start_workflow(
                    EngineeringTicketWorkflow.run,
                    seeded.engineering_input,
                    id=engineering_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
                await _wait_for_marker(engineering_root, PHASE9_SYNC_BEFORE_EFFECT)
                handles["engineering-sync"] = (engineering_id, engineering)

                return await _fetch_all_before_close(handles)
    finally:
        agent_activities_module.build_default_catalog = original_catalog
        agent_activities_module.build_model_client = original_model_factory
        agent_activities_module.delete_sandbox_workspace = original_cleanup
        trigger_activities_module.LINEAR_TOOLS = original_linear_tools
        await engine.dispose()


async def _run_capture(destination: Path, database_url: str) -> None:
    os.environ["PHASE9_CAPTURE_DATABASE_URL"] = database_url
    source_ref = validate_phase9_source()
    await asyncio.to_thread(upgrade_to_head, database_url)
    await generate(destination, source_ref=source_ref)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PHASE9_CAPTURE_DATABASE_URL", DEFAULT_CAPTURE_DATABASE_URL),
    )
    parser.add_argument("--destination", type=Path, default=FIXTURE_ROOT)
    args = parser.parse_args()
    asyncio.run(_run_capture(args.destination, args.database_url))


if __name__ == "__main__":
    main()
