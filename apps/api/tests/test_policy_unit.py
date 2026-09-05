"""Capability grant and tool-catalog API contracts.

A grant is refused when the evaluator would deny it on every call, and a row
that is merely dead today (a lapsed connection, a capability the catalog does
not know yet) is written and reported back with its problems.
"""

from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jhin_connectors.registry
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import router, service
from jhin_api.policy.router import list_tools
from jhin_api.policy.schemas import GrantCreate, GrantOut, ToolOut
from jhin_db.models import Agent, AgentCapabilityGrant, Connection
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, new_uuid7
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


async def _connection(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    connector_type: str = "github",
    name: str = "GitHub",
    status: str = ConnectionStatus.ACTIVE.value,
    config: dict[str, object] | None = None,
) -> Connection:
    """A real connection row: validation checks the pinned id against the
    workspace, so a made-up string is exactly what must be refused. A
    sandbox is made with the allow-list the case is about, because that
    list is the outer limit under every grant pinned to it."""
    connection = Connection(
        workspace_id=ctx.workspace_id,
        connector_type=connector_type,
        name=name,
        auth_type="pat",
        public_id=new_public_id(),
        status=status,
        config_json=dict(config or {}),
    )
    session.add(connection)
    await session.flush()
    return connection


ANY_REPOSITORY: dict[str, object] = {"allowed_repositories": ["*"]}


def _kwargs(**overrides: object) -> dict[str, object]:
    return {
        "capability": "github.repository.read",
        "effect": "allow",
        "request_id": new_uuid7(),
        "ip_hash": "test",
        **overrides,
    }


async def test_same_capability_can_have_distinct_scoped_grants(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _connection(session, admin_ctx)

    first = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"connection_id": str(github.id), "repository": "acme/api"}),  # type: ignore[arg-type]
    )
    second = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"connection_id": str(github.id), "repository": "acme/web"}),  # type: ignore[arg-type]
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
    github = await _connection(session, admin_ctx)
    kwargs = _kwargs(scope={"connection_id": str(github.id), "repository": "acme/api"})
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
    first = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"repository": "acme/api"}),  # type: ignore[arg-type]
    )
    other = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"repository": "acme/web"}),  # type: ignore[arg-type]
    )
    assert other.id != first.id


async def _refused(
    session: AsyncSession, ctx: WorkspaceContext, agent: Agent, **overrides: object
) -> str:
    with pytest.raises(HTTPException) as caught:
        await service.create_grant(session, ctx, agent.id, **_kwargs(**overrides))  # type: ignore[arg-type]
    assert caught.value.status_code == 422
    rows = list(
        await session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == agent.id)
        )
    )
    assert rows == []
    return str(caught.value.detail)


async def test_a_push_grant_without_its_branch_is_refused_by_sentence(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    sandbox = await _connection(
        session, admin_ctx, connector_type="cli", name="Sandbox", config=ANY_REPOSITORY
    )

    detail = await _refused(
        session,
        admin_ctx,
        agent,
        capability="cli.repository.push",
        scope={"connection_id": str(sandbox.id), "repository": "*"},
    )

    assert detail == (
        "cli.repository.push needs branch in its grant scope; a grant without it is refused "
        "on every call."
    )


async def test_a_wildcard_over_tools_that_require_scope_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    sandbox = await _connection(session, admin_ctx, connector_type="cli", name="Sandbox")

    detail = await _refused(
        session, admin_ctx, agent, capability="cli.*", scope={"connection_id": str(sandbox.id)}
    )

    assert detail.startswith("A wildcard grant cannot carry the scope")


async def test_an_mcp_wildcard_and_an_unknown_capability_are_accepted_with_a_problem(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """MCP servers register tools after the connection exists, so a row the
    catalog cannot match yet is written — and says so, rather than being
    refused or silently dead."""
    agent = await _agent(session, admin_ctx)

    wildcard = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(capability="mcp.demo.*", scope={}),  # type: ignore[arg-type]
    )
    unknown = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(capability="acme.widgets.read", scope={}),  # type: ignore[arg-type]
    )

    annotated = await service.list_grants(session, admin_ctx.workspace_id, agent.id)
    problems = {row.id: problems for row, problems, _name in annotated}
    assert problems[wildcard.id] == ["Matches no tool in this workspace's catalog."]
    assert problems[unknown.id] == ["Matches no tool in this workspace's catalog."]


async def test_a_connection_that_does_not_exist_or_is_the_wrong_type_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    web = await _connection(session, admin_ctx, connector_type="web", name="Web")

    missing = await _refused(session, admin_ctx, agent, scope={"connection_id": str(new_uuid7())})
    assert missing == "Connection no longer exists."
    bogus = await _refused(session, admin_ctx, agent, scope={"connection_id": "connection-1"})
    assert bogus == "Connection no longer exists."
    wrong = await _refused(session, admin_ctx, agent, scope={"connection_id": str(web.id)})
    assert wrong == "Connection 'Web' is a web connection, not github."


async def test_a_disabled_connection_is_accepted_and_reported(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _connection(
        session, admin_ctx, name="Old GitHub", status=ConnectionStatus.DISABLED.value
    )

    grant = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"connection_id": str(github.id)}),  # type: ignore[arg-type]
    )

    (row, problems, connection_name), *_ = await service.list_grants(
        session, admin_ctx.workspace_id, agent.id
    )
    assert row.id == grant.id
    assert problems == ["Connection 'Old GitHub' is disabled."]
    assert connection_name == "Old GitHub"


