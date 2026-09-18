"""A small admin control over existing terminal network capability grants."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import service
from jhin_db.models import AgentCapabilityGrant, AuditEvent, Connection
from jhin_domain import ActorType
from jhin_policy import capability_matches, scope_matches

CAPABILITY = "cli.command.execute"
SOURCE = "terminal_internet_control"
OFF_SCOPE = {"network": "internet"}


class TerminalInternetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    connection_id: UUID | None = None


class SandboxChoice(BaseModel):
    id: UUID
    name: str


class TerminalInternetStatus(BaseModel):
    enabled: bool
    status: Literal["enabled", "blocked", "off", "custom"]
    connection_id: UUID | None = None
    connections: list[SandboxChoice]
    has_custom_grants: bool = False


async def _rows(
    db: AsyncSession, ctx: WorkspaceContext, agent_id: UUID
) -> list[AgentCapabilityGrant]:
    return list(
        await db.scalars(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.workspace_id == ctx.workspace_id,
                AgentCapabilityGrant.agent_id == agent_id,
            )
        )
    )


async def _owned_ids(db: AsyncSession, ctx: WorkspaceContext, agent_id: UUID) -> set[str]:
    # The append-only audit proves ownership without adopting an identical
    # handmade grant or requiring a second permission/configuration store.
    records = await db.scalars(
        select(AuditEvent.metadata_json).where(
            AuditEvent.workspace_id == ctx.workspace_id,
            AuditEvent.target_id == agent_id,
            AuditEvent.action == "agent.permission.granted",
            AuditEvent.metadata_json["source"].as_string() == SOURCE,
        )
    )
    return {str(record.get("grant_id")) for record in records}


async def _connections(db: AsyncSession, ctx: WorkspaceContext) -> list[Connection]:
    return list(
        await db.scalars(
            select(Connection)
            .where(
                Connection.workspace_id == ctx.workspace_id,
                Connection.connector_type == "cli",
                Connection.auth_type == "none",
                Connection.status == "active",
            )
            .order_by(Connection.name, Connection.id)
        )
    )


def _canonical_allow(row: AgentCapabilityGrant) -> bool:
    return (
        row.capability == CAPABILITY
        and row.effect == "allow"
        and set(row.scope_json) == {"connection_id", "network", "command"}
        and isinstance(row.scope_json.get("connection_id"), str)
        and row.scope_json.get("network") == "internet"
        and row.scope_json.get("command") == "*"
    )


async def get_status(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    include_connections: bool,
) -> TerminalInternetStatus:
    await service.get_policy(db, ctx.workspace_id, agent_id)
    rows = await _rows(db, ctx, agent_id)
    owned = await _owned_ids(db, ctx, agent_id)
    connections = await _connections(db, ctx)
    active_ids = {str(connection.id) for connection in connections}
    relevant = [row for row in rows if capability_matches(row.capability, CAPABILITY)]
    managed_off = any(
        str(row.id) in owned and row.effect == "deny" and row.scope_json == OFF_SCOPE
        for row in relevant
    )
    candidates = [
        row
        for row in relevant
        if _canonical_allow(row) and row.scope_json["connection_id"] in active_ids
    ]
    candidates.sort(key=lambda row: (str(row.id) not in owned, row.created_at, str(row.id)))
    selected = candidates[0] if candidates else None
    custom = any(str(row.id) not in owned and not _canonical_allow(row) for row in relevant)
    denied = False
    if selected is not None:
        request = {**selected.scope_json, "image": ""}
        denied = any(
            row.effect == "deny" and scope_matches(row.scope_json, request) for row in relevant
        )
    enabled = selected is not None and not managed_off and not denied
    state: Literal["enabled", "blocked", "off", "custom"] = (
        "blocked" if managed_off else "enabled" if enabled else "custom" if relevant else "off"
    )
    return TerminalInternetStatus(
        enabled=enabled,
        status=state,
        connection_id=UUID(selected.scope_json["connection_id"])
        if selected is not None and include_connections
        else None,
        connections=[SandboxChoice(id=row.id, name=row.name) for row in connections]
        if include_connections
        else [],
        has_custom_grants=custom,
    )


async def update(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    payload: TerminalInternetUpdate,
    *,
    request_id: UUID,
    ip_hash: str | None,
    include_connections: bool,
) -> TerminalInternetStatus:
    await service.lock_agent(db, ctx.workspace_id, agent_id)
    target_scope = dict(OFF_SCOPE)
    if payload.enabled:
        choices = await _connections(db, ctx)
        if payload.connection_id is None or all(row.id != payload.connection_id for row in choices):
            raise HTTPException(422, "Choose an active CLI Sandbox in this workspace.")
        target_scope = {
            "connection_id": str(payload.connection_id),
            "network": "internet",
            "command": "*",
        }
        await service.validate_grant(
            db, ctx.workspace_id, capability=CAPABILITY, scope=target_scope, effect="allow"
        )
    effect = "allow" if payload.enabled else "deny"
    rows = await _rows(db, ctx, agent_id)
    owned = await _owned_ids(db, ctx, agent_id)
    for row in rows:
        if str(row.id) not in owned or row.capability != CAPABILITY:
            continue
        if row.effect == effect and row.scope_json == target_scope:
            continue
        audit.record(
            db,
            action="agent.permission.revoked",
            target_type="agent",
            target_id=agent_id,
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.USER,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={
                "grant_id": str(row.id),
                "capability": row.capability,
                "effect": row.effect,
                "source": SOURCE,
            },
        )
        await db.delete(row)
    await db.flush()
    await service._write_grant(
        db,
        ctx,
        agent_id,
        capability=CAPABILITY,
        scope=target_scope,
        effect=effect,
        request_id=request_id,
        ip_hash=ip_hash,
        actor_type=ActorType.USER,
        extra_metadata={"source": SOURCE},
    )
    await db.commit()
    return await get_status(db, ctx, agent_id, include_connections=include_connections)
