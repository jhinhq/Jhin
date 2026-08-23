"""The tool worker resolves MCP tools per workspace from durable connection
state, never at process start (docs/architecture/mcp.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_connectors import build_default_catalog
from jhin_connectors.mcp import DISCOVERY_KEY
from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, Workspace
from jhin_domain import new_uuid7
from jhin_observability import noop_metrics, noop_tracer
from jhin_tool_worker.activities import ToolActivities
from jhin_workflows.agent_task.shared import ResolveAdvertisedToolsInput

_DISCOVERY = [
    {
        "name": "echo",
        "slug": "echo",
        "description": "Return the text.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "annotations": {"read_only_hint": True},
        "derived_risk": "read",
    },
    {
        "name": "delete_everything",
        "slug": "delete_everything",
        "description": "Delete it all.",
        "input_schema": {"type": "object"},
        "annotations": {"destructive_hint": True},
        "derived_risk": "destructive",
    },
]


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    runtime: object = field(
        default_factory=lambda: SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
    )
    crypto: None = None
    test_barrier: None = None


@pytest.fixture
async def world() -> AsyncIterator[tuple[ToolActivities, Workspace, Agent, Connection]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        workspace = Workspace(name="MCP", slug=f"mcp-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Scout", slug="scout")
        session.add(agent)
        connection = Connection(
            workspace_id=workspace.id,
            connector_type="mcp",
            name="Fake",
            auth_type="bearer",
            config_json={
                "server_url": "https://mcp.example.com/mcp",
                "server_slug": "fake",
                DISCOVERY_KEY: _DISCOVERY,
            },
        )
        session.add(connection)
        await session.flush()
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability="mcp.fake.*",
                scope_json={"connection_id": str(connection.id)},
                effect="allow",
            )
        )
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability="system.echo",
                scope_json={},
                effect="allow",
            )
        )
        await session.commit()
    catalog = build_default_catalog()
    assert "mcp.fake.echo" not in catalog.registry.names()
    yield ToolActivities(_Resources(sessions), catalog), workspace, agent, connection  # type: ignore[arg-type]
    await engine.dispose()


async def test_advertised_tools_include_workspace_mcp_tools_with_connection_hints(
    world: tuple[ToolActivities, Workspace, Agent, Connection],
) -> None:
    activities, workspace, agent, connection = world
    advertised = await activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(workspace_id=str(workspace.id), agent_id=str(agent.id))
    )
    by_name = {tool.name: tool for tool in advertised}
    assert {"system.echo", "mcp.fake.echo", "mcp.fake.delete_everything"} <= set(by_name)
    echo = by_name["mcp.fake.echo"]
    assert str(connection.id) in echo.description
    assert "[MCP: fake]" in echo.description
    schema = echo.parameters
    assert schema["properties"]["arguments"]["properties"]["text"] == {"type": "string"}
    assert json.dumps(schema)  # JSON-serializable for the provider payload

    # Disabled connections drop out of the advertised set on the next step.
    async with activities._resources.session_factory() as session:
        row = await session.get(Connection, connection.id)
        assert row is not None
        row.status = "disabled"
        await session.commit()
    advertised = await activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(workspace_id=str(workspace.id), agent_id=str(agent.id))
    )
    assert [tool.name for tool in advertised] == ["system.echo"]
