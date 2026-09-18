"""Authorized settings tools; sensitive values are never returned to the agent."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_db.memberships import active_team_ids, primary_team_id
from jhin_db.models import Agent, Message, Task, Team, Workspace
from jhin_policy import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    capability_matches,
    scope_matches,
)
from jhin_secrets.authority import human_message_authorized
from jhin_secrets.variables import VariableActor, VariableError, VariableStore
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError

VARIABLE_READ_CAPABILITY = "variables.read"
VARIABLE_WRITE_CAPABILITY = "variables.write"


class VariableListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["agent", "team", "company"] | None = None
    scope_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)


class VariableGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variable_id: UUID


class VariableSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variable_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    scope: Literal["agent", "team", "company"] | None = None
    scope_id: UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    value: str | None = Field(default=None, max_length=8192)
    sensitive: bool | None = None
    secret_ref: str | None = Field(default=None, max_length=64)


class VariableDeleteInput(VariableGetInput):
    expected_version: int = Field(ge=1)


class VariableCopyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_variable_id: UUID
    expected_version: int = Field(ge=1)
    scope: Literal["agent", "team", "company"]
    scope_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=120)


class VariableScopeOutput(BaseModel):
    scope: Literal["agent", "team", "company"]
    scope_id: UUID
    name: str


class VariableOutput(BaseModel):
    item: dict[str, Any] | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    deleted: bool = False
    accessible_scopes: list[VariableScopeOutput] = Field(default_factory=list)
    accessible_scopes_truncated: bool = False


async def human_scope_authorized(ctx: ToolExecutionContext, scope: str, scope_id: UUID) -> bool:
    """One-call authority derived from a current direct human task request.

    Agent/tool prose cannot authorize itself. Shared-scope language must be
    explicit in a user message and that user's admin membership is checked now.
    This grants no capability and changes no persistent permission rows.
    """
    if scope == "company":
        if scope_id != ctx.workspace_id:
            return False
        scope_pattern = (
            r"\b(?:company[- ]wide|company\s+scope|"
            r"(?:for|to|with)\s+(?:the\s+)?(?:whole\s+)?company)\b"
        )
    elif scope == "team":
        team = await ctx.session.scalar(
            select(Team).where(Team.id == scope_id, Team.workspace_id == ctx.workspace_id)
        )
        if team is None:
            return False
        scope_pattern = rf"(?<!\w){re.escape(team.name)}(?:\s+team)?(?!\w)"
        if scope_id == await primary_team_id(ctx.session, ctx.workspace_id, ctx.agent_id):
            scope_pattern += (
                r"|\b(?:team[- ]wide|team\s+scope|"
                r"(?:for|to|with)\s+(?:the\s+)?(?:whole\s+)?team)\b"
            )
    else:
        return False
    rows = (
        await ctx.session.scalars(
            select(Message)
            .where(
                Message.workspace_id == ctx.workspace_id,
                Message.task_id == ctx.task_id,
                Message.sender_type == "user",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(20)
        )
    ).all()
    for message in rows:
        text = message.content_json.get("text", "")
        if not isinstance(text, str):
            continue
        decision = _scope_instruction(text, scope_pattern)
        if decision is not None:
            return decision and await human_message_authorized(
                ctx.session, message, required_scope="variables:write"
            )
    return False


def _scope_instruction(text: str, scope_pattern: str) -> bool | None:
    """A scoped prohibition overrides approval in the same current request."""
    text = re.sub(r"```[\s\S]*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|(?<!\w)'[^']*'(?!\w)", "", text)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    clauses = re.split(r"[;\n]|(?<=[.!?])\s+|\bbut\b", text, flags=re.I)
    positive = False
    for clause in clauses:
        if not re.search(scope_pattern, clause, re.I):
            continue
        if re.search(
            r"\b(?:no|not|never|cannot|can't|don't|doesn't|without|avoid|forbid|stop|unless|"
            r"if|either|or|maybe|perhaps|might|whether|example|quoted?|said|says)\b",
            clause.replace("\u2019", "'"),
            re.I,
        ):
            return False
        if re.search(
            r"\b(?:create|save|store|set|update|replace|delete|remove|share|copy|keep)\b",
            clause,
            re.I,
        ):
            positive = True
    return True if positive else None


async def _actor(
    ctx: ToolExecutionContext, scope: str | None = None, scope_id: UUID | None = None
) -> VariableActor:
    permissions: set[tuple[str, UUID]] = set()
    for grant in ctx.authorizing_grants:
        if grant.effect != GrantEffect.ALLOW or not capability_matches(
            grant.capability, VARIABLE_WRITE_CAPABILITY
        ):
            continue
        if scope in {"team", "company"} and scope_id is not None:
            if {"scope", "scope_id"}.issubset(grant.scope) and scope_matches(
                grant.scope, {"scope": scope, "scope_id": str(scope_id)}
            ):
                permissions.add((scope, scope_id))
            continue
        granted_scope, granted_id = grant.scope.get("scope"), grant.scope.get("scope_id")
        if granted_scope in {"team", "company"} and granted_id:
            try:
                permissions.add((granted_scope, UUID(str(granted_id))))
            except ValueError:
                continue
    if (
        scope in {"team", "company"}
        and scope_id is not None
        and await human_scope_authorized(ctx, scope, scope_id)
    ):
        permissions.add((scope, scope_id))
    task = await ctx.session.scalar(
        select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
    )
    return VariableActor(
        ctx.workspace_id,
        "agent",
        ctx.agent_id,
        write_scopes=frozenset(permissions),
        conversation_id=task.conversation_id if task else None,
    )


def _error(exc: VariableError) -> ToolExecutionError:
    return ToolExecutionError(
        str(exc), code="variable_refused", hint=str(exc), side_effect_possible=False
    )


async def validate_variable_write(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Resolve ID-based writes before matching scoped allow and deny grants."""
    try:
        store = VariableStore(ctx.session)
        scope: str
        if isinstance(payload, VariableCopyInput):
            await store.get(await _actor(ctx), payload.source_variable_id)
            scope, identifier = payload.scope, payload.scope_id
        elif isinstance(payload, VariableDeleteInput) or (
            isinstance(payload, VariableSetInput) and payload.variable_id
        ):
            assert payload.variable_id is not None
            current = await store.get(await _actor(ctx), payload.variable_id)
            scope, identifier = current.scope, current.scope_id
        elif isinstance(payload, VariableSetInput):
            scope = payload.scope or "agent"
            target = payload.scope_id or (
                ctx.agent_id
                if scope == "agent"
                else ctx.workspace_id
                if scope == "company"
                else await primary_team_id(ctx.session, ctx.workspace_id, ctx.agent_id)
            )
            if target is None:
                raise VariableError("Choose a current team for this variable", 422)
            identifier = target
        else:
            raise VariableError("Invalid variable operation", 422)
        requested = {"scope": scope, "scope_id": str(identifier)}
        matching = tuple(
            grant
            for grant in grants
            if capability_matches(grant.capability, VARIABLE_WRITE_CAPABILITY)
            and scope_matches(grant.scope, requested)
        )
        if any(grant.effect == GrantEffect.DENY for grant in matching):
            raise VariableError("Variable scope is explicitly denied")
        allowed = tuple(grant for grant in matching if grant.effect == GrantEffect.ALLOW)
        if not allowed:
            raise VariableError("No variable write grant covers this scope")
        actor = await _actor(replace(ctx, authorizing_grants=allowed), scope, identifier)
        await store._authorize(actor, scope, identifier, write=True)
    except VariableError as exc:
        return PolicyDecision(decision=DecisionType.DENY, code="variable_refused", reason=str(exc))
    return None


