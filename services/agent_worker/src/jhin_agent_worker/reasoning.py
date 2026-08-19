"""Agent-only model reasoning and atomic call-manifest binding.

The canonical tool manifest is the only tool-call authority shared with the
tool worker.  Completion text, provider identifiers, transitions, and usage
remain in a separate agent-only event committed in the same transaction.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_agents import AgentExecutionSnapshot
from jhin_agents.context import ConversationTurn, TaskContext
from jhin_agents.graph import NodeTransition
from jhin_agents.runtime import estimate_cost_micros, execute_step
from jhin_db.models import AgentRun, AuditEvent, Message, RunEvent, Task
from jhin_domain import (
    AGENT_MESSAGE_TYPES,
    ActorType,
    MessageVisibility,
    RunStatus,
    SenderType,
)
from jhin_models import (
    ModelClient,
    ModelProviderError,
    ModelToolCall,
    ToolSchema,
    build_model_client,
)
from jhin_models.factory import ProviderConfigError
from jhin_observability import get_logger
from jhin_secrets import SecretStore
from jhin_secrets.redaction import redact_text
from jhin_tools import AGENT_BEFORE_BIND, MAX_TOOL_CALLS_PER_STEP, PHASE9_AFTER_MANIFEST
from jhin_tools.sanitize import sanitize_payload, strict_json_loads
from jhin_workflows.agent_task.shared import (
    ACTIVITY_REASON_AGENT_STEP,
    AdvertisedTool,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
)

_MAX_ARGUMENTS_CHARS = 8_192
_MAX_MODEL_TEXT_CHARS = 8_192
_MAX_PROVIDER_TEXT_CHARS = 200
_MAX_TRANSITIONS = 128
_MAX_TRANSITION_DETAIL_CHARS = 2_000
_MAX_STRUCTURED_TURN_CHARS = 6_000
_STRUCTURED_MESSAGE_TYPES = frozenset(item.value for item in AGENT_MESSAGE_TYPES)

logger = get_logger(__name__)

BoundedProviderText = Annotated[str, StringConstraints(max_length=_MAX_PROVIDER_TEXT_CHARS)]


class AgentStepUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cost_micros: int = Field(ge=0)


class AgentStepReasoningRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    step: int = Field(ge=0)
    completion_sanitized: str = Field(max_length=_MAX_MODEL_TEXT_CHARS)
    model: BoundedProviderText
    finish_reason: BoundedProviderText
    provider_request_id: BoundedProviderText
    provider_call_ids: tuple[BoundedProviderText, ...]
    transitions: tuple[dict[str, Any], ...] = Field(max_length=_MAX_TRANSITIONS)
    done: bool
    usage: AgentStepUsage
    latency_ms: int = Field(ge=0)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        expected_step: int,
        expected_call_count: int,
    ) -> AgentStepReasoningRecord:
        try:
            record = cls.model_validate(payload)
        except ValidationError as error:
            raise ApplicationError(
                "agent step reasoning is malformed",
                type="reasoning_sidecar_invalid",
                non_retryable=True,
            ) from error
        if record.step != expected_step or len(record.provider_call_ids) != expected_call_count:
            raise ApplicationError(
                "agent step reasoning does not match its manifest",
                type="reasoning_sidecar_invalid",
                non_retryable=True,
            )
        return record


class ManifestCall(BaseModel):
    """Validated internal view of one immutable canonical manifest entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    lossless: Literal[True]
    tool_name: str = Field(max_length=_MAX_PROVIDER_TEXT_CHARS)
    arguments_json: str = Field(max_length=_MAX_ARGUMENTS_CHARS)


def to_model_tool_schemas(tools: list[AdvertisedTool]) -> tuple[ToolSchema, ...]:
    return tuple(
        ToolSchema(name=tool.name, description=tool.description, parameters=tool.parameters)
        for tool in tools
    )


def sanitize_transition(transition: NodeTransition) -> dict[str, Any]:
    return {
        "node": redact_text(transition.node)[:_MAX_PROVIDER_TEXT_CHARS],
        "detail": redact_text(transition.detail)[:_MAX_TRANSITION_DETAIL_CHARS],
    }


