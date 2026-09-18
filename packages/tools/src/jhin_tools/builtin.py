"""Phase 4 built-in system tools — one per risk level (plan 45 Phase 4).

These exist so the gateway, policy engine, approvals, and UI can be exercised
end-to-end before real connectors arrive in Phase 5. Connectors register more
tools through exactly the same :class:`ToolCatalog` mechanism.

Executors receive a :class:`ToolExecutionContext` (session + run identity)
and the schema-validated input model; they never see raw model output or
credentials.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import AuditEvent, Message, Task
from jhin_domain import ActorType, MessageVisibility, RecipientType, SenderType
from jhin_policy import (
    CapabilityRegistry,
    Grant,
    GrantEffect,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    capability_matches,
    scope_matches,
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
    attribute them to the exact tool call.

    ``authorizing_grants`` is the gateway's answer to "which allow grants
    covered this call" (:func:`jhin_policy.authorizing_allow_grants`), and
    exists for one job: a listing executor narrowing its own rows to the
    scope those grants carry, so an agent granted one repository does not
    read back the names of every other. It is **not** a decision and never
    stands in for one — the gateway has already allowed or denied the call
    by the time an executor can see this, and an executor that ignored it
    would still be inside the policy the evaluator applied."""

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
    authorizing_grants: tuple[Grant, ...] = ()


ToolExecutor = Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]]

# Optional tool-specific policy validator (plan 7.5): runs in the gateway
# after the generic grant/policy evaluation and before approval staging or
# execution. Receives the already-loaded grants; returns a DENY decision to
# block the call or None to let it proceed. This is policy code — model
# output never reaches it unvalidated.
ToolValidator = Callable[
    [ToolExecutionContext, BaseModel, Sequence[Grant]], Awaitable[PolicyDecision | None]
]


class DynamicToolSource(Protocol):
    """A provider of workspace-scoped tools that are not known at process
    start (e.g. tools discovered from a connected MCP server). Sources load
    definitions from durable workspace state only — never from model output —
    and the resulting definitions pass through exactly the same registry
    guards, grants, scopes, policy, and sanitization as static tools."""

    async def load(
        self, session: AsyncSession, workspace_id: UUID
    ) -> Sequence[tuple[ToolDefinition, ToolExecutor]]: ...


class ToolCatalog:
    """Tool definitions plus their executors (and optional validators).

    Wraps a :class:`CapabilityRegistry`, so the same guards apply (no
    duplicate names, no self-modification capabilities).

    A catalog may also carry :class:`DynamicToolSource`s; ``for_workspace``
    materializes a per-workspace view that adds their tools. Static tools
    always win name collisions, so a dynamic source can never shadow a
    built-in or connector tool.
    """

    def __init__(self) -> None:
        self.registry = CapabilityRegistry()
        self._executors: dict[str, ToolExecutor] = {}
        self._validators: dict[str, ToolValidator] = {}
        self._dynamic_sources: list[DynamicToolSource] = []

    def add_dynamic_source(self, source: DynamicToolSource) -> None:
        self._dynamic_sources.append(source)

    @property
    def has_dynamic_sources(self) -> bool:
        return bool(self._dynamic_sources)

    async def for_workspace(self, session: AsyncSession, workspace_id: UUID) -> ToolCatalog:
        """The catalog as seen from one workspace: static tools plus every
        dynamic source's tools for that workspace. Returns ``self`` when no
        dynamic source is registered, so static deployments pay nothing."""
        if not self._dynamic_sources:
            return self
        view = ToolCatalog()
        for definition in self.registry:
            view.register(
                definition, self._executors[definition.name], self._validators.get(definition.name)
            )
        for source in self._dynamic_sources:
            for definition, executor in await source.load(session, workspace_id):
                if view.registry.get(definition.name) is not None:
                    continue
                view.register(definition, executor)
        return view

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        validator: ToolValidator | None = None,
    ) -> None:
        if definition.defers_scope and validator is None:
            raise ValueError(f"Deferred-scope tool '{definition.name}' requires a validator")
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


class ToolDefinitionCatalog:
    """Tool definitions without executor or validator callables.

    API and discovery processes use this catalog so constructing a public
    schema view cannot import or initialize executable connector behavior.
    """

    def __init__(self) -> None:
        self._registry = CapabilityRegistry()

    def register(self, definition: ToolDefinition) -> None:
        self._registry.register(definition)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._registry)


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


