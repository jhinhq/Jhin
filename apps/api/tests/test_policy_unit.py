"""Capability grant and tool-catalog API contracts."""

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jhin_connectors.registry
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import router, service
from jhin_api.policy.router import list_tools
from jhin_api.policy.schemas import ToolOut
from jhin_db.models import Agent, AgentCapabilityGrant
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools import ToolDefinitionCatalog


class _CatalogInput(BaseModel):
    connection_id: str


class _CatalogOutput(BaseModel):
    ok: bool


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


async def test_asking_for_a_grant_an_agent_already_has_is_a_no_op(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A grant states that this agent may do something, so asking twice is not
    a conflict. It became reachable in normal use once every new agent started
    holding the default baseline: the wizard creates an agent and then applies
    a preset, which asks for several of those by definition, and refusing told
    the person their agent could not be created when it already had exactly
    what they were asking for."""
    agent = await _agent(session, admin_ctx)
    kwargs = {
        "capability": "github.repository.read",
        "scope": {"connection_id": "connection-1", "repository": "acme/api"},
        "effect": "allow",
        "request_id": new_uuid7(),
        "ip_hash": "test",
    }
    first = await service.create_grant(session, admin_ctx, agent.id, **kwargs)  # type: ignore[arg-type]
    again = await service.create_grant(session, admin_ctx, agent.id, **kwargs)  # type: ignore[arg-type]

    assert again.id == first.id
    rows = list(
        await session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == agent.id)
        )
    )
    assert len(rows) == 1


async def test_a_different_scope_is_still_a_separate_grant(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    base = {
        "capability": "github.repository.read",
        "effect": "allow",
        "request_id": new_uuid7(),
        "ip_hash": "test",
    }
    first = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        scope={"repository": "acme/api"},
        **base,  # type: ignore[arg-type]
    )
    other = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        scope={"repository": "acme/web"},
        **base,  # type: ignore[arg-type]
    )
    assert other.id != first.id


async def test_tool_catalog_exposes_declared_and_required_scope_keys(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    assert {"scope_keys", "required_grant_scope_keys"} <= set(ToolOut.model_fields)

    catalog = ToolDefinitionCatalog()
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
        )
    )
    monkeypatch.setattr(router, "build_default_definition_catalog", lambda: catalog)

    tools = await list_tools(ctx=admin_ctx, db=session)
    scoped = next(tool for tool in tools if tool.name == "test.scoped_catalog")
    assert scoped.scope_keys == ("connection_id",)
    assert scoped.required_grant_scope_keys == ("connection_id",)


async def test_tools_endpoint_uses_definition_only_catalog(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    monkeypatch.setattr(
        jhin_connectors.registry,
        "build_default_catalog",
        lambda: pytest.fail("API attempted executable catalog construction"),
    )

    tools = await list_tools(ctx=admin_ctx, db=session)

    assert {tool.name for tool in tools} >= {"system.echo", "linear.issue.read"}
