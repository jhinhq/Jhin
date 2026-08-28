"""Who an agent may put a question in front of.

Advertisement (``PERSON_FACING_TOOLS``) is prompt economy; this is the lock.
A trigger-fired run, a delegated child, and an accepted work request all have
nobody watching, so the ask is denied there and the agent decides for itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Conversation,
    Task,
    Team,
    UserQuestion,
    Workspace,
)
from jhin_domain import TaskState, new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway

SCOPE_OPTIONS = [
    {"value": "team", "label": "Only the Engineering team"},
    {"value": "workspace", "label": "Company wide"},
]
OPEN_OPTIONS = [
    {"value": "staging", "label": "Deploy to staging"},
    {"value": "production", "label": "Deploy to production"},
]


class Org:
    workspace: Workspace
    team: Team
    agent: Agent
    teamless: Agent
    conversation: Conversation


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def org(session: AsyncSession) -> Org:
    f = Org()
    f.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(f.workspace)
    await session.flush()
    ws = f.workspace.id
    f.team = Team(workspace_id=ws, name="Engineering")
    session.add(f.team)
    await session.flush()
    f.agent = Agent(workspace_id=ws, team_id=f.team.id, name="Ada", slug="ada")
    f.teamless = Agent(workspace_id=ws, name="Solo", slug="solo")
    session.add_all([f.agent, f.teamless])
    await session.flush()
    f.conversation = Conversation(
        workspace_id=ws,
        title="Deploys",
        primary_agent_id=f.agent.id,
        last_activity_at=datetime.now(UTC),
    )
    session.add(f.conversation)
    await session.flush()
    for agent in (f.agent, f.teamless):
        session.add(
            AgentCapabilityGrant(
                workspace_id=ws,
                agent_id=agent.id,
                capability="organization.ask_person",
                scope_json={},
                effect="allow",
            )
        )
    await session.flush()
    return f


async def make_task(session: AsyncSession, org: Org, **columns: Any) -> Task:
    task = Task(
        workspace_id=org.workspace.id,
        title="Work",
        state=TaskState.RUNNING.value,
        correlation_id=new_uuid7(),
        **{"assigned_agent_id": org.agent.id, **columns},
    )
    session.add(task)
    await session.flush()
    return task


async def ask(
    session: AsyncSession,
    org: Org,
    *,
    task_id: UUID,
    agent: Agent | None = None,
    options: list[dict[str, Any]] | None = None,
    kind: str = "memory_scope",
) -> GatewayOutcome:
    subject = agent or org.agent
    ctx = ToolExecutionContext(
        session=session,
        workspace_id=org.workspace.id,
        task_id=task_id,
        run_id=new_uuid7(),
        agent_id=subject.id,
        agent_name=subject.name,
    )
    return await ToolGateway(ctx, build_builtin_catalog()).request(
        "organization.ask_person",
        json.dumps(
            {
                "question": "Is this only for Engineering, or company wide?",
                "options": options if options is not None else SCOPE_OPTIONS,
                "kind": kind,
            }
        ),
    )


async def question_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(UserQuestion)) or 0)


async def test_a_chat_turn_may_ask(session: AsyncSession, org: Org) -> None:
    task = await make_task(
        session, org, conversation_id=org.conversation.id, metadata_json={"origin": "conversation"}
    )
    outcome = await ask(session, org, task_id=task.id)
    assert outcome.status == "executed", outcome.decision_reason


async def test_a_delegated_child_asks_nobody(session: AsyncSession, org: Org) -> None:
    """The person is watching the parent thread; a box on a child's task page
    would land where nobody is looking."""
    parent = await make_task(session, org, conversation_id=org.conversation.id)
    child = await make_task(
        session,
        org,
        parent_task_id=parent.id,
        conversation_id=org.conversation.id,
        metadata_json={"delegation": {"kind": "delegation"}},
    )
    outcome = await ask(session, org, task_id=child.id)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_person_watching"
    assert await question_count(session) == 0


async def test_an_accepted_work_request_asks_nobody(session: AsyncSession, org: Org) -> None:
    """It is linked to the requester's chat, so conversation_id must not
    decide alone."""
    task = await make_task(
        session,
        org,
        conversation_id=org.conversation.id,
        metadata_json={"origin": "work_request", "work_request": {"id": "w"}},
    )
    outcome = await ask(session, org, task_id=task.id)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_person_watching"


async def test_a_triggered_run_asks_nobody(session: AsyncSession, org: Org) -> None:
    task = await make_task(session, org, metadata_json={"origin": "trigger"})
    outcome = await ask(session, org, task_id=task.id)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_person_watching"


async def test_a_standalone_task_asks_nobody(session: AsyncSession, org: Org) -> None:
    task = await make_task(session, org)
    outcome = await ask(session, org, task_id=task.id)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_person_watching"


async def test_an_unknown_task_asks_nobody(session: AsyncSession, org: Org) -> None:
    """Withholding a report is prompt economy; withholding an interruption is
    a promise to the person, so a task we cannot read denies."""
    outcome = await ask(session, org, task_id=new_uuid7())
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_person_watching"


async def test_a_teamless_agent_cannot_offer_a_team(session: AsyncSession, org: Org) -> None:
    """Refusing before the row exists is cheaper than refusing after the
    person has already answered into a memory nothing could store."""
    task = await make_task(
        session,
        org,
        assigned_agent_id=org.teamless.id,
        conversation_id=org.conversation.id,
        metadata_json={"origin": "conversation"},
    )
    outcome = await ask(session, org, task_id=task.id, agent=org.teamless)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_team_for_scope"
    assert await question_count(session) == 0


async def test_a_teamless_agent_may_still_ask_about_the_company(
    session: AsyncSession, org: Org
) -> None:
    task = await make_task(
        session,
        org,
        assigned_agent_id=org.teamless.id,
        conversation_id=org.conversation.id,
        metadata_json={"origin": "conversation"},
    )
    outcome = await ask(
        session,
        org,
        task_id=task.id,
        agent=org.teamless,
        options=[
            {"value": "agent", "label": "Just between us"},
            {"value": "workspace", "label": "Company wide"},
        ],
    )
    assert outcome.status == "executed", outcome.decision_reason


async def test_an_open_question_does_not_need_a_team(session: AsyncSession, org: Org) -> None:
    task = await make_task(
        session,
        org,
        assigned_agent_id=org.teamless.id,
        conversation_id=org.conversation.id,
        metadata_json={"origin": "conversation"},
    )
    outcome = await ask(
        session, org, task_id=task.id, agent=org.teamless, options=OPEN_OPTIONS, kind="open"
    )
    assert outcome.status == "executed", outcome.decision_reason


async def test_without_a_grant_nothing_reaches_the_validator(
    session: AsyncSession, org: Org
) -> None:
    """Deny-by-default is unchanged: the default grant set is the only reason
    an ordinary agent has this at all."""
    stranger = Agent(workspace_id=org.workspace.id, name="Stranger", slug="stranger")
    session.add(stranger)
    await session.flush()
    task = await make_task(
        session,
        org,
        assigned_agent_id=stranger.id,
        conversation_id=org.conversation.id,
        metadata_json={"origin": "conversation"},
    )
    outcome = await ask(session, org, task_id=task.id, agent=stranger)
    assert outcome.status == "denied"
    assert outcome.decision_code != "no_person_watching"
