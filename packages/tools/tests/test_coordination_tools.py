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
from jhin_policy import (
    CoordinationSettings,
    Grant,
    GrantEffect,
    RiskLevel,
    collaboration_grant_specs,
)
from jhin_tools.builtin import (
    ToolExecutionContext,
    allowed_tool_definitions,
    build_builtin_catalog,
    task_scoped_tool_definitions,
)
from jhin_tools.directory import (
    DirectoryEntry,
    OrganizationRoster,
    _line,
    build_roster,
    find_agent_by_reference,
    render_roster,
    resolve_agent_reference,
    search_directory,
)
from jhin_tools.errors import ToolExecutionError
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.reviews import (
    ToolCallIntent,
    check_review_gate,
    decide_review,
    open_periodic_review,
    periodic_trigger_key,
)
from jhin_tools.rollups import ColleagueStatus, build_manager_rollup, render_manager_rollup
from jhin_tools.work_requests import (
    activate_work_request,
    derived_title,
    finalize_work_request,
)


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


async def human_in_the_loop(session: AsyncSession, org: Org) -> None:
    """Turn auto-activation off: requests then wait for an explicit accept."""
    org.workspace.settings_json = {
        **(org.workspace.settings_json or {}),
        "coordination": {
            **(org.workspace.settings_json or {}).get("coordination", {}),
            "auto_activate_targets": False,
        },
    }
    await session.flush()


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
    # Everyone else discoverable in the workspace is covered too — but the
    # hidden agent never is.
    assert roster.others == []
    assert "Shadow" not in {e.name for e in roster.entries()}
    assert len(roster.entries()) <= 40
    text = render_roster(roster)
    assert "Your manager:" in text and "CTO" in text
    cto_roster = await build_roster(session, org.cto)
    assert {e.name for e in cto_roster.reports} == {"QA", "SWE"}


async def test_roster_reaches_the_rest_of_the_workspace(session: AsyncSession, org: Org) -> None:
    """ "Who else works here?" is answerable without a tool call: an agent
    with no manager, team, or collaborators still sees the company."""
    loner = Agent(workspace_id=org.workspace.id, name="Loner", slug="loner", role_title="Intern")
    session.add(loner)
    await session.flush()
    roster = await build_roster(session, loner)
    assert {e.name for e in roster.others} == {"Blogger", "CTO", "QA", "SWE"}
    assert "Shadow" not in {e.name for e in roster.entries()}  # hidden stays hidden
    text = render_roster(roster)
    assert "Others in this workspace:" in text
    assert "CTO — Chief Technology Officer, Engineering team." in text


async def test_roster_is_framed_as_knowledge_the_agent_may_answer_from(
    session: AsyncSession, org: Org
) -> None:
    text = render_roster(await build_roster(session, org.swe))
    # Framing: it is the agent's own knowledge, and it is meant to be used.
    assert text.startswith("Your colleagues.")
    assert "answer them from this list, by name, in your own words" in text
    # ...and the security statement survives, in plain words.
    assert "Knowing a colleague is not permission to act for them" in text
    assert "relationships here grant no capabilities" in text
    assert "only through the tools you have been granted" in text
    # Human-readable identity first; the roster no longer leads with a UUID.
    assert "- CTO — Chief Technology Officer, Engineering team." in text
    assert str(org.cto.id) not in text


async def test_roster_prints_ids_only_for_agents_with_a_tool_that_takes_one(
    session: AsyncSession, org: Org
) -> None:
    roster = await build_roster(session, org.swe)
    # Directory read alone needs no agent id.
    read_only = render_roster(roster, capabilities=["organization.directory.read"])
    assert str(org.cto.id) not in read_only
    assert "agent id" not in read_only
    # ...but it does earn the "look further" nudge.
    assert "organization.directory.search before answering" in read_only

    for capability in ("organization.work.request", "organization.delegate", "organization.*"):
        text = render_roster(roster, capabilities=[capability])
        assert f"[agent id: {org.cto.id}]" in text, capability
        assert "Never write an id in a message to a person" in text
        # The id trails the human-readable identity rather than leading it.
        line = next(ln for ln in text.splitlines() if ln.startswith("- CTO"))
        assert line.index("Chief Technology Officer") < line.index("agent id")

    bare = render_roster(roster)
    assert "agent id" not in bare and "organization.directory.search" not in bare