def builtin_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Built-in definitions without importing any executor into the caller."""
    from jhin_tools.ask_person import ASK_PERSON_TOOLS
    from jhin_tools.directory import DIRECTORY_TOOLS
    from jhin_tools.identity import IDENTITY_TOOLS
    from jhin_tools.memory import MEMORY_TOOLS
    from jhin_tools.organization import ORGANIZATION_TOOLS
    from jhin_tools.organization_admin import ORGANIZATION_ADMIN_TOOLS
    from jhin_tools.personas import PERSONA_TOOLS
    from jhin_tools.reviews import REVIEW_TOOLS
    from jhin_tools.schedule_tools import SCHEDULE_TOOLS
    from jhin_tools.skills_tools import SKILL_TOOLS
    from jhin_tools.variables import VARIABLE_TOOLS
    from jhin_tools.work_requests import WORK_REQUEST_TOOLS

    return tuple(definition for definition, _executor in BUILTIN_TOOLS) + tuple(
        definition
        for definition, _executor, _validator in (
            *ORGANIZATION_TOOLS,
            *ORGANIZATION_ADMIN_TOOLS,
            *DIRECTORY_TOOLS,
            *WORK_REQUEST_TOOLS,
            *REVIEW_TOOLS,
            *SCHEDULE_TOOLS,
            *MEMORY_TOOLS,
            *ASK_PERSON_TOOLS,
            *SKILL_TOOLS,
            *PERSONA_TOOLS,
            *IDENTITY_TOOLS,
            *VARIABLE_TOOLS,
        )
    )


def build_builtin_catalog() -> ToolCatalog:
    """The default built-in catalog: Phase 4 system tools plus the Phase 8
    organization tools (delegation + structured result reporting). Phase 5
    connectors extend it by calling ``catalog.register`` with their own
    definitions and executors."""
    # Local import: jhin_tools.organization imports ToolExecutionContext
    # from this module.
    from jhin_tools.ask_person import ASK_PERSON_TOOLS
    from jhin_tools.directory import DIRECTORY_TOOLS
    from jhin_tools.identity import IDENTITY_TOOLS
    from jhin_tools.memory import MEMORY_TOOLS
    from jhin_tools.organization import ORGANIZATION_TOOLS
    from jhin_tools.organization_admin import ORGANIZATION_ADMIN_TOOLS
    from jhin_tools.personas import PERSONA_TOOLS
    from jhin_tools.reviews import REVIEW_TOOLS
    from jhin_tools.schedule_tools import SCHEDULE_TOOLS
    from jhin_tools.skills_tools import SKILL_TOOLS
    from jhin_tools.variables import VARIABLE_TOOLS
    from jhin_tools.work_requests import WORK_REQUEST_TOOLS

    catalog = ToolCatalog()
    for definition, executor in BUILTIN_TOOLS:
        catalog.register(definition, executor)
    # Coordination tools (directory search, peer work requests, reviews)
    # register exactly like the Phase 8 organization tools.
    for definition, org_executor, validator in (
        *ORGANIZATION_TOOLS,
        *ORGANIZATION_ADMIN_TOOLS,
        *DIRECTORY_TOOLS,
        *WORK_REQUEST_TOOLS,
        *REVIEW_TOOLS,
        *SCHEDULE_TOOLS,
        *MEMORY_TOOLS,
        *ASK_PERSON_TOOLS,
        *SKILL_TOOLS,
        *PERSONA_TOOLS,
        *IDENTITY_TOOLS,
        *VARIABLE_TOOLS,
    ):
        catalog.register(definition, org_executor, validator)
    return catalog


def allowed_tool_definitions(
    catalog: ToolCatalog | ToolDefinitionCatalog,
    grants: Sequence[Grant],
    *,
    live_connection_ids: Collection[str] | None = None,
    connection_tool_names: Mapping[str, Collection[str]] | None = None,
) -> tuple[ToolDefinition, ...]:
    """Tools worth advertising to the model: those with any matching allow
    grant. Advertisement is prompt economy, not authorization — the gateway
    re-decides every call against live grants, scopes, and policy (plan 52).

    When ``live_connection_ids`` is given, an allow grant pinned to a
    connection outside that set (deleted, disabled, or waiting to be
    reconnected) is ignored here: a tool the agent could only ever be denied
    is not worth offering, and offering it is what taught agents to report
    blocks they never observed. Unpinned grants and deny grants are
    unaffected by that filter. Per-connection tool metadata, when supplied,
    additionally withholds connector tools with no compatible, grant-admitted
    connection. The gateway still decides every call from live rows.
    """
    allow_patterns: list[str] = []
    for grant in grants:
        if grant.effect is not GrantEffect.ALLOW:
            continue
        pinned = grant.scope.get("connection_id")
        if (
            live_connection_ids is not None
            and "connection_id" in grant.scope
            and not any(
                scope_matches({"connection_id": pinned}, {"connection_id": connection_id})
                for connection_id in live_connection_ids
            )
        ):
            continue
        allow_patterns.append(grant.capability)
    return tuple(
        definition
        for definition in catalog.definitions()
        if any(
            capability_matches(pattern, definition.required_capability)
            for pattern in allow_patterns
        )
        and (
            connection_tool_names is None
            or "connection_id" not in definition.scope_keys
            or connection_hints(
                definition,
                grants,
                dict.fromkeys(connection_tool_names, ""),
                connection_tool_names=connection_tool_names,
            )
        )
    )


# Tools whose whole meaning is "hand a finished assignment back to whoever
# delegated it". Advertising them on an ordinary chat turn invites the model
# to file a report *instead of* answering the person who asked (and then it
# has nothing left to say), so they are withheld there.
DELEGATION_REPORTING_TOOLS = frozenset({"organization.report_result"})

# Task metadata ``origin`` values that mean "a person is talking to this
# agent in a chat thread", as written by the API conversation endpoints.
_CONVERSATION_ORIGINS = frozenset({"conversation", "message"})


def task_expects_a_reported_result(task: Task | None) -> bool:
    """Whether reporting a structured result is meaningful for this task.

    True for assigned work with somewhere to report *to*: delegated and
    review children, accepted work requests, and ordinary standalone tasks
    (whose result card is the task's own outcome). False for a plain
    conversation turn — there the agent's reply *is* the deliverable.

    ``None`` (task unknown to the caller) keeps the tool advertised: this is
    prompt economy, not authorization, and the gateway re-decides every call
    against live grants either way.
    """
    if task is None:
        return True
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    # Delegated / review children and work-request tasks report back by
    # design — the whole Phase 8 flow reads the reported result.
    if task.parent_task_id is not None:
        return True
    if metadata.get("delegation") or metadata.get("work_request"):
        return True
    # A chat turn: a person is waiting for an answer, not for a filed report.
    return not (metadata.get("origin") in _CONVERSATION_ORIGINS or task.conversation_id is not None)


# Tools that put something on a person's screen and wait. They mean nothing
# on work nobody is watching: a trigger-fired run, a delegated child, an
# accepted work request. Withheld there by default — the inverse of
# DELEGATION_REPORTING_TOOLS, which is withheld only on chat turns.
PERSON_FACING_TOOLS = frozenset({"organization.ask_person"})


def task_has_a_person_watching(task: Task | None) -> bool:
    """True only for a turn in a chat thread a person opened.

    ``None`` (task unknown) is False, deliberately the opposite default to
    ``task_expects_a_reported_result``: withholding a report is prompt
    economy, withholding an interruption is a promise to the person.
    """
    if task is None:
        return False
    # A delegated or review child reports into its parent; the person is
    # watching that thread, and a box on the child's task page would land
    # where nobody is looking.
    if task.parent_task_id is not None:
        return False
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    if metadata.get("delegation") or metadata.get("work_request"):
        return False
    return task.conversation_id is not None or metadata.get("origin") in _CONVERSATION_ORIGINS


def task_scoped_tool_definitions(
    definitions: Sequence[ToolDefinition], task: Task | None
) -> tuple[ToolDefinition, ...]:
    """Narrow advertised definitions to those meaningful for ``task``."""
    withheld: set[str] = set()
    if task is not None:
        from jhin_tools.turn_modes import mode_denial

        withheld |= {
            definition.name
            for definition in definitions
            if mode_denial(definition, task.metadata_json)
        }
    if not task_expects_a_reported_result(task):
        withheld |= DELEGATION_REPORTING_TOOLS
    if not task_has_a_person_watching(task):
        withheld |= PERSON_FACING_TOOLS
    if not withheld:
        return tuple(definitions)
    return tuple(definition for definition in definitions if definition.name not in withheld)


def _scope_value_covers(denied: object, allowed: object) -> bool:
    """Prove a deny covers an allow value without guessing glob inclusion."""
    if isinstance(allowed, list):
        return all(_scope_value_covers(denied, item) for item in allowed)
    if isinstance(denied, list):
        return any(_scope_value_covers(item, allowed) for item in denied)
    if isinstance(allowed, str) and any(char in allowed for char in "*?["):
        return denied == allowed or denied == "*"
    return scope_matches({"value": denied}, {"value": allowed})


def connection_hints(
    definition: ToolDefinition,
    grants: Sequence[Grant],
    connection_labels: Mapping[str, str],
    *,
    connection_tool_names: Mapping[str, Collection[str]] | None = None,
) -> str:
    """Describe, for the model, which connections a connector tool may use.

    Connector tools take a ``connection_id`` the model cannot know on its
    own (it is a workspace UUID). With per-connection tool metadata, valid
    unpinned grants can name every compatible live connection. Without it,
    only explicit pins are safe to describe. Required grant dimensions and
    connection denies still apply; other scope values are shown with the ID.
    This is prompt context only: the gateway still decides every call.
    """
    if "connection_id" not in definition.scope_keys:
        return ""
    # Dynamic tools fix scope fields such as tool/server_slug/toolkit with
    # Literals. A grant for a different literal cannot authorize any valid
    # input, while a deny matching it covers every call to this definition.
    properties = definition.input_json_schema().get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    fixed_scope: dict[str, object] = {}
    for key in definition.scope_keys:
        field = properties.get(key, {})
        if not isinstance(field, dict):
            continue
        if "const" in field:
            fixed_scope[key] = field["const"]
        elif isinstance(field.get("enum"), list) and len(field["enum"]) == 1:
            fixed_scope[key] = field["enum"][0]
    lines: list[str] = []
    seen: set[str] = set()
    for grant in grants:
        if grant.effect is not GrantEffect.ALLOW:
            continue
        if not capability_matches(grant.capability, definition.required_capability):
            continue
        if not set(definition.required_grant_scope_keys).issubset(grant.scope):
            continue
        if not definition.defers_scope and not set(grant.scope).issubset(
            (*definition.scope_keys, *definition.result_scope_keys)
        ):
            continue
        candidates = (
            connection_labels
            if connection_tool_names is not None
            else {str(grant.scope.get("connection_id", "")): ""}
        )
        extras = ", ".join(
            f"{key}={grant.scope[key]}"
            for key in definition.scope_keys
            if key != "connection_id" and key in grant.scope
        )
        for connection_id in candidates:
            label = connection_labels.get(connection_id)
            if label is None or (
                connection_tool_names is not None
                and definition.name not in connection_tool_names.get(connection_id, ())
            ):
                continue
            requested = {**fixed_scope, "connection_id": connection_id}
            if not scope_matches(
                {key: value for key, value in grant.scope.items() if key in requested}, requested
            ):
                continue
            matching_denies = [
                deny
                for deny in grants
                if (
                    deny.effect is GrantEffect.DENY
                    and capability_matches(deny.capability, definition.required_capability)
                    and scope_matches(
                        {key: value for key, value in deny.scope.items() if key in requested},
                        requested,
                    )
                )
            ]
            # Only a deny covering the whole allow scope removes the candidate.
            # A private/* deny must not hide public files on the same app. The
            # gateway decides the actual command/path; spell out the remaining
            # deny scopes so the model can choose a permitted call.
            denied_dimensions = [
                {
                    key: value
                    for key, value in deny.scope.items()
                    if key not in requested and key not in definition.result_scope_keys
                }
                for deny in matching_denies
            ]
            if any(
                all(
                    key in grant.scope and _scope_value_covers(value, grant.scope[key])
                    for key, value in dimensions.items()
                )
                for dimensions in denied_dimensions
            ):
                continue
            exclusions = "; ".join(
                ", ".join(f"{key}={value}" for key, value in dimensions.items())
                for dimensions in denied_dimensions
            )
            line = f"{label} — connection_id={connection_id}" + (f" ({extras})" if extras else "")
            if exclusions:
                line += f" [denied scopes: {exclusions}]"
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    if not lines:
        return ""
    return "Connections you may use (pass the connection_id exactly as given): " + "; ".join(lines)


def advertised_description(
    definition: ToolDefinition,
    grants: Sequence[Grant],
    connection_labels: Mapping[str, str],
    *,
    connection_tool_names: Mapping[str, Collection[str]] | None = None,
) -> str:
    """Tool description for the model: the registry text plus connection hints."""
    hints = connection_hints(
        definition, grants, connection_labels, connection_tool_names=connection_tool_names
    )
    return f"{definition.description} {hints}".strip() if hints else definition.description
