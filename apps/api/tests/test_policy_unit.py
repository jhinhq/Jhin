"""Capability grant and tool-catalog API contracts."""

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.policy import router, service
from jhin_api.policy.router import list_tools
from jhin_api.policy.schemas import ToolOut
from jhin_db.models import Agent, AgentCapabilityGrant
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools import ToolCatalog, ToolExecutionContext


class _CatalogInput(BaseModel):
    connection_id: str


class _CatalogOutput(BaseModel):
    ok: bool


async def _catalog_executor(_ctx: ToolExecutionContext, _payload: BaseModel) -> _CatalogOutput:
    return _CatalogOutput(ok=True)


async def _agent(session: AsyncSession, ctx: WorkspaceContext) -> Agent:
    agent = Agent(
        workspace_id=ctx.workspace_id,
        name="Scoped Worker",
        slug=f"scoped-worker-{new_uuid7().hex[:8]}",
    )
    session.add(agent)
    await session.flush()
    return agent


async def test_same_capability_can_have_distinct_scoped_grants(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    request_id = new_uuid7()

    first = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="github.repository.read",
        scope={"connection_id": "connection-1", "repository": "acme/api"},
        effect="allow",
        request_id=request_id,
        ip_hash="test",
    )
    second = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="github.repository.read",
        scope={"connection_id": "connection-1", "repository": "acme/web"},
        effect="allow",
        request_id=request_id,
        ip_hash="test",
    )

    assert first.id != second.id
    rows = (await session.scalars(select(AgentCapabilityGrant))).all()
    assert {row.scope_json["repository"] for row in rows} == {"acme/api", "acme/web"}


async def test_exact_duplicate_scoped_grant_still_conflicts(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    kwargs = {
        "capability": "github.repository.read",
        "scope": {"connection_id": "connection-1", "repository": "acme/api"},
        "effect": "allow",
        "request_id": new_uuid7(),
        "ip_hash": "test",
    }
    await service.create_grant(session, admin_ctx, agent.id, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as excinfo:
        await service.create_grant(
            session,
            admin_ctx,
            agent.id,
            **kwargs,  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 409


async def test_tool_catalog_exposes_declared_and_required_scope_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert {"scope_keys", "required_grant_scope_keys"} <= set(ToolOut.model_fields)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.scoped_catalog",
            description="Scoped catalog contract",
            risk=RiskLevel.READ,
            input_model=_CatalogInput,
            output_model=_CatalogOutput,
            required_capability="test.scoped_catalog",
            scope_keys=("connection_id",),
            required_grant_scope_keys=("connection_id",),
        ),
        _catalog_executor,
    )
    monkeypatch.setattr(router, "build_default_catalog", lambda: catalog)

    tools = await list_tools(ctx=None)  # type: ignore[arg-type]
    scoped = next(tool for tool in tools if tool.name == "test.scoped_catalog")
    assert scoped.scope_keys == ("connection_id",)
    assert scoped.required_grant_scope_keys == ("connection_id",)
