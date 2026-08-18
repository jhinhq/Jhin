"""Routes for capability grants, approval policies, and the tool catalog.

Reads are viewer+; granting/revoking capabilities and changing approval
policy are admin-only (plan 20.2: admins manage agent access).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.policy import service
from jhin_api.policy.schemas import (
    GrantCreate,
    GrantOut,
    PolicyOut,
    PolicyRuleIn,
    PolicyUpdate,
    ToolOut,
)
from jhin_api.security.csrf import csrf_protect
from jhin_connectors import build_default_catalog

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["policy"],
    dependencies=[Depends(csrf_protect)],
)


@router.get("/tools")
async def list_tools(ctx: ViewerCtx) -> list[ToolOut]:
    """The registered tool catalog: system built-ins plus connector tools."""
    return [
        ToolOut(
            name=definition.name,
            description=definition.description,
            risk=definition.risk.value,
            required_capability=definition.required_capability,
            supports_approval=definition.supports_approval,
            scope_keys=definition.scope_keys,
            required_grant_scope_keys=definition.required_grant_scope_keys,
            input_schema=definition.input_json_schema(),
        )
        for definition in build_default_catalog().definitions()
    ]


@router.get("/agents/{agent_id}/grants")
async def list_grants(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[GrantOut]:
    rows = await service.list_grants(db, ctx.workspace_id, agent_id)
    return [GrantOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/agents/{agent_id}/grants", status_code=201)
async def create_grant(
    agent_id: UUID, payload: GrantCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> GrantOut:
    grant = await service.create_grant(
        db,
        ctx,
        agent_id,
        capability=payload.capability,
        scope=payload.scope,
        effect=payload.effect,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return GrantOut.model_validate(grant, from_attributes=True)


@router.delete("/agents/{agent_id}/grants/{grant_id}", status_code=204)
async def revoke_grant(
    agent_id: UUID, grant_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> None:
    await service.revoke_grant(
        db, ctx, agent_id, grant_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


def _policy_out(agent_rules: list[object], autonomy_level: str) -> PolicyOut:
    rules = service.parse_rules(list(agent_rules))
    return PolicyOut(
        rules=[PolicyRuleIn.model_validate(rule.model_dump(mode="json")) for rule in rules],
        preset=service.preset_of(rules),
        autonomy_level=autonomy_level,
    )


@router.get("/agents/{agent_id}/policy")
async def get_policy(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> PolicyOut:
    agent = await service.get_policy(db, ctx.workspace_id, agent_id)
    return _policy_out(agent.approval_policy_json, agent.autonomy_level)


@router.put("/agents/{agent_id}/policy")
async def update_policy(
    agent_id: UUID, payload: PolicyUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> PolicyOut:
    agent = await service.update_policy(
        db,
        ctx,
        agent_id,
        preset=payload.preset,
        rules=(
            [rule.model_dump(exclude_none=True) for rule in payload.rules]
            if payload.rules is not None
            else None
        ),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _policy_out(agent.approval_policy_json, agent.autonomy_level)