def _recursively_unchanged(value: Any) -> bool:
    if isinstance(value, str):
        return bool(redact_text(value) == value)
    if isinstance(value, dict):
        return all(
            redact_text(str(key)) == str(key) and _recursively_unchanged(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_recursively_unchanged(child) for child in value)
    return True


def _step_tool_manifest(tool_calls: tuple[ModelToolCall, ...]) -> dict[str, Any]:
    """Build the provider-independent, secret-safe binding for one call set."""
    calls: list[dict[str, Any]] = []
    for ordinal, call in enumerate(tool_calls):
        valid_json_object = False
        try:
            decoded = strict_json_loads(call.arguments_json)
            valid_json_object = isinstance(decoded, dict)
            canonical_arguments = json.dumps(
                decoded,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            manifest_arguments: Any = decoded
        except (json.JSONDecodeError, TypeError, ValueError):
            canonical_arguments = call.arguments_json
            manifest_arguments = call.arguments_json
        sanitized = sanitize_payload(
            {
                "tool_name": call.name,
                "arguments": manifest_arguments,
            }
        )
        lossless = (
            valid_json_object
            and sanitized.get("tool_name") == call.name
            and sanitized.get("arguments") == manifest_arguments
            and _recursively_unchanged(manifest_arguments)
            and len(call.name) <= _MAX_PROVIDER_TEXT_CHARS
            and len(canonical_arguments) <= _MAX_ARGUMENTS_CHARS
        )
        entry: dict[str, Any] = {"ordinal": ordinal, "lossless": lossless}
        if lossless:
            entry.update(
                {
                    "tool_name": call.name,
                    "arguments_json": canonical_arguments,
                }
            )
        calls.append(entry)
    return {"count": len(calls), "calls": calls}


def manifest_calls_from_payload(
    payload: dict[str, Any],
    *,
    expected_step: int,
) -> tuple[ManifestCall, ...]:
    """Validate an existing manifest before it is used by agent projections."""
    if set(payload) != {"step", "manifest"} or payload.get("step") != expected_step:
        raise ApplicationError(
            "agent step tool manifest is malformed",
            type="tool_step_manifest_invalid",
            non_retryable=True,
        )
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict) or set(manifest) != {"count", "calls"}:
        raise ApplicationError(
            "agent step tool manifest is malformed",
            type="tool_step_manifest_invalid",
            non_retryable=True,
        )
    raw_count = manifest.get("count")
    raw_calls = manifest.get("calls")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count < 0
        or not isinstance(raw_calls, list)
        or len(raw_calls) != raw_count
    ):
        raise ApplicationError(
            "agent step tool manifest is malformed",
            type="tool_step_manifest_invalid",
            non_retryable=True,
        )
    calls: list[ManifestCall] = []
    try:
        for ordinal, raw_call in enumerate(raw_calls):
            call = ManifestCall.model_validate(raw_call)
            decoded = strict_json_loads(call.arguments_json)
            canonical = json.dumps(
                decoded,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if (
                call.ordinal != ordinal
                or not isinstance(decoded, dict)
                or canonical != call.arguments_json
                or not _recursively_unchanged(decoded)
                or redact_text(call.tool_name) != call.tool_name
            ):
                raise ValueError("noncanonical manifest entry")
            calls.append(call)
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ApplicationError(
            "agent step tool manifest is malformed",
            type="tool_step_manifest_invalid",
            non_retryable=True,
        ) from error
    return tuple(calls)


async def load_step_event(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    task_id: UUID,
    run_id: UUID,
    step_index: int,
    event_type: str,
) -> RunEvent | None:
    events = list(
        await session.scalars(
            select(RunEvent).where(
                RunEvent.workspace_id == workspace_id,
                RunEvent.task_id == task_id,
                RunEvent.run_id == run_id,
                RunEvent.event_type == event_type,
            )
        )
    )
    matching = [event for event in events if event.payload_json.get("step") == step_index]
    if len(matching) > 1:
        raise ApplicationError(
            "agent step has duplicate durable bindings",
            type="reasoning_bind_incomplete",
            non_retryable=True,
        )
    return matching[0] if matching else None


async def _load_step_pair(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    task_id: UUID,
    run_id: UUID,
    step_index: int,
) -> tuple[RunEvent | None, RunEvent | None]:
    manifest = await load_step_event(
        session,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        step_index=step_index,
        event_type="agent.step.tool_manifest",
    )
    reasoning = await load_step_event(
        session,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        step_index=step_index,
        event_type="agent.step.reasoning",
    )
    return manifest, reasoning


def _validate_complete_pair(
    manifest: RunEvent,
    reasoning: RunEvent,
    *,
    step_index: int,
) -> tuple[tuple[ManifestCall, ...], AgentStepReasoningRecord]:
    calls = manifest_calls_from_payload(manifest.payload_json, expected_step=step_index)
    record = AgentStepReasoningRecord.from_payload(
        reasoning.payload_json,
        expected_step=step_index,
        expected_call_count=len(calls),
    )
    return calls, record


async def _next_seq(session: AsyncSession, run_id: UUID) -> int:
    current = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    return (current if current is not None else -1) + 1


def _add_run_event(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    task_id: UUID,
    run_id: UUID,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        RunEvent(
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            payload_json=payload,
        )
    )


async def _load_history(session: AsyncSession, task: Task) -> tuple[ConversationTurn, ...]:
    rows = await session.scalars(
        select(Message)
        .where(
            Message.task_id == task.id,
            or_(
                Message.visibility == MessageVisibility.VISIBLE.value,
                Message.message_type.in_(("tool_call", "tool_result")),
            ),
        )
        .order_by(Message.created_at, Message.id)
    )
    turns: list[ConversationTurn] = []
    for message in rows:
        content = message.content_json
        if message.message_type == "tool_call":
            turns.append(
                ConversationTurn(
                    role="agent",
                    text=str(content.get("text", "") or ""),
                    kind="tool_call",
                    tool_call_id=str(content.get("tool_call_id", "")),
                    tool_name=str(content.get("tool_name", "")),
                    arguments_json=str(content.get("arguments_json", "{}")),
                )
            )
            continue
        if message.message_type == "tool_result":
            turns.append(
                ConversationTurn(
                    role="agent",
                    text=str(content.get("result", "")),
                    kind="tool_result",
                    tool_call_id=str(content.get("tool_call_id", "")),
                    tool_name=str(content.get("tool_name", "")),
                )
            )
            continue
        if message.message_type in _STRUCTURED_MESSAGE_TYPES:
            if content.get("delivered") == "observation":
                continue
            is_own = (
                message.sender_type == SenderType.AGENT.value
                and message.sender_id == task.assigned_agent_id
            )
            rendered = json.dumps(content, ensure_ascii=False, default=str)
            turns.append(
                ConversationTurn(
                    role="agent" if is_own else "user",
                    text=f"[{message.message_type}] {rendered}"[:_MAX_STRUCTURED_TURN_CHARS],
                )
            )
            continue
        text = str(content.get("text", ""))
        if not text:
            continue
        role = "agent" if message.sender_type == SenderType.AGENT.value else "user"
        if not turns and role == "user" and text.strip() == task.description.strip():
            continue
        turns.append(ConversationTurn(role=role, text=text))
    return tuple(turns)


class AgentReasoningActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    def _build_model_client(
        self,
        provider_type: str,
        *,
        base_url: str | None,
        api_key: str | None,
    ) -> ModelClient:
        return build_model_client(provider_type, base_url=base_url, api_key=api_key)

    async def _after_reasoning_bind_commit(self) -> None:
        """Compatibility hook for the frozen Phase 9 crash-barrier harness."""
        return None

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step_activity(
        self,
        params: ReasonAgentStepInput,
    ) -> ReasonAgentStepResult:
        return await self.reason_agent_step(params)

    async def reason_agent_step(
        self,
        params: ReasonAgentStepInput,
        *,
        legacy_sidecar_repair: bool = False,
    ) -> ReasonAgentStepResult:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)
        snapshot = AgentExecutionSnapshot.model_validate_json(params.snapshot_json)

        async with self._resources.session_factory() as session:
            manifest_event, reasoning_event = await _load_step_pair(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                step_index=params.step_index,
            )
            if manifest_event is not None and reasoning_event is not None:
                calls, _record = _validate_complete_pair(
                    manifest_event,
                    reasoning_event,
                    step_index=params.step_index,
                )
                return ReasonAgentStepResult(call_count=len(calls))
            if reasoning_event is not None:
                raise ApplicationError(
                    "agent step reasoning binding is incomplete",
                    type="reasoning_bind_incomplete",
                    non_retryable=True,
                )
            if manifest_event is not None:
                manifest_calls_from_payload(
                    manifest_event.payload_json,
                    expected_step=params.step_index,
                )
                if not legacy_sidecar_repair:
                    raise ApplicationError(
                        "agent step reasoning sidecar is missing",
                        type="reasoning_sidecar_missing",
                        non_retryable=True,
                    )

            existing_run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.id == run_id,
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.agent_id == agent_id,
                    AgentRun.task_id == task_id,
                )
            )
            if (
                existing_run is not None
                and existing_run.error_code == "tool_step_manifest_not_lossless"
            ):
                raise ApplicationError(
                    existing_run.error_message
                    or (
                        "tool call set could not be stored safely; "
                        "manual reconciliation is required"
                    ),
                    type="tool_step_manifest_not_lossless",
                    non_retryable=True,
                )

            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is None:
                raise ApplicationError("task not found", type="task_not_found", non_retryable=True)
            history = await _load_history(session, task)

            api_key: str | None = None
            if snapshot.model_profile.secret_id is not None:
                api_key = await SecretStore(session, self._resources.crypto).reveal(
                    workspace_id,
                    snapshot.model_profile.secret_id,
                )
            try:
                client = self._build_model_client(
                    snapshot.model_profile.provider_type,
                    base_url=snapshot.model_profile.base_url,
                    api_key=api_key,
                )
            except ProviderConfigError as error:
                raise ApplicationError(
                    redact_text(str(error))[:2_000],
                    type="provider_config",
                    non_retryable=True,
                ) from None
            except Exception as error:
                raise ApplicationError(
                    redact_text(str(error))[:2_000] or "model provider configuration failed",
                    type="provider_config",
                    non_retryable=True,
                ) from None
            del api_key

            try:
                outcome = await execute_step(
                    client,
                    snapshot,
                    TaskContext(
                        title=task.title,
                        description=task.description,
                        history=history,
                        user_instructions=tuple(params.user_instructions),
                    ),
                    tools=to_model_tool_schemas(params.advertised_tools),
                )
            except ModelProviderError as error:
                raise ApplicationError(
                    redact_text(str(error))[:2_000],
                    type="model_provider_error",
                    non_retryable=not error.retryable,
                ) from None
            except Exception as error:
                raise ApplicationError(
                    redact_text(str(error))[:2_000] or "model provider request failed",
                    type="model_provider_error",
                ) from None
            finally:
                try:
                    await client.close()
                except Exception as error:
                    logger.warning(
                        "model.client_close_failed",
                        error=(
                            redact_text(str(error))[:2_000] or "model client close failed"
                        ),
                    )

            if len(outcome.tool_calls) > MAX_TOOL_CALLS_PER_STEP:
                raise ApplicationError(
                    "model returned too many tool calls in one step",
                    type="tool_call_limit_exceeded",
                    non_retryable=True,
                )
            manifest = _step_tool_manifest(outcome.tool_calls)
            cost_micros = estimate_cost_micros(
                outcome.usage,
                snapshot.model_profile.input_cost_micros_per_million,
                snapshot.model_profile.output_cost_micros_per_million,
            )
            reasoning = AgentStepReasoningRecord(
                step=params.step_index,
                completion_sanitized=redact_text(outcome.text)[:_MAX_MODEL_TEXT_CHARS],
                model=redact_text(outcome.model)[:_MAX_PROVIDER_TEXT_CHARS],
                finish_reason=redact_text(outcome.finish_reason)[:_MAX_PROVIDER_TEXT_CHARS],
                provider_request_id=redact_text(outcome.provider_request_id or "")[
                    :_MAX_PROVIDER_TEXT_CHARS
                ],
                provider_call_ids=tuple(
                    redact_text(call.id)[:_MAX_PROVIDER_TEXT_CHARS]
                    for call in outcome.tool_calls
                ),
                transitions=tuple(
                    sanitize_transition(transition)
                    for transition in outcome.transitions[:_MAX_TRANSITIONS]
                ),
                done=outcome.done,
                usage=AgentStepUsage(
                    input_tokens=max(0, int(outcome.usage.input_tokens)),
                    output_tokens=max(0, int(outcome.usage.output_tokens)),
                    cached_tokens=max(0, int(outcome.usage.cached_tokens)),
                    cost_micros=max(0, int(cost_micros)),
                ),
                latency_ms=max(0, int(outcome.latency_ms)),
            )

            test_barrier = getattr(self._resources, "test_barrier", None)
            if test_barrier is not None:
                await test_barrier.arrive_and_wait(AGENT_BEFORE_BIND, run_id)

            with session.no_autoflush:
                run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.agent_id == agent_id,
                        AgentRun.task_id == task_id,
                    )
                    .with_for_update()
                )
                manifest_after_lock, reasoning_after_lock = await _load_step_pair(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    step_index=params.step_index,
                )
                await load_step_event(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    step_index=params.step_index,
                    event_type="agent.step.committed",
                )
            if run is None:
                raise ApplicationError(
                    "agent run not found for reasoning",
                    type="run_not_found",
                    non_retryable=True,
                )

            if manifest_after_lock is not None and reasoning_after_lock is not None:
                existing_calls, _existing_reasoning = _validate_complete_pair(
                    manifest_after_lock,
                    reasoning_after_lock,
                    step_index=params.step_index,
                )
                if manifest_after_lock.payload_json.get("manifest") != manifest:
                    await session.rollback()
                    raise ApplicationError(
                        "a concurrent reasoning attempt returned a different tool call set",
                        type="tool_step_manifest_drift",
                    )
                await session.rollback()
                return ReasonAgentStepResult(call_count=len(existing_calls))

            if reasoning_after_lock is not None:
                await session.rollback()
                raise ApplicationError(
                    "agent step reasoning binding is incomplete",
                    type="reasoning_bind_incomplete",
                    non_retryable=True,
                )

            if manifest_after_lock is not None:
                existing_calls = manifest_calls_from_payload(
                    manifest_after_lock.payload_json,
                    expected_step=params.step_index,
                )
                if not legacy_sidecar_repair:
                    await session.rollback()
                    raise ApplicationError(
                        "agent step reasoning binding is incomplete",
                        type="reasoning_bind_incomplete",
                        non_retryable=True,
                    )
                if manifest_after_lock.payload_json.get("manifest") != manifest:
                    await session.rollback()
                    raise ApplicationError(
                        "legacy reasoning repair changed the canonical tool call set",
                        type="tool_step_manifest_drift",
                    )
                seq = await _next_seq(session, run_id)
                _add_run_event(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    seq=seq,
                    event_type="agent.step.reasoning",
                    payload=reasoning.to_payload(),
                )
                await session.commit()
                await self._after_reasoning_bind_commit()
                if test_barrier is not None:
                    await test_barrier.arrive_and_wait(PHASE9_AFTER_MANIFEST, run_id)
                return ReasonAgentStepResult(call_count=len(existing_calls))

            manifest_is_lossless = all(
                bool(entry.get("lossless")) for entry in manifest["calls"]
            )
            if not manifest_is_lossless:
                run.status = RunStatus.FAILED.value
                run.error_code = "tool_step_manifest_not_lossless"
                run.error_message = (
                    f"tool call set for step {params.step_index} could not be stored safely; "
                    "manual reconciliation is required"
                )
                session.add(
                    AuditEvent(
                        workspace_id=workspace_id,
                        actor_type=ActorType.AGENT.value,
                        actor_id=agent_id,
                        action="agent.step.manifest_not_lossless",
                        target_type="agent_run",
                        target_id=run_id,
                        metadata_json={
                            "task_id": str(task_id),
                            "step": params.step_index,
                            "call_count": manifest["count"],
                        },
                    )
                )
                await session.commit()
                raise ApplicationError(
                    run.error_message,
                    type="tool_step_manifest_not_lossless",
                    non_retryable=True,
                )

            seq = await _next_seq(session, run_id)
            _add_run_event(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                seq=seq,
                event_type="agent.step.tool_manifest",
                payload={"step": params.step_index, "manifest": manifest},
            )
            _add_run_event(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                seq=seq + 1,
                event_type="agent.step.reasoning",
                payload=reasoning.to_payload(),
            )
            await session.commit()
            await self._after_reasoning_bind_commit()
            if test_barrier is not None:
                await test_barrier.arrive_and_wait(PHASE9_AFTER_MANIFEST, run_id)
            return ReasonAgentStepResult(call_count=int(manifest["count"]))
