"""Phase 4 built-in system tools — one per risk level (plan 45 Phase 4).

These exist so the gateway, policy engine, approvals, and UI can be exercised
end-to-end before real connectors arrive in Phase 5. Connectors register more
tools through exactly the same :class:`ToolCatalog` mechanism.

Executors receive a :class:`ToolExecutionContext` (session + run identity)
and the schema-validated input model; they never see raw model output or
credentials.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import AuditEvent, Message
from jhin_domain import ActorType, MessageVisibility, RecipientType, SenderType
from jhin_policy import (
    CapabilityRegistry,
    Grant,
    GrantEffect,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    capability_matches,
)
from jhin_secrets import SecretCrypto
from jhin_tools.test_barriers import CrashBarrier


@dataclass(frozen=True)
class ToolExecutionContext:
    """Everything an executor may touch. Deliberately narrow: no model
    client, no workflow handle. ``crypto`` exists so connector executors can
    decrypt connection credentials at the moment of use (plan 13.5) — it is
    None for processes that hold no master key, and system tools never
    touch it. ``tool_call_id`` is set by the gateway just before execution
    so executors that spawn linked records (e.g. sandbox jobs, plan 14) can
    attribute them to the exact tool call."""

    session: AsyncSession
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID
    agent_name: str
    crypto: SecretCrypto | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    tool_call_id: UUID | None = None
    test_barrier: CrashBarrier | None = None


ToolExecutor = Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]]

# Optional tool-specific policy validator (plan 7.5): runs in the gateway
# after the generic grant/policy evaluation and before approval staging or
# execution. Receives the already-loaded grants; returns a DENY decision to
# block the call or None to let it proceed. This is policy code — model
# output never reaches it unvalidated.
ToolValidator = Callable[
    [ToolExecutionContext, BaseModel, Sequence[Grant]], Awaitable[PolicyDecision | None]
]


class ToolCatalog:
    """Tool definitions plus their executors (and optional validators).

    Wraps a :class:`CapabilityRegistry`, so the same guards apply (no
    duplicate names, no self-modification capabilities).
    """

    def __init__(self) -> None:
        self.registry = CapabilityRegistry()
        self._executors: dict[str, ToolExecutor] = {}
        self._validators: dict[str, ToolValidator] = {}

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        validator: ToolValidator | None = None,
    ) -> None:
        self.registry.register(definition)
        self._executors[definition.name] = executor
        if validator is not None:
            self._validators[definition.name] = validator

    def get(self, name: str) -> tuple[ToolDefinition, ToolExecutor] | None:
        definition = self.registry.get(name)
        if definition is None:
            return None
        return definition, self._executors[name]

    def validator_for(self, name: str) -> ToolValidator | None:
        return self._validators.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self.registry)


# --- system.echo (read) ---


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=10_000)


class EchoOutput(BaseModel):
    text: str


async def _echo(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(EchoInput, payload)
    return EchoOutput(text=data.text)


# --- system.time (read) ---


class TimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeOutput(BaseModel):
    utc_iso: str


async def _time(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return TimeOutput(utc_iso=datetime.now(UTC).isoformat())


# --- system.note.append (write) ---


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)


class NoteOutput(BaseModel):
    note_id: str
    task_id: str


async def _note_append(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """A harmless persistent side effect: a visible note on the task."""
    data = cast(NoteInput, payload)
    note = Message(
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        run_id=ctx.run_id,
        sender_type=SenderType.AGENT.value,
        sender_id=ctx.agent_id,
        recipient_type=RecipientType.TASK.value,
        recipient_id=ctx.task_id,
        message_type="note",
        content_json={"text": data.text},
        visibility=MessageVisibility.VISIBLE.value,
    )
    ctx.session.add(note)
    await ctx.session.flush()
    return NoteOutput(note_id=str(note.id), task_id=str(ctx.task_id))


# --- system.demo.elevated (elevated) ---


class DemoElevatedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=200)


class DemoElevatedOutput(BaseModel):
    acknowledged: str


async def _demo_elevated(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(DemoElevatedInput, payload)
    return DemoElevatedOutput(acknowledged=data.label or "elevated demo executed")


# --- system.demo.destructive (destructive, approval-gated) ---


class DemoDestructiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=200)


class DemoDestructiveOutput(BaseModel):
    marker: str


async def _demo_destructive(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """Inert 'destructive' effect: an append-only audit marker row."""
    data = cast(DemoDestructiveInput, payload)
    marker = AuditEvent(
        workspace_id=ctx.workspace_id,
        actor_type=ActorType.AGENT.value,
        actor_id=ctx.agent_id,
        action="demo.destructive.marker",
        target_type="task",
        target_id=ctx.task_id,
        metadata_json={"label": data.label, "run_id": str(ctx.run_id)},
    )
    ctx.session.add(marker)
    await ctx.session.flush()
    return DemoDestructiveOutput(marker=str(marker.id))


BUILTIN_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="system.echo",
            description="Return the given text unchanged. Useful for testing tool access.",
            risk=RiskLevel.READ,
            input_model=EchoInput,
            output_model=EchoOutput,
            required_capability="system.echo",
        ),
        _echo,
    ),
    (
        ToolDefinition(
            name="system.time",
            description="Return the current UTC time in ISO-8601 form.",
            risk=RiskLevel.READ,
            input_model=TimeInput,
            output_model=TimeOutput,
            required_capability="system.time",
        ),
        _time,
    ),
    (
        ToolDefinition(
            name="system.note.append",
            description="Append a persistent note to the current task.",
            risk=RiskLevel.WRITE,
            input_model=NoteInput,
            output_model=NoteOutput,
            required_capability="system.note.append",
            supports_approval=True,
        ),
        _note_append,
    ),
    (
        ToolDefinition(
            name="system.demo.elevated",
            description="Demonstration tool at the elevated risk level.",
            risk=RiskLevel.ELEVATED,
            input_model=DemoElevatedInput,
            output_model=DemoElevatedOutput,
            required_capability="system.demo.elevated",
            supports_approval=True,
        ),
        _demo_elevated,
    ),
    (
        ToolDefinition(
            name="system.demo.destructive",
            description=(
                "Demonstration tool at the destructive risk level; records an inert audit marker."
            ),
            risk=RiskLevel.DESTRUCTIVE,
            input_model=DemoDestructiveInput,
            output_model=DemoDestructiveOutput,
            required_capability="system.demo.destructive",
            supports_approval=True,
        ),
        _demo_destructive,
    ),
)


def build_builtin_catalog() -> ToolCatalog:
    """The default built-in catalog: Phase 4 system tools plus the Phase 8
    organization tools (delegation + structured result reporting). Phase 5
    connectors extend it by calling ``catalog.register`` with their own
    definitions and executors."""
    # Local import: jhin_tools.organization imports ToolExecutionContext
    # from this module.
    from jhin_tools.organization import ORGANIZATION_TOOLS

    catalog = ToolCatalog()
    for definition, executor in BUILTIN_TOOLS:
        catalog.register(definition, executor)
    for definition, org_executor, validator in ORGANIZATION_TOOLS:
        catalog.register(definition, org_executor, validator)
    return catalog


def allowed_tool_definitions(
    catalog: ToolCatalog, grants: Sequence[Grant]
) -> tuple[ToolDefinition, ...]:
    """Tools worth advertising to the model: those with any matching allow
    grant. Advertisement is prompt economy, not authorization — the gateway
    re-decides every call against live grants, scopes, and policy (plan 52).
    """
    allow_patterns = [g.capability for g in grants if g.effect is GrantEffect.ALLOW]
    return tuple(
        definition
        for definition in catalog.definitions()
        if any(
            capability_matches(pattern, definition.required_capability)
            for pattern in allow_patterns
        )
    )