async def list_variables(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(VariableListInput, payload)
    store = VariableStore(ctx.session)
    try:
        rows = await store.list(
            await _actor(ctx), scope=data.scope, scope_id=data.scope_id, limit=data.limit
        )
        scopes, truncated = await _accessible_scopes(ctx, data)
        return VariableOutput(
            items=[store.public(row) for row in rows],
            accessible_scopes=scopes,
            accessible_scopes_truncated=truncated,
        )
    except VariableError as exc:
        raise _error(exc) from None


async def _accessible_scopes(
    ctx: ToolExecutionContext, data: VariableListInput
) -> tuple[list[VariableScopeOutput], bool]:
    """Discover current destinations; this metadata confers no write authority."""
    identity = (
        await ctx.session.execute(
            select(Agent.name, Workspace.name)
            .join(Workspace, Workspace.id == Agent.workspace_id)
            .where(
                Agent.id == ctx.agent_id,
                Agent.workspace_id == ctx.workspace_id,
                Agent.status == "active",
            )
        )
    ).one_or_none()
    if identity is None:
        raise VariableError("Variable not found", 404)
    query = select(Team.id, Team.name).where(
        Team.workspace_id == ctx.workspace_id,
        Team.id.in_(await active_team_ids(ctx.session, ctx.workspace_id, ctx.agent_id)),
    )
    if data.scope == "team" and data.scope_id is not None:
        query = query.where(Team.id == data.scope_id)
    teams = (await ctx.session.execute(query.order_by(Team.name, Team.id).limit(101))).all()
    return (
        [VariableScopeOutput(scope="agent", scope_id=ctx.agent_id, name=identity[0])]
        + [
            VariableScopeOutput(scope="team", scope_id=team.id, name=team.name)
            for team in teams[:100]
        ]
        + [VariableScopeOutput(scope="company", scope_id=ctx.workspace_id, name=identity[1])],
        len(teams) > 100,
    )


async def get_variable(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(VariableGetInput, payload)
    store = VariableStore(ctx.session)
    try:
        return VariableOutput(
            item=store.public(await store.get(await _actor(ctx), data.variable_id))
        )
    except VariableError as exc:
        raise _error(exc) from None


async def set_variable(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(VariableSetInput, payload)
    store = VariableStore(ctx.session, ctx.crypto)
    try:
        values = data.model_dump(exclude_unset=True)
        scope_id: UUID | None
        if data.variable_id is not None:
            current = await store.get(await _actor(ctx), data.variable_id)
            scope, scope_id = current.scope, current.scope_id
        else:
            scope = data.scope or "agent"
            scope_id = data.scope_id or (
                ctx.agent_id
                if scope == "agent"
                else ctx.workspace_id
                if scope == "company"
                else await primary_team_id(ctx.session, ctx.workspace_id, ctx.agent_id)
            )
            if scope_id is None:
                raise VariableError("Choose a current team for this variable", 422)
            values.update(scope=scope, scope_id=scope_id)
        if data.secret_ref:
            values["sensitive"] = True
        row = await store.set(await _actor(ctx, scope, scope_id), **values)
        return VariableOutput(item=store.public(row))
    except VariableError as exc:
        raise _error(exc) from None


async def delete_variable(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(VariableDeleteInput, payload)
    store = VariableStore(ctx.session)
    try:
        row = await store.get(await _actor(ctx), data.variable_id)
        await store.delete(
            await _actor(ctx, row.scope, row.scope_id),
            row.id,
            expected_version=data.expected_version,
        )
        return VariableOutput(deleted=True)
    except VariableError as exc:
        raise _error(exc) from None


async def copy_variable(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(VariableCopyInput, payload)
    store = VariableStore(ctx.session, ctx.crypto)
    try:
        row = await store.copy(
            await _actor(ctx, data.scope, data.scope_id),
            data.source_variable_id,
            expected_version=data.expected_version,
            scope=data.scope,
            scope_id=data.scope_id,
            name=data.name,
        )
        return VariableOutput(item=store.public(row))
    except VariableError as exc:
        raise _error(exc) from None


VARIABLE_TOOLS: Sequence[tuple[ToolDefinition, ToolExecutor, ToolValidator | None]] = (
    (
        ToolDefinition(
            name="variables.list",
            description=(
                "List settings you can currently access, including private agent, active team "
                "and company settings. Sensitive entries return metadata only. Always returns "
                "accessible_scopes with current names and exact scope_id values, even when "
                "items is empty. Use these IDs with variables.set/copy; never guess IDs or "
                "delegate their discovery. Discovery does not authorize shared writes. Up to "
                "100 teams are returned; accessible_scopes_truncated reports omitted teams. "
                "A team scope_id filter also narrows team discovery to that accessible team."
            ),
            risk=RiskLevel.READ,
            input_model=VariableListInput,
            output_model=VariableOutput,
            required_capability=VARIABLE_READ_CAPABILITY,
        ),
        list_variables,
        None,
    ),
    (
        ToolDefinition(
            name="variables.get",
            description=(
                "Read one authorized setting. Plaintext values are returned; sensitive values "
                "remain write-only metadata. Use a connector binding to consume a secret."
            ),
            risk=RiskLevel.READ,
            input_model=VariableGetInput,
            output_model=VariableOutput,
            required_capability=VARIABLE_READ_CAPABILITY,
        ),
        get_variable,
        None,
    ),
    (
        ToolDefinition(
            name="variables.set",
            description=(
                "Create or replace a scoped setting. Defaults to your private agent scope. "
                "For sensitive values pass the opaque secret_ref from secure chat intake, "
                "never plaintext. Updates require expected_version. Team/company writes "
                "require an explicit current admin request or a matching scoped grant. "
                "This tool does not connect an app; use the native connector setup tool "
                "after required inputs are confirmed. Use variables.copy to share an "
                "existing private variable without asking for its secret again. Discover "
                "exact destination scope_id values through variables.list accessible_scopes."
            ),
            risk=RiskLevel.WRITE,
            input_model=VariableSetInput,
            output_model=VariableOutput,
            required_capability=VARIABLE_WRITE_CAPABILITY,
            supports_approval=True,
            defers_scope=True,
        ),
        set_variable,
        validate_variable_write,
    ),
    (
        ToolDefinition(
            name="variables.delete",
            description=(
                "Delete an authorized setting using its expected_version. Apps bound to "
                "this credential are disabled and disconnected atomically. This does not "
                "grant access or reveal secrets."
            ),
            risk=RiskLevel.WRITE,
            input_model=VariableDeleteInput,
            output_model=VariableOutput,
            required_capability=VARIABLE_WRITE_CAPABILITY,
            supports_approval=True,
            defers_scope=True,
        ),
        delete_variable,
        validate_variable_write,
    ),
    (
        ToolDefinition(
            name="variables.copy",
            description=(
                "Copy an accessible setting to an authorized scope, preserving the original. "
                "Sensitive values are re-encrypted internally and never revealed. Requires "
                "the source expected_version and explicit current admin scope authority "
                "or a matching scoped grant. Existing conflicting names are refused. Use "
                "variables.list accessible_scopes for exact destination names and scope_id "
                "values, including when no variables exist in the destination yet."
            ),
            risk=RiskLevel.WRITE,
            input_model=VariableCopyInput,
            output_model=VariableOutput,
            required_capability=VARIABLE_WRITE_CAPABILITY,
            supports_approval=True,
            defers_scope=True,
        ),
        copy_variable,
        validate_variable_write,
    ),
)
