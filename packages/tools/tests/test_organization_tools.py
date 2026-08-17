"""organization.delegate_task + organization.report_result through the full
gateway pipeline (plan 7.5, 7.6, 29) against in-memory SQLite.

The delegation validator (relationship/cycle/depth) runs inside the gateway,
so every denial here is a recorded, audited tool_call row — never model
text.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, AuditEvent, Message, Task, Team, Workspace
from jhin_domain import MessageType, TaskState, new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway


class Org:
    """A seeded mini-organization: CTO -> (SWE, QA) on one team, plus a
    Blogger on another team, and a running task assigned to the CTO."""

    workspace: Workspace
    cto: Agent
    swe: Agent
    qa: Agent
    blogger: Agent
    task: Task

    def ctx(self, session: AsyncSession, agent: Agent, task: Task) -> ToolExecutionContext:
        return ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=task.id,
            run_id=new_uuid7(),
            agent_id=agent.id,
            agent_name=agent.name,
        )

    def gateway(self, session: AsyncSession, agent: Agent, task: Task | None = None) -> ToolGateway:
        return ToolGateway(self.ctx(session, agent, task or self.task), build_builtin_catalog())


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
    fixture = Org()
    fixture.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(fixture.workspace)
    await session.flush()
    ws = fixture.workspace.id

    engineering = Team(workspace_id=ws, name="Engineering")
    marketing = Team(workspace_id=ws, name="Marketing")
    session.add_all([engineering, marketing])
    await session.flush()

    fixture.cto = Agent(workspace_id=ws, team_id=engineering.id, name="CTO", slug="cto")
    session.add(fixture.cto)
    await session.flush()
    fixture.swe = Agent(
        workspace_id=ws,
        team_id=engineering.id,
        manager_agent_id=fixture.cto.id,
        name="SWE",
        slug="swe",
    )
    fixture.qa = Agent(
        workspace_id=ws,
        team_id=engineering.id,
        manager_agent_id=fixture.cto.id,
        name="QA",
        slug="qa",
    )
    fixture.blogger = Agent(workspace_id=ws, team_id=marketing.id, name="Blogger", slug="blogger")
    session.add_all([fixture.swe, fixture.qa, fixture.blogger])
    await session.flush()

    fixture.task = Task(
        workspace_id=ws,
        title="Ship the feature",
        state=TaskState.RUNNING.value,
        assigned_agent_id=fixture.cto.id,
        correlation_id=new_uuid7(),
    )
    session.add(fixture.task)
    await session.flush()
    return fixture


async def grant(
    session: AsyncSession,
    org: Org,
    agent: Agent,
    capability: str,
    scope: dict[str, Any] | None = None,
    effect: str = "allow",
) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=agent.id,
            capability=capability,
            scope_json=scope or {},
            effect=effect,
        )
    )
    await session.flush()


def delegate_args(target: Agent, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "target_agent_id": str(target.id),
        "title": "Implement the fix",
        "instructions": "Fix the failing test and open a PR.",
        "expected_output": "A green PR",
        "blocking": True,
    }
    body.update(overrides)
    return json.dumps(body)


async def delegate(
    session: AsyncSession, org: Org, actor: Agent, target: Agent, **overrides: Any
) -> GatewayOutcome:
    return await org.gateway(session, actor).request(
        "organization.delegate_task", delegate_args(target, **overrides)
    )


# --- happy path ---


async def test_delegation_creates_child_task_and_structured_message(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.cto, "organization.delegate")  # default: subordinates
    outcome = await delegate(session, org, org.cto, org.swe)
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert output["blocking"] is True
    assert output["kind"] == "delegation"
    assert output["target_agent_name"] == "SWE"

    child = await session.scalar(select(Task).where(Task.id == UUID(output["child_task_id"])))
    assert child is not None
    assert child.parent_task_id == org.task.id
    assert child.assigned_agent_id == org.swe.id
    assert child.state == TaskState.QUEUED.value
    assert child.correlation_id == org.task.correlation_id
    assert child.temporal_workflow_id == f"task-{child.id}"
    assert child.description.endswith("Expected output: A green PR")
    meta = child.metadata_json["delegation"]
    assert meta["kind"] == "delegation"
    assert meta["blocking"] is True
    assert meta["delegated_by_agent_id"] == str(org.cto.id)

    message = await session.scalar(
        select(Message).where(Message.message_type == MessageType.DELEGATION.value)
    )
    assert message is not None
    assert message.task_id == org.task.id
    assert message.sender_id == org.cto.id
    assert message.recipient_id == org.swe.id
    content = message.content_json
    assert content["summary"] == "Implement the fix"
    assert content["child_task_id"] == str(child.id)
    assert content["blocking"] is True
    assert content["artifacts"] == []

    actions = list(await session.scalars(select(AuditEvent.action)))
    assert "task.delegated" in actions


async def test_review_request_kind_and_artifacts(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.swe, "organization.delegate", {"targets": "team"})
    outcome = await delegate(
        session,
        org,
        org.swe,
        org.qa,
        kind="review_request",
        artifacts=[{"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}],
    )
    assert outcome.status == "executed", outcome.decision_reason
    message = await session.scalar(
        select(Message).where(Message.message_type == MessageType.REVIEW_REQUEST.value)
    )
    assert message is not None
    assert message.content_json["artifacts"] == [
        {"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}
    ]
    child = await session.scalar(
        select(Task).where(Task.id == UUID((outcome.sanitized_output or {})["child_task_id"]))
    )
    assert child is not None
    assert child.metadata_json["delegation"]["kind"] == "review_request"


# --- denials (all recorded through the gateway) ---


async def test_no_delegate_grant_is_denied(session: AsyncSession, org: Org) -> None:
    outcome = await delegate(session, org, org.cto, org.swe)
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"
    row_status = await session.scalar(select(Task).where(Task.parent_task_id == org.task.id))
    assert row_status is None  # no child task was created


async def test_non_subordinate_outside_scope_is_denied(session: AsyncSession, org: Org) -> None:
    # SWE has the default (subordinates-only) grant; QA is a peer, not a report.
    await grant(session, org, org.swe, "organization.delegate")
    swe_task = Task(
        workspace_id=org.workspace.id,
        title="SWE task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.swe.id,
        correlation_id=new_uuid7(),
    )
    session.add(swe_task)
    await session.flush()
    outcome = await org.gateway(session, org.swe, swe_task).request(
        "organization.delegate_task", delegate_args(org.qa)
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "delegation_target_not_permitted"
    assert "tool.call.denied" in list(await session.scalars(select(AuditEvent.action)))


async def test_team_scope_does_not_reach_other_teams(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "team"})
    outcome = await delegate(session, org, org.cto, org.blogger)
    assert outcome.status == "denied"
    assert outcome.decision_code == "delegation_target_not_permitted"


async def test_inactive_target_is_denied(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "any"})
    org.swe.status = "disabled"
    await session.flush()
    outcome = await delegate(session, org, org.cto, org.swe)
    assert outcome.status == "denied"
    assert outcome.decision_code == "target_inactive"


async def test_unknown_target_is_denied(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "any"})
    ghost = Agent(workspace_id=org.workspace.id, name="Ghost", slug="ghost")  # never flushed
    ghost.id = new_uuid7()
    outcome = await delegate(session, org, org.cto, ghost)
    assert outcome.status == "denied"
    assert outcome.decision_code == "target_not_found"


async def test_cross_workspace_target_is_denied(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "any"})
    other_ws = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other_ws)
    await session.flush()
    outsider = Agent(workspace_id=other_ws.id, name="Outsider", slug="outsider")
    session.add(outsider)
    await session.flush()
    outcome = await delegate(session, org, org.cto, outsider)
    assert outcome.status == "denied"
    assert outcome.decision_code == "target_not_found"


async def test_delegation_cycle_denied(session: AsyncSession, org: Org) -> None:
    # CTO task -> child assigned to SWE; SWE trying to delegate back up to
    # the CTO would deadlock the blocking lineage.
    await grant(session, org, org.swe, "organization.delegate", {"targets": "any"})
    child = Task(
        workspace_id=org.workspace.id,
        title="Child",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.swe.id,
        parent_task_id=org.task.id,
        correlation_id=org.task.correlation_id,
    )
    session.add(child)
    await session.flush()
    outcome = await org.gateway(session, org.swe, child).request(
        "organization.delegate_task", delegate_args(org.cto)
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "delegation_cycle"


async def test_completed_ancestor_does_not_block_redelegation(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.swe, "organization.delegate", {"targets": "any"})
    done = Task(
        workspace_id=org.workspace.id,
        title="Old CTO task",
        state=TaskState.COMPLETED.value,
        assigned_agent_id=org.cto.id,
        correlation_id=new_uuid7(),
    )
    session.add(done)
    await session.flush()
    child = Task(
        workspace_id=org.workspace.id,
        title="Child",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.swe.id,
        parent_task_id=done.id,
        correlation_id=done.correlation_id,
    )
    session.add(child)
    await session.flush()
    outcome = await org.gateway(session, org.swe, child).request(
        "organization.delegate_task", delegate_args(org.cto)
    )
    assert outcome.status == "executed", outcome.decision_reason


async def test_depth_limit_enforced_from_workspace_settings(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "any"})
    org.workspace.settings_json = {"delegation": {"max_task_depth": 2}}
    await session.flush()
    # Build a chain: root -> c1 -> c2 (depth 2); delegating from c2 => depth 3.
    parent = org.task
    for index in range(2):
        child = Task(
            workspace_id=org.workspace.id,
            title=f"chain-{index}",
            state=TaskState.RUNNING.value,
            assigned_agent_id=org.cto.id if index else org.swe.id,
            parent_task_id=parent.id,
            correlation_id=org.task.correlation_id,
        )
        session.add(child)
        await session.flush()
        parent = child
    outcome = await org.gateway(session, org.cto, parent).request(
        "organization.delegate_task", delegate_args(org.qa)
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "delegation_depth_exceeded"


async def test_scoped_deny_grant_blocks_specific_target(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate", {"targets": "any"})
    await grant(
        session,
        org,
        org.cto,
        "organization.delegate",
        {"target_agent_id": str(org.qa.id)},
        effect="deny",
    )
    denied = await delegate(session, org, org.cto, org.qa)
    assert denied.status == "denied"
    assert denied.decision_code == "explicit_deny"
    allowed = await delegate(session, org, org.cto, org.swe)
    assert allowed.status == "executed"


# --- report_result ---


async def test_report_result_on_delegated_task(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, "organization.delegate")
    outcome = await delegate(session, org, org.cto, org.swe)
    child_id = UUID((outcome.sanitized_output or {})["child_task_id"])
    child = await session.scalar(select(Task).where(Task.id == child_id))
    assert child is not None

    await grant(session, org, org.swe, "organization.report_result")
    report = await org.gateway(session, org.swe, child).request(
        "organization.report_result",
        json.dumps(
            {
                "status": "completed",
                "summary": "Implemented token rotation and opened PR #381.",
                "artifacts": [{"type": "github_pull_request", "id": "381"}],
                "risks": ["touches auth"],
                "recommended_next_action": "delegate_to_qa",
            }
        ),
    )
    assert report.status == "executed", report.decision_reason

    message = await session.scalar(
        select(Message).where(Message.message_type == MessageType.RESULT.value)
    )
    assert message is not None
    assert message.task_id == child_id
    assert message.recipient_id == org.cto.id  # routed to the delegating agent
    content = message.content_json
    assert content["status"] == "completed"
    assert content["artifacts"] == [{"type": "github_pull_request", "id": "381", "url_ref": ""}]

    refreshed = await session.scalar(select(Task).where(Task.id == child_id))
    assert refreshed is not None
    assert refreshed.metadata_json["reported_result"]["summary"].startswith("Implemented")
    assert "task.result_reported" in list(await session.scalars(select(AuditEvent.action)))


async def test_report_result_on_review_task_is_review_result(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.swe, "organization.delegate", {"targets": "team"})
    outcome = await delegate(session, org, org.swe, org.qa, kind="review_request")
    child_id = UUID((outcome.sanitized_output or {})["child_task_id"])
    child = await session.scalar(select(Task).where(Task.id == child_id))
    assert child is not None

    await grant(session, org, org.qa, "organization.report_result")
    report = await org.gateway(session, org.qa, child).request(
        "organization.report_result",
        json.dumps({"status": "fail", "summary": "Tests fail on the PR branch."}),
    )
    assert report.status == "executed"
    assert (report.sanitized_output or {})["message_type"] == "review_result"
    message = await session.scalar(
        select(Message).where(Message.message_type == MessageType.REVIEW_RESULT.value)
    )
    assert message is not None
    assert message.content_json["status"] == "fail"
    assert "task.review_reported" in list(await session.scalars(select(AuditEvent.action)))


async def test_report_result_requires_grant(session: AsyncSession, org: Org) -> None:
    outcome = await org.gateway(session, org.cto).request(
        "organization.report_result", json.dumps({"summary": "done"})
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"


async def test_denied_delegation_leaves_no_child_rows(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.swe, "organization.delegate")  # subordinates only
    swe_task = Task(
        workspace_id=org.workspace.id,
        title="SWE solo task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.swe.id,
        correlation_id=new_uuid7(),
    )
    session.add(swe_task)
    await session.flush()
    outcome = await org.gateway(session, org.swe, swe_task).request(
        "organization.delegate_task", delegate_args(org.qa)
    )
    assert outcome.status == "denied"
    children = list(await session.scalars(select(Task).where(Task.parent_task_id == swe_task.id)))
    assert children == []
    messages = list(
        await session.scalars(
            select(Message).where(Message.message_type == MessageType.DELEGATION.value)
        )
    )
    assert messages == []
