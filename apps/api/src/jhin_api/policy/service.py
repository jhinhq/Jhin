"""Capability grants and approval-policy business logic (plan 6.6, 42, 23).

Grants are the only way an agent gains a capability, and only workspace
admins may create or revoke them through this API — agents themselves have no
tool that can reach these endpoints (plan 21.7). Every change is audited as
``agent.permission.granted`` / ``agent.permission.revoked``.

No grant writer here may persist a row the evaluator refuses: a new allow
grant is checked against the workspace's catalog and connections first
(:func:`validate_grant`), and the rows an agent already holds are annotated
with whatever is wrong with them (:func:`annotate_grants`) so a dead grant is
visible instead of silently denying every call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service as agents_service
from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.policy.refs import connection_refs
from jhin_connectors import build_default_definition_catalog
from jhin_connectors.mcp import workspace_mcp_tool_definitions
from jhin_db.models import Agent, AgentCapabilityGrant
from jhin_domain import ActorType
from jhin_policy import (
    ApprovalPreset,
    ConnectionRef,
    PolicyRule,
    ToolDefinition,
    capability_rules,
    grant_pattern_problem,
    grant_problem_details,
    matching_preset,
    neutral_problem_text,
    rules_for_preset,
)
from jhin_policy.bundles import REFUSED_PROBLEM_KINDS

# What the router maps onto ``GrantOut``: the row, its problems, and the
# name of the connection it pins (when that connection still exists).
AnnotatedGrantRow = tuple[AgentCapabilityGrant, list[str], str | None]


async def workspace_catalog(db: AsyncSession, workspace_id: UUID) -> list[ToolDefinition]:
    """Static connector tools plus this workspace's discovered MCP tools —
    the same view ``GET /tools`` serves."""
    definitions = list(build_default_definition_catalog().definitions())
    definitions.extend(await workspace_mcp_tool_definitions(db, workspace_id))
    return definitions


def annotate_grant(
    row: AgentCapabilityGrant,
    *,
    catalog: Sequence[ToolDefinition],
    connections: Sequence[ConnectionRef],
    redact: bool = False,
) -> tuple[list[str], str | None]:
    """A row's problems and its pinned connection's name.

    ``redact`` is for a caller who may not read the connection inventory
    (``bundles.may_read_connections``): the name is withheld and each
    sentence that would carry a name, a status or an allow-list is replaced
    by its neutral form, so the row still reads as dead without saying what
    the inventory would.
    """
    scope = row.scope_json if isinstance(row.scope_json, dict) else {}
    details = grant_problem_details(
        capability=row.capability,
        scope=scope,
        effect=row.effect,
        catalog=catalog,
        connections=connections,
    )
    problems = [neutral_problem_text(problem) if redact else problem.text for problem in details]
    if redact:
        return problems, None
    pinned = scope.get("connection_id")
    connection_name = next(
        (connection.name for connection in connections if connection.id == pinned), None
    )
    return problems, connection_name


async def annotate_grants(
    db: AsyncSession,
    workspace_id: UUID,
    rows: Sequence[AgentCapabilityGrant],
    *,
    redact: bool = False,
) -> list[AnnotatedGrantRow]:
    """Problems and connection names for a set of rows, computed once per
    request rather than once per row."""
    if not rows:
        return []
    catalog = await workspace_catalog(db, workspace_id)
    connections = await connection_refs(db, workspace_id)
    annotated: list[AnnotatedGrantRow] = []
    for row in rows:
        problems, connection_name = annotate_grant(
            row, catalog=catalog, connections=connections, redact=redact
        )
        annotated.append((row, problems, connection_name))
    return annotated


async def list_grants(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID, *, redact: bool = False
) -> list[AnnotatedGrantRow]:
    await agents_service.get_agent(db, workspace_id, agent_id)  # 404 if missing
    rows = await db.scalars(
        select(AgentCapabilityGrant)
        .where(
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == workspace_id,
        )
        .order_by(AgentCapabilityGrant.created_at)
    )
    return await annotate_grants(db, workspace_id, list(rows), redact=redact)


async def validate_grant(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    capability: str,
    scope: dict[str, str],
    effect: str,
) -> None:
    """Refuse a grant that can never be held, or that the evaluator would
    deny on every call.

    Refused (422) for either effect: a capability that is not a name or
    pattern, or sits in a namespace no agent may ever hold. Refused for an
    allow grant: a wildcard over tools that require scope, a missing
    required key, an unknown scope key, a malformed repository, a repository
    outside the pinned sandbox's allow-list, a branch the push tool refuses,
    and a connection that does not exist or is of the wrong type. Accepted,
    and only reported back in ``GrantOut.problems``: a capability the
    catalog does not know yet (MCP servers register tools after the
    connection exists) and a connection that is merely disabled or waiting
    to be reconnected — those rows come alive without being rewritten.

    This is the one path every writer goes through — the route (after its
    schema said the same), the bundles and the console — so all of them
    refuse in the same words.
    """
    pattern_problem = grant_pattern_problem(capability)
    if pattern_problem is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=pattern_problem
        )
    if effect != "allow":
        return
    catalog = await workspace_catalog(db, workspace_id)
    connections = await connection_refs(db, workspace_id)
    refused = [
        problem.text
        for problem in grant_problem_details(
            capability=capability,
            scope=scope,
            effect=effect,
            catalog=catalog,
            connections=connections,
        )
        if problem.kind in REFUSED_PROBLEM_KINDS
    ]
    if refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=" ".join(refused)
        )


async def _write_grant(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    capability: str,
    scope: dict[str, str],
    effect: str,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType,
    extra_metadata: dict[str, Any] | None,
) -> tuple[AgentCapabilityGrant, bool]:
    """Insert one grant row (or find the identical one) and audit it.

    No commit: the caller owns the transaction, and must hold the agent's
    row lock (see :func:`create_grant`) so two concurrent identical requests
    cannot both pass the pre-insert check. Returns ``(row, created)``.
    """
    same_capability_rows = await db.scalars(
        select(AgentCapabilityGrant).where(
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == ctx.workspace_id,
            AgentCapabilityGrant.capability == capability,
            AgentCapabilityGrant.effect == effect,
        )
    )
    requested_scope = dict(scope)
    # Idempotent, not a conflict. A grant is a statement that this agent may do
    # something, so asking for one it already has is a no-op -- and since every
    # new agent is now created holding the default baseline, the create-then-
    # apply-a-preset flow the wizard uses asks for several of them by
    # definition. Refusing that told the person their agent could not be
    # created, when it already had exactly what they were asking for.
    existing = next(
        (row for row in same_capability_rows if row.scope_json == requested_scope), None
    )
    if existing is not None:
        return existing, False

    grant = AgentCapabilityGrant(
        workspace_id=ctx.workspace_id,
        agent_id=agent_id,
        capability=capability,
        scope_json=requested_scope,
        effect=effect,
    )
    db.add(grant)
    await db.flush()
    audit.record(
        db,
        action="agent.permission.granted",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_type=actor_type,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "grant_id": str(grant.id),
            "capability": capability,
            "scope": scope,
            "effect": effect,
            **(extra_metadata or {}),
        },
    )
    return grant, True


async def lock_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> None:
    """Serialize grant-set mutations on the owning agent. PostgreSQL releases
    this row lock only after the insert/duplicate decision commits."""
    locked_agent_id = await db.scalar(
        select(Agent.id)
        .where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        .with_for_update()
    )
    if locked_agent_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")


async def create_grant(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    capability: str,
    scope: dict[str, str],
    effect: str,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
) -> AgentCapabilityGrant:
    await lock_agent(db, ctx.workspace_id, agent_id)
    await validate_grant(db, ctx.workspace_id, capability=capability, scope=scope, effect=effect)
    grant, _created = await _write_grant(
        db,
        ctx,
        agent_id,
        capability=capability,
        scope=scope,
        effect=effect,
        request_id=request_id,
        ip_hash=ip_hash,
        actor_type=actor_type,
        extra_metadata=extra_metadata,
    )
    await db.commit()
    return grant


async def revoke_grant(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    grant_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    grant = await db.scalar(
        select(AgentCapabilityGrant).where(
            AgentCapabilityGrant.id == grant_id,
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == ctx.workspace_id,
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    audit.record(
        db,
        action="agent.permission.revoked",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_type=actor_type,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "grant_id": str(grant.id),
            "capability": grant.capability,
            "effect": grant.effect,
            **(extra_metadata or {}),
        },
    )
    await db.delete(grant)
    await db.commit()


def parse_rules(raw: list[Any]) -> list[PolicyRule]:
    rules: list[PolicyRule] = []
    for item in raw:
        try:
            rules.append(PolicyRule.model_validate(item))
        except ValueError:
            continue  # malformed persisted rules are skipped, not fatal
    return rules


async def ensure_capability_rules(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent: Agent,
    rules: Sequence[PolicyRule],
    *,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType,
    extra_metadata: dict[str, Any] | None,
    bundle_id: str,
) -> list[PolicyRule]:
    """Prepend the per-capability rules a bundle insists on, unless the
    policy already speaks for that capability (an operator who set
    ``cli.repository.push`` to run automatically decided that on purpose).

    Prepended, because rules are first-match and a risk-level rule ahead of
    one of these would answer for the capability before it was reached. No
    commit; called only while the agent row is locked. Returns what was
    added.
    """
    existing = parse_rules(list(agent.approval_policy_json or []))
    spoken_for = {rule.capability for rule in existing}
    missing = [rule for rule in rules if rule.capability not in spoken_for]
    if not missing:
        return []
    agent.approval_policy_json = [rule.model_dump(mode="json") for rule in (*missing, *existing)]
    audit.record(
        db,
        action="agent.policy.updated",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_type=actor_type,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "preset": None,
            "rules": agent.approval_policy_json,
            "bundle": bundle_id,
            "added": [rule.model_dump(mode="json") for rule in missing],
            **(extra_metadata or {}),
        },
    )
    return missing


async def get_policy(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    return await agents_service.get_agent(db, workspace_id, agent_id)


async def update_policy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    preset: str | None,
    rules: list[dict[str, Any]] | None,
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    """Persist explicit rules; a preset is expanded before persisting (plan 42).

    A preset answers for risk levels, so choosing one restates those and keeps
    the agent's per-capability rules — the approval gate the Code-editing
    bundle puts on ``cli.repository.push`` is one of those, and a mode switch
    in the chat sidebar is not a decision to remove it. They are kept ahead of
    the preset's rules because matching is first-match. Sending explicit
    ``rules`` still means exactly what it says: that list becomes the policy,
    and it is how a rule is deliberately removed.
    """
    agent = await agents_service.get_agent(db, ctx.workspace_id, agent_id)

    if preset is not None and rules is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide either a preset or explicit rules, not both",
        )
    if preset is not None:
        kept = capability_rules(parse_rules(list(agent.approval_policy_json or [])))
        new_rules = [*kept, *rules_for_preset(ApprovalPreset(preset))]
    elif rules is not None:
        new_rules = [PolicyRule.model_validate(rule) for rule in rules]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide a preset or explicit rules",
        )

    agent.approval_policy_json = [rule.model_dump(mode="json") for rule in new_rules]
    audit.record(
        db,
        action="agent.policy.updated",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "preset": preset,
            "rules": agent.approval_policy_json,
        },
    )
    await db.commit()
    return agent


def preset_of(rules: list[PolicyRule]) -> str | None:
    preset = matching_preset(rules)
    return preset.value if preset is not None else None
