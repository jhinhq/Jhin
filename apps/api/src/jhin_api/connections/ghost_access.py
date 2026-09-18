"""Access diagnostics use the same scoped-variable rules as Ghost execution."""

from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.ghost.access import ghost_access_allowed, variable_audience
from jhin_connectors.ghost.tools import GHOST_TOOLS
from jhin_db.models import Agent, AgentCapabilityGrant, Connection
from jhin_policy import Grant, GrantEffect
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.sanitize import sanitize_payload


async def ghost_agent_access(
    db: AsyncSession, connection: Connection, *, row_limit: int = 256
) -> list[dict[str, Any]]:
    from jhin_api.connections.service import _capability_pattern_candidates

    definitions = tuple(definition for definition, _ in GHOST_TOOLS)
    rows = await db.stream(
        select(Agent, AgentCapabilityGrant)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .where(
            Agent.workspace_id == connection.workspace_id,
            Agent.status == "active",
            AgentCapabilityGrant.workspace_id == connection.workspace_id,
            AgentCapabilityGrant.capability.in_(_capability_pattern_candidates(definitions)),
            AgentCapabilityGrant.effect.in_(("allow", "deny")),
        )
        .execution_options(yield_per=64)
    )
    grouped: dict[UUID, list[tuple[AgentCapabilityGrant, Grant]]] = defaultdict(list)
    contexts: dict[UUID, ToolExecutionContext] = {}
    audiences: dict[UUID, bool] = {}
    count = 0
    try:
        async for agent, row in rows.tuples():
            if agent.id not in contexts:
                ctx = ToolExecutionContext(
                    session=db,
                    workspace_id=connection.workspace_id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    task_id=UUID(int=0),
                    run_id=UUID(int=0),
                )
                contexts[agent.id] = ctx
                audiences[agent.id] = await variable_audience(ctx, connection)
            scope = row.scope_json
            if not isinstance(scope, dict):
                raise HTTPException(503, "Connection access summary is temporarily unavailable")
            if scope.get("variable_audience") is True and not audiences[agent.id]:
                continue
            # Keep only scopes that can influence this connection, including
            # broad denies and explicit manual grants. Never expose credentials.
            from jhin_policy import scope_matches

            target = {"connection_id": str(connection.id)}
            pinned = {"connection_id": scope["connection_id"]} if "connection_id" in scope else {}
            if not scope_matches(pinned, target):
                continue
            count += 1
            if count > row_limit:
                raise HTTPException(503, "Connection access summary is temporarily unavailable")
            grouped[agent.id].append(
                (
                    row,
                    Grant(
                        capability=row.capability,
                        effect=GrantEffect(row.effect),
                        scope=scope,
                    ),
                )
            )
    finally:
        await rows.close()
    output: list[dict[str, Any]] = []
    for agent_id, entries in grouped.items():
        ctx = contexts[agent_id]
        grants = [grant for _, grant in entries]
        names = [
            definition.name
            for definition in definitions
            if await ghost_access_allowed(
                ctx, connection, definition.name, grants, audience=audiences[agent_id]
            )
        ]
        summaries = []
        for row, grant in entries:
            eligible = [
                definition.name
                for definition in definitions
                if await ghost_access_allowed(
                    ctx,
                    connection,
                    definition.name,
                    [
                        Grant(
                            capability=grant.capability, effect=GrantEffect.ALLOW, scope=grant.scope
                        )
                    ],
                    audience=audiences[agent_id],
                )
            ]
            summaries.append(
                {
                    "grant_id": row.id,
                    "capability": row.capability,
                    "effect": row.effect,
                    "scope": sanitize_payload(
                        {
                            key: str(value).lower() if isinstance(value, bool) else str(value)
                            for key, value in row.scope_json.items()
                        }
                    ),
                    "eligible_tool_names": eligible,
                    "eligibility_reason": None
                    if eligible
                    else "Current audience or publisher does not match",
                }
            )
        output.append(
            {
                "agent_id": agent_id,
                "agent_name": ctx.agent_name,
                "authorized": bool(names),
                "authorized_tool_names": names,
                "grants": summaries,
            }
        )
    return sorted(
        output,
        key=lambda item: (
            not item["authorized"],
            item["agent_name"].casefold(),
            str(item["agent_id"]),
        ),
    )
