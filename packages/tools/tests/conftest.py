"""Shared fixtures: an in-memory SQLite database with the full schema, plus a
workspace/agent/run identity for exercising the gateway without Postgres."""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, Workspace
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import ToolGateway


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def context(session: AsyncSession) -> ToolExecutionContext:
    workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    agent = Agent(workspace_id=workspace.id, name="Scout", slug="scout")
    session.add(agent)
    await session.flush()
    return ToolExecutionContext(
        session=session,
        workspace_id=workspace.id,
        task_id=new_uuid7(),
        run_id=new_uuid7(),
        agent_id=agent.id,
        agent_name=agent.name,
    )


@pytest.fixture
def gateway(context: ToolExecutionContext) -> ToolGateway:
    return ToolGateway(context, build_builtin_catalog())


@pytest.fixture
def agent_id(context: ToolExecutionContext) -> UUID:
    return context.agent_id
