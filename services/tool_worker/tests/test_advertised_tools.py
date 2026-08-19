"""Definition-only catalog and live tool advertisement contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Workspace
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tools import TOOL_AFTER_CLAIM, ToolCatalog, ToolExecutionContext
from jhin_workflows.agent_task.shared import ResolveAdvertisedToolsInput


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _Output(BaseModel):
    value: str


async def _executor(_ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return _Output(value=str(payload.model_dump()["value"]))


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Definition for {name}",
        risk=RiskLevel.READ,
        input_model=_Input,
        output_model=_Output,
        required_capability=name,
    )


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    crypto: None = None
    test_barrier: None = None


@pytest.fixture
async def advertised_world() -> AsyncIterator[tuple[ToolActivities, Workspace, Agent]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    catalog = ToolCatalog()
    for name in ("system.echo", "linear.issue.get", "system.time"):
        catalog.register(_definition(name), _executor)

    async with sessions() as session:
        workspace = Workspace(name="Advertise", slug=f"advertise-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Catalog agent", slug="catalog-agent")
        session.add(agent)
        await session.flush()
        for capability in ("system.echo", "linear.issue.get"):
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json={},
                    effect="allow",
                )
            )
        await session.commit()

    yield ToolActivities(_Resources(sessions), catalog), workspace, agent  # type: ignore[arg-type]
    await engine.dispose()


async def test_advertisement_uses_live_grants_and_preserves_catalog_order(
    advertised_world: tuple[ToolActivities, Workspace, Agent],
) -> None:
    activities, workspace, agent = advertised_world

    advertised = await activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(workspace_id=str(workspace.id), agent_id=str(agent.id))
    )

    assert [tool.name for tool in advertised] == ["system.echo", "linear.issue.get"]
    assert advertised[0].parameters["type"] == "object"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("JHIN_TEST_CRASH_BARRIER_DIR", "/run/jhin/test-barriers"),
        ("JHIN_TEST_CRASH_BARRIER_NAME", TOOL_AFTER_CLAIM),
        ("JHIN_TEST_CRASH_BARRIER_MATCH", "018f4d52-8b93-7d41-8ac7-7f190f091111"),
    ],
)
def test_tool_worker_rejects_crash_barrier_in_production(
    setting: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValidationError, match="test crash barriers are forbidden"):
        ToolWorkerSettings()


def test_tool_worker_settings_ignore_unrelated_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = f"unrelated-{new_uuid7()}"
    monkeypatch.setenv("JHIN_UNRELATED_TEST_SETTING", marker)

    settings = ToolWorkerSettings()

    assert settings.app_env == "development"
    assert marker not in repr(settings)
