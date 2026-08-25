"""Coordination tools through the full gateway pipeline against SQLite:
directory search, peer work requests (idempotent accept, decline creates no
task, structural target check), review request/submit, the pre-action review
gate, and the manager rollup."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRelationship,
    AgentRun,
    AgentTeamMembership,
    AuditEvent,
    Message,
    ReviewPolicy,
    Task,
    Team,
    WorkRequest,
    WorkReview,
    Workspace,
)
from jhin_domain import MessageType, RunStatus, TaskState, WorkRequestStatus, new_uuid7
from jhin_policy import Grant, GrantEffect, collaboration_grant_specs
from jhin_tools.builtin import (
    ToolExecutionContext,
    allowed_tool_definitions,
    build_builtin_catalog,
)
from jhin_tools.directory import DirectoryEntry, build_roster, render_roster, search_directory
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.reviews import (
    ToolCallIntent,
    check_review_gate,
    decide_review,
    open_periodic_review,
    periodic_trigger_key,
)
from jhin_tools.rollups import build_manager_rollup, render_manager_rollup
from jhin_tools.work_requests import finalize_work_request


class Org:
    workspace: Workspace
    engineering: Team
    marketing: Team
    cto: Agent
    swe: Agent
    qa: Agent
    blogger: Agent
    hidden: Agent
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
    fixture.engineering = Team(workspace_id=ws, name="Engineering")
    fixture.marketing = Team(workspace_id=ws, name="Marketing")
    session.add_all([fixture.engineering, fixture.marketing])
    await session.flush()
    fixture.cto = Agent(
        workspace_id=ws,
        team_id=fixture.engineering.id,
        name="CTO",
        slug="cto",
        role_title="Chief Technology Officer",
        expertise_json=["architecture"],
    )
    session.add(fixture.cto)
    await session.flush()
    fixture.swe = Agent(
        workspace_id=ws,
        team_id=fixture.engineering.id,
        manager_agent_id=fixture.cto.id,
        name="SWE",
        slug="swe",
        role_title="Software Engineer",
        public_purpose="Ships backend features",
        expertise_json=["python", "postgres"],
    )
    fixture.qa = Agent(
        workspace_id=ws,
        team_id=fixture.engineering.id,
        manager_agent_id=fixture.cto.id,
        name="QA",
        slug="qa",
        role_title="QA Engineer",
        expertise_json=["testing"],
    )
    fixture.blogger = Agent(
        workspace_id=ws,
        team_id=fixture.marketing.id,
        name="Blogger",
        slug="blogger",
        role_title="Content Writer",
        expertise_json=["writing"],
    )
    fixture.hidden = Agent(workspace_id=ws, name="Shadow", slug="shadow", discoverability="hidden")
    session.add_all([fixture.swe, fixture.qa, fixture.blogger, fixture.hidden])
    await session.flush()
    session.add_all(
        [
            AgentTeamMembership(
                workspace_id=ws,
                agent_id=fixture.swe.id,
                team_id=fixture.engineering.id,
                is_primary=True,
            ),
            AgentTeamMembership(
                workspace_id=ws, agent_id=fixture.swe.id, team_id=fixture.marketing.id
            ),
        ]
    )
    first, second = sorted([fixture.swe.id, fixture.blogger.id])
    session.add(
        AgentRelationship(
            workspace_id=ws,
            source_agent_id=first,
            target_agent_id=second,
            kind="close_collaborator",
        )
    )
    fixture.task = Task(
        workspace_id=ws,
        title="Ship the feature",
        state=TaskState.RUNNING.value,
        assigned_agent_id=fixture.swe.id,
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


def request_args(target: Agent, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "target_agent_id": str(target.id),
        "title": "Write release notes",
        "description": "Summarize the changes for the 2.0 release.",
        "expected_output": "A markdown document",
        "idempotency_key": "rel-notes-1",
    }
    body.update(overrides)
    return json.dumps(body)


async def request_work(
    session: AsyncSession, org: Org, actor: Agent, target: Agent, **overrides: Any
) -> GatewayOutcome:
    return await org.gateway(session, actor).request(
        "organization.request_work", request_args(target, **overrides)
    )


async def respond(
    session: AsyncSession, org: Org, actor: Agent, request_id: str, decision: str, task: Task
) -> GatewayOutcome:
    return await org.gateway(session, actor, task).request(
        "organization.respond_work_request",
        json.dumps({"work_request_id": request_id, "decision": decision, "response": "ok"}),
    )


# --- directory ---


async def test_directory_search_public_fields_only(session: AsyncSession, org: Org) -> None:
    entries, has_more = await search_directory(session, org.workspace.id, q="engineer")
    names = [e.name for e in entries]
    assert names == ["QA", "SWE"]  # role match, sorted by name
    assert not has_more
    assert set(DirectoryEntry.model_fields) == {
        "id",
        "name",
        "slug",
        "role_title",
        "public_purpose",
        "expertise",
        "availability",
        "primary_team_id",
        "primary_team_name",
        "manager_agent_id",
    }
    swe = next(e for e in entries if e.name == "SWE")
    assert swe.primary_team_name == "Engineering"
    assert swe.manager_agent_id == str(org.cto.id)

    by_tag, _ = await search_directory(session, org.workspace.id, expertise="python")
    assert [e.name for e in by_tag] == ["SWE"]
    by_team, _ = await search_directory(session, org.workspace.id, team_id=org.marketing.id)
    assert [e.name for e in by_team] == ["Blogger", "SWE"]  # SWE is a secondary member
    everyone, _ = await search_directory(session, org.workspace.id)
    assert "Shadow" not in [e.name for e in everyone]


async def test_directory_tool_requires_grant_and_is_scoped(session: AsyncSession, org: Org) -> None:
    denied = await org.gateway(session, org.swe).request(
        "organization.directory.search", json.dumps({"query": "qa"})
    )
    assert denied.status == "denied" and denied.decision_code == "no_grant"

    other_ws = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other_ws)
    await session.flush()
    session.add(Agent(workspace_id=other_ws.id, name="QA Outsider", slug="qa-out"))
    await session.flush()

    await grant(session, org, org.swe, "organization.directory.read")
    outcome = await org.gateway(session, org.swe).request(
        "organization.directory.search", json.dumps({"query": "qa", "limit": 5})
    )
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert [e["name"] for e in output["entries"]] == ["QA"]
    assert "system_prompt" not in json.dumps(output)


async def test_roster_is_bounded_and_rendered(session: AsyncSession, org: Org) -> None:
    roster = await build_roster(session, org.swe)
    assert roster.manager is not None and roster.manager.name == "CTO"
    assert [e.name for e in roster.primary_team_members] == ["QA"]
    assert [e.name for e in roster.collaborators] == ["Blogger"]
    assert roster.secondary_team_members == []  # Blogger already listed
    assert len(roster.entries()) <= 40
    text = render_roster(roster)
    assert "routing context only" in text
    assert "Your manager:" in text and "CTO" in text
    cto_roster = await build_roster(session, org.cto)
    assert {e.name for e in cto_roster.reports} == {"QA", "SWE"}


# --- work requests ---


async def test_request_work_denied_without_grant_and_cross_team_default(
    session: AsyncSession, org: Org
) -> None:
    denied = await request_work(session, org, org.swe, org.blogger)
    assert denied.status == "denied" and denied.decision_code == "no_grant"
    await grant(session, org, org.swe, "organization.work.request")  # default: team
    cross = await request_work(session, org, org.swe, org.blogger)
    assert cross.status == "denied"
    assert cross.decision_code == "request_target_not_permitted"
    assert await session.scalar(select(WorkRequest)) is None
    self_request = await request_work(session, org, org.swe, org.swe)
    assert self_request.decision_code == "self_request"


async def test_collaboration_baseline_advertises_and_permits_cross_team_ask(
    session: AsyncSession, org: Org
) -> None:
    """The safe-by-default collaboration baseline makes 'ask a colleague'
    work out of the box: the three collaboration tools are advertised, an
    ordinary agent can ask across teams, and delegation is NOT advertised."""
    baseline = [
        Grant(capability=capability, scope=scope, effect=GrantEffect.ALLOW)
        for capability, scope in collaboration_grant_specs()
    ]
    advertised = {
        definition.name
        for definition in allowed_tool_definitions(build_builtin_catalog(), baseline)
    }
    assert {
        "organization.directory.search",
        "organization.request_work",
        "organization.respond_work_request",
    } <= advertised
    assert "organization.delegate_task" not in advertised

    # Persist the baseline on the SWE and ask the Blogger (a different team):
    # targets=any in the baseline permits the cross-team ask end to end.
    for capability, scope in collaboration_grant_specs():
        await grant(session, org, org.swe, capability, scope)
    outcome = await request_work(session, org, org.swe, org.blogger)
    assert outcome.status == "executed", outcome.decision_reason
    assert (outcome.sanitized_output or {})["created"] is True
    request = await session.scalar(select(WorkRequest))
    assert request is not None
    assert request.target_agent_id == org.blogger.id


async def test_request_accept_is_idempotent_and_creates_one_task(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    first = await request_work(session, org, org.swe, org.blogger)
    assert first.status == "executed", first.decision_reason
    output = first.sanitized_output or {}
    assert output["created"] is True and output["status"] == "pending"
    again = await request_work(session, org, org.swe, org.blogger)
    assert (again.sanitized_output or {})["created"] is False
    requests = list(await session.scalars(select(WorkRequest)))
    assert len(requests) == 1
    request = requests[0]
    assert request.depth == 1
    assert request.root_task_id == org.task.id
    question = await session.scalar(
        select(Message).where(Message.message_type == MessageType.QUESTION.value)
    )
    assert question is not None
    assert question.content_json["target_agent_name"] == "Blogger"
    assert question.content_json["from_agent_name"] == "SWE"
    assert question.content_json["work_request_id"] == str(request.id)

    # Only the target may respond.
    blogger_task = Task(
        workspace_id=org.workspace.id,
        title="Inbox",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.blogger.id,
        correlation_id=new_uuid7(),
    )
    session.add(blogger_task)
    await session.flush()
    await grant(session, org, org.qa, "organization.work.respond")
    await grant(session, org, org.blogger, "organization.work.respond")
    wrong = await respond(session, org, org.qa, str(request.id), "accept", org.task)
    assert wrong.status == "denied" and wrong.decision_code == "not_request_target"

    accepted = await respond(session, org, org.blogger, str(request.id), "accept", blogger_task)
    assert accepted.status == "executed", accepted.decision_reason
    created_task_id = (accepted.sanitized_output or {})["created_task_id"]
    retry = await respond(session, org, org.blogger, str(request.id), "accept", blogger_task)
    assert (retry.sanitized_output or {})["created_task_id"] == created_task_id
    tasks = list(
        await session.scalars(
            select(Task).where(Task.metadata_json["origin"].as_string() == "work_request")
        )
    )
    assert len(tasks) == 1
    created = tasks[0]
    assert created.parent_task_id is None
    assert created.assigned_agent_id == org.blogger.id
    assert created.temporal_workflow_id == f"task-{created.id}"
    assert created.metadata_json["work_request"]["id"] == str(request.id)
    await session.refresh(request)
    assert request.status == WorkRequestStatus.ACCEPTED.value
    assert request.created_task_id == created.id

    # Ping-pong guard: Blogger cannot open a request back to SWE on this root.
    await grant(session, org, org.blogger, "organization.work.request", {"targets": "any"})
    back = await org.gateway(session, org.blogger, created).request(
        "organization.request_work", request_args(org.swe, idempotency_key="back")
    )
    assert back.status == "denied" and back.decision_code == "request_ping_pong"

    # Depth: a request opened from the created task sits at depth 2.
    forward = await org.gateway(session, org.blogger, created).request(
        "organization.request_work", request_args(org.qa, idempotency_key="forward")
    )
    assert forward.status == "executed", forward.decision_reason
    second = await session.scalar(
        select(WorkRequest).where(WorkRequest.idempotency_key == "forward")
    )
    assert second is not None and second.depth == 2 and second.root_task_id == org.task.id

    # Finalization posts a standardized result to the requester's task.
    created.metadata_json = {
        **created.metadata_json,
        "reported_result": {"summary": "Notes written.", "status": "completed", "artifacts": []},
    }
    await session.flush()
    done = await finalize_work_request(
        session, workspace_id=org.workspace.id, request_id=request.id, run_status="completed"
    )
    assert done is not None and done.status == WorkRequestStatus.COMPLETED.value
    result = await session.scalar(
        select(Message).where(Message.message_type == MessageType.RESULT.value)
    )
    assert result is not None
    assert result.task_id == org.task.id
    assert result.content_json["summary"] == "Notes written."
    assert result.content_json["from_agent_name"] == "Blogger"
    again_done = await finalize_work_request(
        session, workspace_id=org.workspace.id, request_id=request.id, run_status="failed"
    )
    assert again_done is not None and again_done.status == WorkRequestStatus.COMPLETED.value


async def test_decline_creates_no_task(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.swe, "organization.work.request", {"targets": "team"})
    outcome = await request_work(session, org, org.swe, org.qa)
    assert outcome.status == "executed", outcome.decision_reason
    request_id = (outcome.sanitized_output or {})["work_request_id"]
    qa_task = Task(
        workspace_id=org.workspace.id,
        title="QA inbox",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.qa.id,
        correlation_id=new_uuid7(),
    )
    session.add(qa_task)
    await session.flush()
    await grant(session, org, org.qa, "organization.work.respond")
    declined = await respond(session, org, org.qa, request_id, "decline", qa_task)
    assert declined.status == "executed"
    assert (declined.sanitized_output or {})["created_task_id"] is None
    request = await session.get(WorkRequest, UUID(request_id))
    assert request is not None and request.status == WorkRequestStatus.DECLINED.value
    assert (
        await session.scalar(
            select(Task).where(Task.metadata_json["origin"].as_string() == "work_request")
        )
        is None
    )
    late_accept = await respond(session, org, org.qa, request_id, "accept", qa_task)
    assert late_accept.status == "executed"
    assert "work_request_not_open" in (late_accept.sanitized_output or {})["detail"]
    actions = list(await session.scalars(select(AuditEvent.action)))
    assert "work_request.created" in actions and "work_request.declined" in actions


async def test_target_capacity_and_unavailable_target(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    org.blogger.availability = "unavailable"
    await session.flush()
    outcome = await request_work(session, org, org.swe, org.blogger)
    assert outcome.decision_code == "target_unavailable"
    org.blogger.availability = "available"
    org.workspace.settings_json = {"coordination": {"max_pending_requests_per_agent": 1}}
    await session.flush()
    assert (await request_work(session, org, org.swe, org.blogger)).status == "executed"
    capped = await request_work(session, org, org.swe, org.qa, idempotency_key="second")
    assert capped.decision_code == "requester_pending_limit"


# --- reviews ---


async def run_for(session: AsyncSession, org: Org, agent: Agent, task: Task) -> AgentRun:
    run = AgentRun(
        workspace_id=org.workspace.id,
        agent_id=agent.id,
        task_id=task.id,
        status=RunStatus.RUNNING.value,
    )
    session.add(run)
    await session.flush()
    return run


async def test_review_gate_routine_work_proceeds_and_destructive_waits(
    session: AsyncSession, org: Org
) -> None:
    run = await run_for(session, org, org.swe, org.task)
    intent = ToolCallIntent(tool_name="system.demo.destructive", risk="destructive")
    assert (await check_review_gate(session, run, intent)).status == "proceed"

    session.add(
        ReviewPolicy(
            workspace_id=org.workspace.id,
            name="destructive needs manager",
            mode="pre_action",
            conditions_json=[{"kind": "destructive_action"}],
            reviewer_selector_json={"kind": "reporting_manager"},
            fail_closed=True,
        )
    )
    await session.flush()
    routine = await check_review_gate(
        session, run, ToolCallIntent(tool_name="system.note.append", risk="write")
    )
    assert routine.status == "proceed" and routine.code == "no_review"

    call_id = new_uuid7()
    gate = await check_review_gate(
        session, run, intent.model_copy(update={"tool_call_id": call_id})
    )
    assert gate.status == "wait_review"
    assert gate.reviewer_type == "agent" and gate.reviewer_agent_id == org.cto.id
    review = await session.get(WorkReview, gate.review_id)
    assert review is not None and review.status == "pending"
    # Same exception → same review (no duplicates).
    again = await check_review_gate(
        session, run, intent.model_copy(update={"tool_call_id": call_id})
    )
    assert again.review_id == gate.review_id
    assert len(list(await session.scalars(select(WorkReview)))) == 1

    # The assigned AI reviewer submits; a non-reviewer is denied structurally.
    await grant(session, org, org.qa, "organization.review.request")
    await grant(session, org, org.cto, "organization.review.request")
    submit_args = json.dumps(
        {"review_id": str(review.id), "verdict": "approve", "feedback": "Looks safe."}
    )
    wrong = await org.gateway(session, org.qa).request("organization.review.submit", submit_args)
    assert wrong.status == "denied" and wrong.decision_code == "not_assigned_reviewer"
    ok = await org.gateway(session, org.cto).request("organization.review.submit", submit_args)
    assert ok.status == "executed", ok.decision_reason
    after = await check_review_gate(
        session, run, intent.model_copy(update={"tool_call_id": call_id})
    )
    assert after.status == "proceed" and after.code == "review_approved"
    repeat = await org.gateway(session, org.cto).request("organization.review.submit", submit_args)
    assert repeat.decision_code == "review_already_decided"


async def test_review_gate_fails_closed_without_mandatory_reviewer(
    session: AsyncSession, org: Org
) -> None:
    # The CTO has no manager; a mandatory policy with no human fallback
    # still parks the call on a human-assigned review (fail closed).
    cto_task = Task(
        workspace_id=org.workspace.id,
        title="CTO task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.cto.id,
        correlation_id=new_uuid7(),
    )
    session.add(cto_task)
    await session.flush()
    run = await run_for(session, org, org.cto, cto_task)
    session.add(
        ReviewPolicy(
            workspace_id=org.workspace.id,
            name="mandatory",
            mode="pre_action",
            conditions_json=[{"kind": "elevated_action"}],
            reviewer_selector_json={"kind": "reporting_manager", "fallback_to_human": False},
            fail_closed=True,
        )
    )
    session.add(
        ReviewPolicy(
            workspace_id=org.workspace.id,
            name="optional",
            mode="pre_action",
            conditions_json=[{"kind": "elevated_action"}],
            reviewer_selector_json={"kind": "reporting_manager", "fallback_to_human": False},
            fail_closed=False,
            priority=1,
        )
    )
    await session.flush()
    gate = await check_review_gate(
        session, run, ToolCallIntent(tool_name="system.demo.elevated", risk="elevated")
    )
    assert gate.status == "wait_review" and gate.reviewer_type == "human"
    reviews = list(await session.scalars(select(WorkReview)))
    assert {r.status for r in reviews} == {"pending", "skipped"}
    mandatory = next(r for r in reviews if r.status == "pending")
    assert mandatory.evidence_json["fail_closed"] is True


async def test_explicit_review_request_falls_back_to_manager(
    session: AsyncSession, org: Org
) -> None:
    await run_for(session, org, org.swe, org.task)
    await grant(session, org, org.swe, "organization.review.request")
    outcome = await org.gateway(session, org.swe).request(
        "organization.review.request",
        json.dumps({"summary": "PR ready", "risks": ["touches auth"]}),
    )
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert output["reviewer_agent_name"] == "CTO" and output["status"] == "pending"
    review = await session.get(WorkReview, UUID(output["review_id"]))
    assert review is not None and review.evidence_json["summary"] == "PR ready"


async def test_submit_review_output_names_the_source_workflow(
    session: AsyncSession, org: Org
) -> None:
    """The assigned AI reviewer's decision carries the source task's durable
    workflow id so the agent worker can lift it into a review_decision signal."""
    org.task.temporal_workflow_id = f"task-{org.task.id}"
    run = await run_for(session, org, org.swe, org.task)
    session.add(
        ReviewPolicy(
            workspace_id=org.workspace.id,
            name="destructive needs manager",
            mode="pre_action",
            conditions_json=[{"kind": "destructive_action"}],
            reviewer_selector_json={"kind": "reporting_manager"},
            fail_closed=True,
        )
    )
    await session.flush()
    call_id = new_uuid7()
    gate = await check_review_gate(
        session,
        run,
        ToolCallIntent(
            tool_name="system.demo.destructive", risk="destructive", tool_call_id=call_id
        ),
    )
    assert gate.status == "wait_review"
    await grant(session, org, org.cto, "organization.review.request")
    ok = await org.gateway(session, org.cto).request(
        "organization.review.submit",
        json.dumps({"review_id": str(gate.review_id), "verdict": "approve", "feedback": "ok"}),
    )
    assert ok.status == "executed", ok.decision_reason
    assert ok.sanitized_output == {
        "review_id": str(gate.review_id),
        "status": "approved",
        "verdict": "approve",
        "task_id": str(org.task.id),
        "source_workflow_id": f"task-{org.task.id}",
    }


async def test_periodic_review_opens_one_review_per_window_with_rollup_evidence(
    session: AsyncSession, org: Org
) -> None:
    policy = ReviewPolicy(
        workspace_id=org.workspace.id,
        name="weekly engineering review",
        scope_kind="agent",
        scope_id=org.swe.id,
        mode="periodic",
        conditions_json=[{"kind": "always"}],
        reviewer_selector_json={"kind": "reporting_manager"},
        period_seconds=7 * 24 * 3600,
    )
    session.add(policy)
    await run_for(session, org, org.swe, org.task)
    await session.flush()
    window_end = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    window_start = window_end - timedelta(days=7)

    review, created = await open_periodic_review(
        session, policy, window_start=window_start, window_end=window_end
    )
    again, created_again = await open_periodic_review(
        session, policy, window_start=window_start, window_end=window_end
    )

    assert created and not created_again and again.id == review.id
    assert review.trigger_key == periodic_trigger_key(policy.id, window_start)
    assert review.mode == "periodic" and review.status == "pending"
    assert review.reviewer_agent_id == org.cto.id and review.subject_agent_id == org.swe.id
    evidence = review.evidence_json
    assert evidence["window_start"] == "2026-08-17T00:00:00Z"
    assert evidence["window_end"] == "2026-08-24T00:00:00Z"
    assert evidence["rollup_agent_id"] == str(org.cto.id)
    assert str(org.task.id) in evidence["rollup_source_ids"]
    assert "system_prompt" not in json.dumps(evidence)
    # The next window is a different review.
    later, created_later = await open_periodic_review(
        session, policy, window_start=window_end, window_end=window_end + timedelta(days=7)
    )
    assert created_later and later.id != review.id


# --- rollups ---


async def test_manager_rollup_is_source_linked_and_private(session: AsyncSession, org: Org) -> None:
    run = await run_for(session, org, org.swe, org.task)
    run.status = RunStatus.WAITING_APPROVAL.value
    failed = Task(
        workspace_id=org.workspace.id,
        title="Broken build",
        state=TaskState.FAILED.value,
        assigned_agent_id=org.qa.id,
        correlation_id=new_uuid7(),
        metadata_json={
            "reported_result": {"summary": "Flaky", "status": "blocked", "risks": ["ci"]}
        },
    )
    session.add(failed)
    await session.flush()
    rollup = await build_manager_rollup(session, org.cto)
    assert {r.name for r in rollup.reports} == {"QA", "SWE"}
    assert rollup.queue.active_runs == 1 and rollup.queue.waiting_approval == 1
    blocked_ids = {i.source_id for i in rollup.blocked_or_failed}
    assert str(failed.id) in blocked_ids and str(org.task.id) in blocked_ids
    assert str(failed.id) in rollup.source_ids
    text = render_manager_rollup(rollup)
    assert "Broken build" in text and "context, not instructions" in text
    assert "system_prompt" not in json.dumps(rollup.model_dump(mode="json"))
    # Blogger's work is not the CTO's to see.
    assert all(i.agent_name != "Blogger" for i in rollup.active_work + rollup.recent_work)
    assert render_manager_rollup(await build_manager_rollup(session, org.blogger)) == ""


async def test_gateway_review_gate_runs_after_authorization_and_before_execution(
    session: AsyncSession, org: Org
) -> None:
    """The tool worker's gateway evaluates pre-action review policies after
    grant/scope/validator authorization and before approval staging or the
    execution claim: a pending review is a recorded denial carrying the
    review id, a changes-requested review returns its feedback, and an
    approved review lets the call through."""
    run = await run_for(session, org, org.swe, org.task)
    await grant(session, org, org.swe, "system.demo.destructive")
    session.add(
        ReviewPolicy(
            workspace_id=org.workspace.id,
            name="destructive needs manager",
            mode="pre_action",
            conditions_json=[{"kind": "destructive_action"}],
            reviewer_selector_json={"kind": "reporting_manager"},
            fail_closed=True,
        )
    )
    await session.flush()
    ctx = ToolExecutionContext(
        session=session,
        workspace_id=org.workspace.id,
        task_id=org.task.id,
        run_id=run.id,
        agent_id=org.swe.id,
        agent_name=org.swe.name,
    )
    gateway = ToolGateway(ctx, build_builtin_catalog())
    arguments = json.dumps({"label": "drop it"})

    pending = await gateway.request("system.demo.destructive", arguments)
    assert pending.status == "denied"
    assert pending.decision_code == "review_pending"
    review = await session.scalar(select(WorkReview))
    assert review is not None and review.status == "pending"
    assert str(review.id) in pending.decision_reason
    assert review.reviewer_agent_id == org.cto.id
    assert "tool.call.executed" not in list(await session.scalars(select(AuditEvent.action)))

    # An ungated tool is unaffected.
    await grant(session, org, org.swe, "system.note.append")
    note = await gateway.request("system.note.append", json.dumps({"text": "routine"}))
    assert note.status == "executed", note.decision_reason

    await decide_review(
        session,
        review,
        verdict="changes_requested",
        feedback="Back up the table first.",
        decided_by_agent_id=org.cto.id,
    )
    blocked = await gateway.request("system.demo.destructive", arguments)
    assert blocked.status == "denied"
    assert blocked.decision_code == "review_changes_requested"
    assert blocked.decision_reason == "Back up the table first."

    # Another run opens its own review; approved, the call executes.
    later_run = await run_for(session, org, org.swe, org.task)
    later_gateway = ToolGateway(replace(ctx, run_id=later_run.id), build_builtin_catalog())
    later = await later_gateway.request("system.demo.destructive", arguments)
    assert later.decision_code == "review_pending"
    reviews = list(await session.scalars(select(WorkReview).order_by(WorkReview.created_at)))
    assert len(reviews) == 2 and reviews[-1].run_id == later_run.id
    await decide_review(
        session, reviews[-1], verdict="approve", feedback="ok", decided_by_agent_id=org.cto.id
    )
    # The gate passed; the destructive call advances to human approval
    # staging (review gate -> human approval -> execute).
    staged = await later_gateway.request("system.demo.destructive", arguments)
    assert staged.status == "needs_approval", staged.decision_reason
    assert staged.approval_id is not None