async def test_two_person_company_reads_sensibly(session: AsyncSession) -> None:
    """The reported bug's shape: one agent, one manager, nobody else."""
    workspace = Workspace(name="Varand", slug=f"varand-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    engineering = Team(workspace_id=workspace.id, name="Engineering")
    session.add(engineering)
    await session.flush()
    cto = Agent(
        workspace_id=workspace.id,
        team_id=engineering.id,
        name="CTO",
        slug="cto",
        role_title="Chief Technology Officer",
    )
    session.add(cto)
    await session.flush()
    bisby = Agent(
        workspace_id=workspace.id,
        team_id=engineering.id,
        manager_agent_id=cto.id,
        name="Bisby",
        slug="bisby",
        role_title="Senior Software Engineer",
    )
    session.add(bisby)
    await session.flush()

    text = render_roster(await build_roster(session, bisby))
    assert "You are Bisby, Senior Software Engineer on the Engineering team." in text
    assert "Your manager:\n- CTO — Chief Technology Officer, Engineering team." in text
    # No empty sections, and no confusing duplicate of the manager.
    assert "Your team (Engineering):" not in text
    assert "Others in this workspace:" not in text
    assert text.count("CTO") == 1
    assert "only agent in this workspace" not in text


async def test_solo_agent_says_so_instead_of_rendering_an_empty_roster(
    session: AsyncSession,
) -> None:
    workspace = Workspace(name="Solo", slug=f"solo-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    only = Agent(workspace_id=workspace.id, name="Only", slug="only", role_title="Generalist")
    session.add(only)
    await session.flush()
    text = render_roster(await build_roster(session, only))
    assert "You are the only agent in this workspace right now" in text
    assert "Your manager" not in text


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
    # The explicit accept/decline path, with auto-activation turned off.
    await human_in_the_loop(session, org)
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
    await human_in_the_loop(session, org)
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


async def test_request_auto_activates_the_target(session: AsyncSession, org: Org) -> None:
    """The reported bug: asking a colleague used to leave a pending row that
    nothing ever woke. A permitted request now starts the target itself."""
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    outcome = await request_work(session, org, org.swe, org.blogger)
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert output["status"] == WorkRequestStatus.ACCEPTED.value
    assert output["activated"] is True
    assert output["created_task_id"]
    # The workflow needs the *target*'s id to run their task.
    assert output["agent_id"] == str(org.blogger.id)
    # What the model is told to do next, now that its turn is held open for
    # the answer: report what the colleague said, and never promise one.
    assert "started on it" in output["detail"]
    assert "held open until they answer" in output["detail"]
    assert "do not need to wait" not in output["detail"]

    request = await session.scalar(select(WorkRequest))
    assert request is not None
    assert request.status == WorkRequestStatus.ACCEPTED.value
    created = await session.get(Task, UUID(output["created_task_id"]))
    assert created is not None
    assert request.created_task_id == created.id
    assert created.assigned_agent_id == org.blogger.id
    assert created.parent_task_id is None
    assert created.conversation_id == org.task.conversation_id
    assert created.temporal_workflow_id == f"task-{created.id}"
    assert created.metadata_json["work_request"]["auto_activated"] is True
    # The colleague is told the ask is incoming, so it answers instead of
    # trying to relay the question onward.
    assert created.description.startswith("SWE asked you this. Answer it yourself")
    assert "Summarize the changes for the 2.0 release." in created.description
    # Auto-acceptance is the platform's act, not the target agent's.
    accepted_audit = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "work_request.accepted")
    )
    assert accepted_audit is not None
    assert accepted_audit.actor_type == "system" and accepted_audit.actor_id is None
    assert accepted_audit.metadata_json["auto_activated"] is True

    # A retried invocation of the same ask creates no second request or task.
    again = await request_work(session, org, org.swe, org.blogger)
    again_output = again.sanitized_output or {}
    assert again_output["created"] is False
    assert again_output["created_task_id"] == output["created_task_id"]
    assert again_output["activated"] is True
    assert len(list(await session.scalars(select(WorkRequest)))) == 1
    assert (
        len(
            list(
                await session.scalars(
                    select(Task).where(Task.metadata_json["origin"].as_string() == "work_request")
                )
            )
        )
        == 1
    )

    # The answer lands on the requester's own task, so it shows up in the
    # conversation the person is watching.
    created.metadata_json = {
        **created.metadata_json,
        "reported_result": {"summary": "Working on the 2.0 migration.", "status": "completed"},
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
    assert result.content_json["summary"] == "Working on the 2.0 migration."
    assert result.content_json["from_agent_name"] == "Blogger"


async def test_auto_activation_is_workspace_configurable(session: AsyncSession, org: Org) -> None:
    await human_in_the_loop(session, org)
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    output = (await request_work(session, org, org.swe, org.blogger)).sanitized_output or {}
    assert output["status"] == WorkRequestStatus.PENDING.value
    assert output["activated"] is False and output["created_task_id"] == ""
    # The model is told the truth so it does not promise an answer.
    assert "waiting for a human" in output["detail"]


async def test_activation_failure_is_terminal_with_a_reason(
    session: AsyncSession, org: Org
) -> None:
    """A request that cannot be started never stays pending: it fails with a
    readable reason posted back into the requester's conversation."""
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    await human_in_the_loop(session, org)
    request_id = (await request_work(session, org, org.swe, org.blogger)).sanitized_output or {}
    request = await session.get(WorkRequest, UUID(request_id["work_request_id"]))
    assert request is not None and request.status == WorkRequestStatus.PENDING.value

    # The colleague goes inactive between the ask and the activation.
    org.blogger.status = "archived"
    await session.flush()
    activation = await activate_work_request(
        session, request, settings=CoordinationSettings(), target_name="Blogger"
    )
    assert activation.activated is False
    assert "target_inactive" in activation.detail
    await session.refresh(request)
    assert request.status == WorkRequestStatus.FAILED.value
    assert request.completed_at is not None
    message = await session.scalar(
        select(Message).where(Message.message_type == MessageType.RESULT.value)
    )
    assert message is not None
    assert message.task_id == org.task.id
    assert "Could not start Blogger" in message.content_json["summary"]
    assert message.content_json["failure_code"] == "target_inactive"
    # Idempotent: a second attempt does not post a second failure.
    assert (
        await activate_work_request(
            session, request, settings=CoordinationSettings(), target_name="Blogger"
        )
    ).activated is False
    assert (
        len(
            list(
                await session.scalars(
                    select(Message).where(Message.message_type == MessageType.RESULT.value)
                )
            )
        )
        == 1
    )


async def test_auto_activated_mutual_ask_cannot_loop(session: AsyncSession, org: Org) -> None:
    """Two agents asking each other must not ping-pong: the guards run before
    the row exists and auto-activation never gets a chance to widen them."""
    await grant(session, org, org.swe, "organization.work.request", {"targets": "any"})
    await grant(session, org, org.blogger, "organization.work.request", {"targets": "any"})
    first = (await request_work(session, org, org.swe, org.blogger)).sanitized_output or {}
    created = await session.get(Task, UUID(first["created_task_id"]))
    assert created is not None

    back = await org.gateway(session, org.blogger, created).request(
        "organization.request_work", request_args(org.swe, idempotency_key="back")
    )
    assert back.status == "denied" and back.decision_code == "request_ping_pong"
    assert "answer it instead" in (back.decision_reason or "")

    # The depth cap bounds a chain that walks forward instead of back.
    org.workspace.settings_json = {"coordination": {"max_request_depth": 1}}
    await session.flush()
    deeper = await org.gateway(session, org.blogger, created).request(
        "organization.request_work", request_args(org.qa, idempotency_key="deeper")
    )
    assert deeper.status == "denied" and deeper.decision_code == "request_depth_exceeded"


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


# --- colleague references by name ---------------------------------------
#
# Regression cluster for the reported bug: Bisby held organization.work.request
# and was told "can you ask him", yet answered "I can't see the CTO's current
# activity". Two things were missing — a way to *look* (colleague_status) and
# a way to *ask* using the only handle an agent has for a colleague: a name.


async def test_a_colleague_resolves_by_name_case_insensitively(
    session: AsyncSession, org: Org
) -> None:
    ws = org.workspace.id

    async def find(**kwargs: Any) -> Any:
        return await find_agent_by_reference(session, ws, **kwargs)

    exact = await find(agent_name="CTO")
    assert exact is not None and not isinstance(exact, str) and exact.id == org.cto.id
    for spelling in ("cto", "  CtO ", "Chief Technology Officer", "chief technology officer"):
        match = await find(agent_name=spelling)
        assert match is not None and not isinstance(match, str), spelling
        assert match.id == org.cto.id, spelling
    # Slug, and a substring unique to one colleague.
    by_slug = await find(agent_name="blogger")
    assert by_slug is not None and not isinstance(by_slug, str) and by_slug.id == org.blogger.id
    by_part = await find(agent_name="Blog")
    assert by_part is not None and not isinstance(by_part, str) and by_part.id == org.blogger.id
    # An id still wins, and a malformed one is simply not found.
    by_id = await find(agent_id=str(org.qa.id))
    assert by_id is not None and not isinstance(by_id, str) and by_id.id == org.qa.id
    assert await find(agent_id="not-a-uuid") is None


async def test_an_ambiguous_or_unknown_name_says_so_and_names_the_candidates(
    session: AsyncSession, org: Org
) -> None:
    ws = org.workspace.id
    # "Engineer" is a substring of two role titles: never silently pick one.
    assert await find_agent_by_reference(session, ws, agent_name="Engineer") == "ambiguous"
    with pytest.raises(ToolExecutionError) as ambiguous:
        await resolve_agent_reference(session, ws, agent_name="Engineer", role="colleague")
    assert ambiguous.value.code == "agent_name_ambiguous"

    assert await find_agent_by_reference(session, ws, agent_name="Nobody At All") is None
    with pytest.raises(ToolExecutionError) as unknown:
        await resolve_agent_reference(session, ws, agent_name="Nobody At All")
    assert unknown.value.code == "agent_not_found"
    # The retry hint names real colleagues...
    assert "CTO" in unknown.value.hint and "SWE" in unknown.value.hint
    # ...but a wrong name is never a way to enumerate hidden ones.
    assert "Shadow" not in unknown.value.hint

    # Cross-workspace: a valid agent id from elsewhere resolves to nothing.
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    outsider = Agent(workspace_id=other.id, name="CTO", slug="cto")
    session.add(outsider)
    await session.flush()
    assert await find_agent_by_reference(session, ws, agent_id=str(outsider.id)) is None
    mine = await find_agent_by_reference(session, ws, agent_name="CTO")
    assert mine is not None and not isinstance(mine, str) and mine.id == org.cto.id


# --- organization.colleague_status --------------------------------------


async def _busy_cto(session: AsyncSession, org: Org) -> tuple[Task, Task, Task]:
    """The CTO with one running task, one queued, and one just finished."""
    ws = org.workspace.id
    running = Task(
        workspace_id=ws,
        title="Architecture review",
        description="Confidential rewrite plan the CTO has not shared.",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.cto.id,
        correlation_id=new_uuid7(),
        metadata_json={"reported_result": {"summary": "internal result summary"}},
    )
    queued = Task(
        workspace_id=ws,
        title="Hiring plan",
        state=TaskState.QUEUED.value,
        assigned_agent_id=org.cto.id,
        correlation_id=new_uuid7(),
    )
    done = Task(
        workspace_id=ws,
        title="Q3 roadmap",
        state=TaskState.COMPLETED.value,
        assigned_agent_id=org.cto.id,
        correlation_id=new_uuid7(),
    )
    session.add_all([running, queued, done])
    await session.flush()
    session.add(
        AgentRun(
            workspace_id=ws,
            agent_id=org.cto.id,
            task_id=running.id,
            status=RunStatus.RUNNING.value,
        )
    )
    await session.flush()
    return running, queued, done


async def test_colleague_status_answers_what_a_colleague_is_doing_right_now(
    session: AsyncSession, org: Org
) -> None:
    await _busy_cto(session, org)
    await grant(session, org, org.swe, "organization.directory.read")

    outcome = await org.gateway(session, org.swe).request(
        "organization.colleague_status", json.dumps({"agent_name": "cto"})
    )
    assert outcome.status == "executed", outcome.decision_reason
    status = outcome.sanitized_output or {}
    assert status["name"] == "CTO"
    assert status["role_title"] == "Chief Technology Officer"
    assert status["team_name"] == "Engineering"
    assert status["working_now"] is True and status["active_runs"] == 1
    assert [item["title"] for item in status["current_work"]] == ["Architecture review"]
    assert status["current_work"][0]["run_status"] == "running"
    assert [item["title"] for item in status["queued_work"]] == ["Hiring plan"]
    assert [item["title"] for item in status["recently_finished"]] == ["Q3 roadmap"]
    assert status["last_active_at"]
    # One ready-made sentence, so the agent has something to say back.
    assert "CTO" in status["summary"] and "Architecture review" in status["summary"]


async def test_colleague_status_counts_what_is_waiting_on_them(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.swe, "organization.directory.read")
    session.add(
        WorkRequest(
            workspace_id=org.workspace.id,
            requester_agent_id=org.qa.id,
            target_agent_id=org.cto.id,
            title="Review the migration",
            description="please look",
            status=WorkRequestStatus.PENDING.value,
            idempotency_key=f"k-{new_uuid7().hex[:8]}",
        )
    )
    session.add(
        WorkReview(
            workspace_id=org.workspace.id,
            trigger_key=f"t-{new_uuid7().hex[:8]}",
            mode="pre_action",
            reviewer_type="agent",
            reviewer_agent_id=org.cto.id,
            subject_agent_id=org.qa.id,
            requested_at=datetime.now(UTC),
        )
    )
    await session.flush()
    outcome = await org.gateway(session, org.swe).request(
        "organization.colleague_status", json.dumps({"agent_name": "CTO"})
    )
    status = outcome.sanitized_output or {}
    assert status["open_requests_awaiting_them"] == 1
    assert status["pending_reviews"] == 1
    assert status["pending_approvals"] == 0
    assert status["working_now"] is False
    assert "Waiting on them" in status["summary"]
    # Counts only: what they were asked is never named.
    assert "Review the migration" not in json.dumps(status)


async def test_colleague_status_is_public_work_status_and_nothing_else(
    session: AsyncSession, org: Org
) -> None:
    """The privacy contract, asserted as negatives."""
    running, _queued, _done = await _busy_cto(session, org)
    org.cto.system_prompt = "SECRET-CTO-INSTRUCTIONS"
    org.cto.metadata_json = {"private_note": "SECRET-CTO-METADATA"}
    session.add(
        Message(
            workspace_id=org.workspace.id,
            task_id=running.id,
            sender_type="agent",
            sender_id=org.cto.id,
            recipient_type="task",
            recipient_id=running.id,
            message_type="note",
            content_json={"text": "SECRET-CTO-CONVERSATION"},
        )
    )
    await grant(session, org, org.cto, "cli.command.execute", {"command": "SECRET-CTO-GRANT"})
    await grant(session, org, org.swe, "organization.directory.read")
    await session.flush()

    outcome = await org.gateway(session, org.swe).request(
        "organization.colleague_status", json.dumps({"agent_name": "CTO"})
    )
    status = outcome.sanitized_output or {}
    blob = json.dumps(status)
    for secret in (
        "SECRET-CTO-INSTRUCTIONS",  # another agent's system prompt
        "SECRET-CTO-METADATA",  # private metadata
        "SECRET-CTO-CONVERSATION",  # message / conversation content
        "SECRET-CTO-GRANT",  # capability grants
        "Confidential rewrite plan",  # task descriptions
        "internal result summary",  # reported-result summaries
    ):
        assert secret not in blob, secret
    # The payload is exactly the declared allowlist — a new field has to be
    # added here deliberately, with the privacy reasoning in ColleagueStatus.
    assert set(status) == set(ColleagueStatus.model_fields)
    assert set(ColleagueStatus.model_fields) == {
        "agent_id",
        "name",
        "role_title",
        "team_name",
        "status",
        "availability",
        "working_now",
        "active_runs",
        "current_work",
        "queued_work",
        "recently_finished",
        "open_requests_awaiting_them",
        "pending_reviews",
        "pending_approvals",
        "last_active_at",
        "generated_at",
        "summary",
    }


async def test_colleague_status_never_reaches_hidden_agents_or_another_workspace(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.swe, "organization.directory.read")
    gateway = org.gateway(session, org.swe)

    hidden_by_name = await gateway.request(
        "organization.colleague_status", json.dumps({"agent_name": "Shadow"})
    )
    assert hidden_by_name.status == "failed"
    assert hidden_by_name.error_code == "agent_not_found"
    # ...and knowing the id does not help either: a status lookup is discovery.
    hidden_by_id = await gateway.request(
        "organization.colleague_status", json.dumps({"agent_id": str(org.hidden.id)})
    )
    assert hidden_by_id.error_code == "agent_not_found"

    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    outsider = Agent(workspace_id=other.id, name="Outsider", slug="outsider")
    session.add(outsider)
    await session.flush()
    across = await gateway.request(
        "organization.colleague_status", json.dumps({"agent_id": str(outsider.id)})
    )
    assert across.error_code == "agent_not_found"

    # Asking about yourself is a mistake worth naming, not a status card.
    mirror = await gateway.request(
        "organization.colleague_status", json.dumps({"agent_name": "SWE"})
    )
    assert mirror.error_code == "self_status"


async def test_colleague_status_needs_the_directory_grant(session: AsyncSession, org: Org) -> None:
    """The negative the product depends on: an agent without the capability
    is told it cannot look, rather than failing silently."""
    denied = await org.gateway(session, org.qa).request(
        "organization.colleague_status", json.dumps({"agent_name": "CTO"})
    )
    assert denied.status == "denied" and denied.decision_code == "no_grant"
    assert "organization.directory.read" in (denied.decision_reason or "")


# --- asking a colleague by name -----------------------------------------


async def test_request_work_reaches_a_colleague_by_name_with_only_a_question(
    session: AsyncSession, org: Org
) -> None:
    """ "Can you ask him what he's working on" must be one cheap tool call:
    a name and a question, no uuid, no title, no expected output."""
    for capability, scope in collaboration_grant_specs():
        await grant(session, org, org.swe, capability, scope)
    outcome = await org.gateway(session, org.swe).request(
        "organization.request_work",
        json.dumps(
            {"target_agent_name": "cto", "description": "What are you working on right now?"}
        ),
    )
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert output["created"] is True
    assert output["target_agent_name"] == "CTO"

    request = await session.scalar(select(WorkRequest))
    assert request is not None
    assert request.target_agent_id == org.cto.id
    assert request.requester_agent_id == org.swe.id
    # A missing title becomes the ask itself rather than blocking the call.
    assert request.title == "What are you working on right now?"


async def test_request_work_by_name_is_idempotent_with_the_id_form(
    session: AsyncSession, org: Org
) -> None:
    for capability, scope in collaboration_grant_specs():
        await grant(session, org, org.swe, capability, scope)
    gateway = org.gateway(session, org.swe)
    body = {"description": "What are you working on right now?"}
    first = await gateway.request(
        "organization.request_work", json.dumps({"target_agent_name": "CTO", **body})
    )
    assert first.status == "executed", first.decision_reason
    again = await gateway.request(
        "organization.request_work", json.dumps({"target_agent_id": str(org.cto.id), **body})
    )
    # The default key is built from the *resolved* target, so naming and
    # id-ing the same colleague is the same request.
    assert (again.sanitized_output or {})["created"] is False
    assert len(list(await session.scalars(select(WorkRequest)))) == 1


async def test_request_work_with_a_bad_name_explains_and_names_candidates(
    session: AsyncSession, org: Org
) -> None:
    for capability, scope in collaboration_grant_specs():
        await grant(session, org, org.swe, capability, scope)
    gateway = org.gateway(session, org.swe)

    unknown = await gateway.request(
        "organization.request_work",
        json.dumps({"target_agent_name": "Nobody At All", "description": "hello?"}),
    )
    assert unknown.status == "denied" and unknown.decision_code == "agent_not_found"
    assert "CTO" in (unknown.decision_reason or "")
    assert "Shadow" not in (unknown.decision_reason or "")

    ambiguous = await gateway.request(
        "organization.request_work",
        json.dumps({"target_agent_name": "Engineer", "description": "hello?"}),
    )
    assert ambiguous.decision_code == "agent_name_ambiguous"

    nobody = await gateway.request(
        "organization.request_work", json.dumps({"description": "hello?"})
    )
    assert nobody.status == "denied" and nobody.decision_code == "invalid_input"
    assert await session.scalar(select(WorkRequest)) is None


def test_derived_title_is_bounded_and_deterministic() -> None:
    assert derived_title("Given", "ignored") == "Given"
    assert derived_title("", "What are you working on?") == "What are you working on?"
    assert derived_title("", "Two things. The second one.") == "Two things."
    long_ask = "x" * 400
    assert len(derived_title("", long_ask)) <= 118
    assert derived_title("", long_ask) == derived_title("", long_ask)
    assert derived_title("", "   ") == "A question from a colleague"


def test_asking_a_colleague_is_advertised_on_an_ordinary_chat_turn() -> None:
    """The advertisement regression behind the report: on the exact task
    shape of a chat message, an agent holding the collaboration baseline is
    still offered both "look it up" and "go ask"."""
    baseline = [
        Grant(capability=capability, scope=scope, effect=GrantEffect.ALLOW)
        for capability, scope in collaboration_grant_specs()
    ]
    chat_turn = Task(
        workspace_id=new_uuid7(),
        title="What is the CTO doing right now?",
        state=TaskState.RUNNING.value,
        correlation_id=new_uuid7(),
        conversation_id=new_uuid7(),
        metadata_json={"origin": "conversation"},
    )
    names = {
        definition.name
        for definition in task_scoped_tool_definitions(
            allowed_tool_definitions(build_builtin_catalog(), baseline), chat_turn
        )
    }
    assert "organization.request_work" in names
    assert "organization.colleague_status" in names
    assert "organization.directory.search" in names
    # ...and the reporting tool still stays out of a chat turn.
    assert "organization.report_result" not in names


def test_the_ask_tool_reads_like_asking_a_colleague() -> None:
    """Wording is load-bearing: the previous description ("help with a piece
    of work", "a separate task is created") read as task-routing machinery,
    so the model never mapped "can you ask him" onto it."""
    catalog = build_builtin_catalog()
    entry = catalog.get("organization.request_work")
    assert entry is not None
    description = entry[0].description
    assert description.startswith("Ask a colleague something and get an answer back.")
    assert "can you ask" in description.lower()
    assert "target_agent_name" in description

    schema = entry[0].input_json_schema()
    # The cheap common case: a name and a question, nothing else.
    assert schema["required"] == ["description"]
    assert "target_agent_name" in schema["properties"]

    status = catalog.get("organization.colleague_status")
    assert status is not None
    assert "what a colleague is doing right now" in status[0].description
    assert status[0].risk is RiskLevel.READ
    assert status[0].supports_approval is False
    assert status[0].required_capability == "organization.directory.read"


async def test_a_prose_answer_is_relayed_not_reported_as_empty(
    session: AsyncSession, org: Org
) -> None:
    """A colleague asked a question usually just answers it rather than
    calling ``organization.report_result``. Before this, the requester was
    handed "Finished: <title>" and truthfully told the person that no details
    came back -- with the answer sitting one table away."""
    await human_in_the_loop(session, org)
    await grant(session, org, org.swe, "organization.work.request", {"targets": "team"})
    outcome = await request_work(session, org, org.swe, org.qa)
    assert outcome.status == "executed", outcome.decision_reason
    request = await session.scalar(select(WorkRequest))
    assert request is not None
    inbox = Task(
        workspace_id=org.workspace.id,
        title="Inbox",
        state=TaskState.RUNNING.value,
        assigned_agent_id=org.qa.id,
        correlation_id=new_uuid7(),
    )
    session.add(inbox)
    await session.flush()
    await grant(session, org, org.qa, "organization.work.respond")
    accepted = await respond(session, org, org.qa, str(request.id), "accept", inbox)
    assert accepted.status == "executed", accepted.decision_reason
    await session.refresh(request)
    created = await session.get(Task, request.created_task_id)
    assert created is not None

    session.add(
        Message(
            workspace_id=org.workspace.id,
            task_id=created.id,
            sender_type="agent",
            sender_id=org.qa.id,
            recipient_type="agent",
            recipient_id=org.swe.id,
            message_type=MessageType.TEXT.value,
            content_json={"text": "I am reviewing the retry branch and the release checklist."},
            visibility="visible",
        )
    )
    await session.flush()

    done = await finalize_work_request(
        session, workspace_id=org.workspace.id, request_id=request.id, run_status="completed"
    )
    assert done is not None and done.status == WorkRequestStatus.COMPLETED.value
    result = await session.scalar(
        select(Message)
        .where(Message.message_type == MessageType.RESULT.value)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    assert result is not None
    summary = result.content_json["summary"]
    assert "retry branch" in summary
    assert not summary.startswith("Finished:")


def test_a_colleague_with_no_team_says_so_rather_than_leaving_a_gap() -> None:
    """Omitting the team read as "unstated" rather than "none". Asked which
    team such a colleague was on, an agent invented one out of their expertise
    tags -- a Chief of Staff with "operations, planning" became "on the
    Operations team", stated to the person as fact."""
    entry = DirectoryEntry(
        id="01a0",
        name="Alder",
        slug="alder",
        role_title="Chief of Staff",
        expertise=["operations", "planning", "routing"],
    )
    line = _line(entry, with_id=False)
    assert "not on a team" in line

    on_a_team = entry.model_copy(update={"primary_team_name": "Platform"})
    assert "Platform team" in _line(on_a_team, with_id=False)
    assert "not on a team" not in _line(on_a_team, with_id=False)


def test_the_agent_states_its_own_missing_team_and_manager() -> None:
    """The colleague line was fixed; the self line and the manager section had
    the identical gap. A teamless agent rendered "You are Alder, Chief of
    Staff." and, asked what team it was on, invented one from its own role."""
    alone = DirectoryEntry(
        id="01a0",
        name="Alder",
        slug="alder",
        role_title="Chief of Staff",
        expertise=["operations", "planning"],
    )
    text = render_roster(
        OrganizationRoster(
            self_entry=alone,
            manager=None,
            reports=[],
            primary_team_members=[],
            collaborators=[],
            secondary_team_members=[],
            others=[DirectoryEntry(id="01a1", name="Rowan", slug="rowan")],
            truncated=False,
        )
    )
    assert "you are not on a team" in text
    assert "no manager in this workspace" in text
