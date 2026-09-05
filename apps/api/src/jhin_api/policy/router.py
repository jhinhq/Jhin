"""Routes for capability grants, approval policies, and the tool catalog.

Reads are viewer+; granting/revoking capabilities and changing approval
policy are admin-only (plan 20.2: admins manage agent access).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, SecretCryptoDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.policy import bundles, service
from jhin_api.policy.schemas import (
    BundleApply,
    BundleApplyOut,
    BundleOut,
    BundleRemoveOut,
    BundleStatusOut,
    GrantCreate,
    GrantOut,
    PolicyOut,
    PolicyRuleIn,
    PolicyUpdate,
    ToolOut,
)
from jhin_api.security.csrf import csrf_protect
from jhin_connectors import build_default_definition_catalog
from jhin_connectors.mcp import workspace_mcp_tool_definitions

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["policy"],
    dependencies=[Depends(csrf_protect)],
)


@router.get("/tools")
async def list_tools(ctx: ViewerCtx, db: DbSession) -> list[ToolOut]:
    """The registered tool catalog: system built-ins, connector tools, and
    the tools discovered from this workspace's MCP connections."""
    definitions = list(build_default_definition_catalog().definitions())
    definitions.extend(await workspace_mcp_tool_definitions(db, ctx.workspace_id))
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
        for definition in definitions
    ]


@router.get("/tools/bundles")
async def list_bundles(ctx: ViewerCtx, db: DbSession) -> list[BundleOut]:
    """The capability bundles and whether this workspace can turn each on
    as it stands (docs/operations/agent-access.md)."""
    return await bundles.workspace_bundles(
        db, ctx.workspace_id, include_connections=bundles.may_read_connections(ctx)
    )


@router.get("/agents/{agent_id}/grants")
async def list_grants(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[GrantOut]:
    rows = await service.list_grants(
        db, ctx.workspace_id, agent_id, redact=not bundles.may_read_connections(ctx)
    )
    return [
        GrantOut.model_validate(row, from_attributes=True).model_copy(
            update={"problems": problems, "connection_name": connection_name}
        )
        for row, problems, connection_name in rows
    ]


@router.get("/agents/{agent_id}/bundles")
async def list_agent_bundles(
    agent_id: UUID, ctx: ViewerCtx, db: DbSession
) -> list[BundleStatusOut]:
    """Every bundle as it stands on this agent: on, partial, or off, and
    which of its grants cannot work as written."""
    return await bundles.agent_bundles(
        db, ctx.workspace_id, agent_id, include_connections=bundles.may_read_connections(ctx)
    )


@router.post("/agents/{agent_id}/bundles/{bundle_id}")
async def apply_bundle(
    agent_id: UUID,
    bundle_id: str,
    payload: BundleApply,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> BundleApplyOut:
    """Turn a bundle on: write its grants (and, for Code editing, the sandbox
    it runs in) in one transaction, or say what is still needed."""
    return await bundles.apply_bundle(
        db,
        crypto,
        ctx,
        agent_id,
        bundle_id,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        include_connections=bundles.may_read_connections(ctx),
    )


@router.delete("/agents/{agent_id}/bundles/{bundle_id}")
async def remove_bundle(
    agent_id: UUID,
    bundle_id: str,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    dry_run: bool = False,
) -> BundleRemoveOut:
    """Turn a bundle off: revoke the grants it owns that no other bundle
    still needs. Policy rules stay."""
    return await bundles.remove_bundle(
        db,
        ctx,
        agent_id,
        bundle_id,
        dry_run=dry_run,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


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
    (_row, problems, connection_name), *_ = await service.annotate_grants(
        db, ctx.workspace_id, [grant]
    )
    return GrantOut.model_validate(grant, from_attributes=True).model_copy(
        update={"problems": problems, "connection_name": connection_name}
    )


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