async def test_a_malformed_repository_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _connection(session, admin_ctx)

    detail = await _refused(
        session, admin_ctx, agent, scope={"connection_id": str(github.id), "repository": "../x"}
    )

    assert detail == "repository must be owner/name, owner/*, or *."


async def test_deny_grants_are_not_validated(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A deny that matches nothing denies nothing; refusing it would only stop
    an admin from writing the deny before the tool exists."""
    agent = await _agent(session, admin_ctx)

    grant = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(
            capability="cli.repository.push", scope={"connection_id": "anything"}, effect="deny"
        ),  # type: ignore[arg-type]
    )

    (row, problems, _name), *_ = await service.list_grants(
        session, admin_ctx.workspace_id, agent.id
    )
    assert row.id == grant.id
    assert problems == []


async def test_grant_out_carries_problems_and_the_connection_name(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _connection(session, admin_ctx, name="GitHub main")
    grant = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"connection_id": str(github.id), "repository": "*"}),  # type: ignore[arg-type]
    )
    (row, problems, connection_name), *_ = await service.list_grants(
        session, admin_ctx.workspace_id, agent.id
    )

    out = GrantOut.model_validate(row, from_attributes=True).model_copy(
        update={"problems": problems, "connection_name": connection_name}
    )

    assert out.id == grant.id
    assert out.problems == []
    assert out.connection_name == "GitHub main"
    assert {"problems", "connection_name"} <= set(GrantOut.model_fields)


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


async def test_validation_never_loads_an_executable_catalog(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    monkeypatch.setattr(
        jhin_connectors.registry,
        "build_default_catalog",
        lambda: pytest.fail("grant validation attempted executable catalog construction"),
    )
    agent = await _agent(session, admin_ctx)
    github = await _connection(session, admin_ctx)

    grant = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(scope={"connection_id": str(github.id)}),  # type: ignore[arg-type]
    )

    assert isinstance(grant.id, UUID)


async def test_a_capability_no_agent_may_hold_is_refused_by_the_service_in_the_schema_s_words(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """The console never sees ``GrantCreate``: it once wrote an allow row in a
    namespace agents must never hold. Now the service refuses on its own,
    for either effect, with the sentence the schema uses."""
    agent = await _agent(session, admin_ctx)

    for capability, sentence in (
        (
            "agent.permission.grant",
            "capabilities in this namespace can never be granted to agents",
        ),
        ("GitHub.Repository.Read", "not a valid dotted capability name or pattern"),
    ):
        for effect in ("allow", "deny"):
            detail = await _refused(
                session, admin_ctx, agent, capability=capability, scope={}, effect=effect
            )
            assert detail == sentence
        with pytest.raises(ValidationError) as schema:
            GrantCreate(capability=capability)
        assert sentence in str(schema.value)


async def test_a_row_wider_than_the_sandbox_allow_list_is_refused_like_the_bundle(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """The sandbox's allow-list is the outer limit under every grant pinned
    to it; a hand-made row past it is refused with the planner's sentence,
    and a row the list later stops covering says so in its problems."""
    agent = await _agent(session, admin_ctx)
    sandbox = await _connection(
        session,
        admin_ctx,
        connector_type="cli",
        name="Sandbox",
        config={"allowed_repositories": ["octo/a"]},
    )

    detail = await _refused(
        session,
        admin_ctx,
        agent,
        capability="cli.repository.checkout",
        scope={"connection_id": str(sandbox.id), "repository": "*"},
    )
    assert detail == (
        "'Sandbox' allows only: octo/a — '*' is outside it. Add it to the sandbox's allowed "
        "repositories under Apps, or grant only what the sandbox allows."
    )

    inside = await service.create_grant(
        session,
        admin_ctx,
        agent.id,
        **_kwargs(
            capability="cli.repository.checkout",
            scope={"connection_id": str(sandbox.id), "repository": "octo/a"},
        ),  # type: ignore[arg-type]
    )
    (row, problems, _name), *_ = await service.list_grants(
        session, admin_ctx.workspace_id, agent.id
    )
    assert row.id == inside.id
    assert problems == []

    sandbox.config_json = {"allowed_repositories": ["octo/b"]}
    await session.commit()
    (_row, problems, _name), *_ = await service.list_grants(
        session, admin_ctx.workspace_id, agent.id
    )
    assert problems == [
        "'Sandbox' allows only: octo/b — 'octo/a' is outside it. Add it to the sandbox's "
        "allowed repositories under Apps, or grant only what the sandbox allows."
    ]


async def test_a_push_to_a_branch_the_sandbox_refuses_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    sandbox = await _connection(
        session, admin_ctx, connector_type="cli", name="Sandbox", config=ANY_REPOSITORY
    )

    detail = await _refused(
        session,
        admin_ctx,
        agent,
        capability="cli.repository.push",
        scope={"connection_id": str(sandbox.id), "repository": "*", "branch": "main"},
    )

    assert detail == (
        "branch 'main' is refused on every push: the sandbox never pushes to main, master or "
        "HEAD. Use a pattern such as agent/*."
    )
