"""How long the agent has been thinking, with the person's waiting taken out.

The bug these cover, proven on the operator's own database: a turn is one
``agent_run`` whose ``started_at`` is stamped once and never re-stamped. When
an approval is decided or a question answered, the worker puts the run back to
``running`` carrying that original stamp — so a chat pill counting from it
shows the reader their own deliberation, labelled as the agent's thinking. Run
``01a076a5-d23f-7691-bb64-21f265ce2a3a`` ran twenty minutes and thought for
fifty seconds; the approval on run ``01a0750d-a11a-70e0-969b-182593285740``
was decided ten and a half minutes after it was requested, all of it counted.

The correction is a derivation, not a re-stamp: ``started_at`` still means
when the run began (the metrics and the audit read it that way), and the waits
come out of the count from the rows that *are* the waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.conversations import service
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AgentRun, Approval, Conversation, Task, UserQuestion, WorkReview
from jhin_domain import (
    AgentStatus,
    ApprovalStatus,
    ConversationStatus,
    ReviewerType,
    RunStatus,
    TaskState,
    UserQuestionStatus,
    WorkReviewStatus,
    new_uuid7,
)

START = datetime(2026, 9, 6, 12, 16, 13, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def moment(value: datetime | None) -> datetime | None:
    """The instant, without arguing about tzinfo.

    Postgres hands these back in UTC and the SQLite the unit tests run on
    hands them back naive. Nothing here is testing the driver.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture
async def agent(session: AsyncSession, admin_ctx: WorkspaceContext) -> Agent:
    row = Agent(
        workspace_id=admin_ctx.workspace_id,
        name="Bisby",
        slug="bisby",
        role_title="Eng",
        status=AgentStatus.ACTIVE.value,
    )
    session.add(row)
    await session.flush()
    return row


async def running_turn(
    session: AsyncSession,
    ctx: WorkspaceContext,
    agent: Agent,
    *,
    run_status: str = RunStatus.RUNNING.value,
    completed_at: datetime | None = None,
    task_state: str = TaskState.RUNNING.value,
) -> tuple[Conversation, Task, AgentRun]:
    """A conversation whose one turn is in flight, started at :data:`START`."""
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        title="Chat",
        status=ConversationStatus.ACTIVE.value,
        primary_agent_id=agent.id,
        created_by_user_id=ctx.user.id,
        last_activity_at=START,
    )
    session.add(conversation)
    await session.flush()
    task = Task(
        workspace_id=ctx.workspace_id,
        title="Turn",
        description="do the thing",
        state=task_state,
        assigned_agent_id=agent.id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    run = AgentRun(
        workspace_id=ctx.workspace_id,
        agent_id=agent.id,
        task_id=task.id,
        status=run_status,
        started_at=START,
        completed_at=completed_at,
    )
    session.add(run)
    await session.commit()
    return conversation, task, run


async def project(session: AsyncSession, ctx: WorkspaceContext, conversation: Conversation) -> Any:
    [row] = await service.project_conversations(session, ctx.workspace_id, [conversation])
    return row


async def test_a_turn_nobody_interrupted_counts_from_where_it_started(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    conversation, _task, _run = await running_turn(session, admin_ctx, agent)

    row = await project(session, admin_ctx, conversation)

    assert moment(row.active_run_working_since) == START
    assert row.active_run_working_seconds == 0
    # And the run's own start is still sent, still meaning what it always did.
    assert moment(row.active_run_started_at) == START


async def test_an_approval_left_overnight_is_not_hours_of_thinking(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """The incident, in the shape the live database has it: ten seconds of
    work, ten and a half minutes of a person deciding, then work again."""
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    session.add(
        Approval(
            workspace_id=admin_ctx.workspace_id,
            task_id=task.id,
            run_id=run.id,
            action_type="cli.command.execute",
            status=ApprovalStatus.APPROVED.value,
            requested_at=at(9.85),
            decided_at=at(640.8),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    # Ten seconds, not nine: the banked total is rounded to the nearest
    # second rather than truncated, which used to cost up to a second a wait.
    assert row.active_run_working_seconds == 10
    assert moment(row.active_run_working_since) == at(640.8)
    # What the pill would have said instead, and the whole reason for this.
    assert (at(645) - moment(row.active_run_started_at)).total_seconds() == pytest.approx(645)


async def test_a_question_still_unanswered_stops_the_clock(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """A pill that keeps counting while the agent waits on the reader is
    counting the reader. There is no stretch of thinking in progress, so the
    API sends no instant to count from and the client shows no clock."""
    conversation, task, run = await running_turn(
        session, admin_ctx, agent, run_status=RunStatus.WAITING_PERSON.value
    )
    session.add(
        UserQuestion(
            workspace_id=admin_ctx.workspace_id,
            conversation_id=conversation.id,
            task_id=task.id,
            run_id=run.id,
            agent_id=agent.id,
            question="Which repository?",
            dedupe_hash="h",
            idempotency_key=str(new_uuid7()),
            status=UserQuestionStatus.PENDING.value,
            asked_at=at(16.9),
            expires_at=at(86_400),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_since is None
    assert row.active_run_working_seconds == 17


async def test_thinking_resumes_where_it_stopped_rather_than_at_zero(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Run 01a076a5, answered: seventeen seconds banked, and the clock running
    again from the moment the answer arrived."""
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    session.add(
        UserQuestion(
            workspace_id=admin_ctx.workspace_id,
            conversation_id=conversation.id,
            task_id=task.id,
            run_id=run.id,
            agent_id=agent.id,
            question="Which repository?",
            dedupe_hash="h",
            idempotency_key=str(new_uuid7()),
            status=UserQuestionStatus.ANSWERED.value,
            asked_at=at(16.9),
            expires_at=at(86_400),
            answered_at=at(1167.5),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_seconds == 17
    assert moment(row.active_run_working_since) == at(1167.5)
    # Twenty minutes of wall clock, fifty seconds of thought.
    assert row.active_run_working_seconds + int((at(1201) - at(1167.5)).total_seconds()) == 50


async def test_a_review_is_a_wait_too(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Parked on a reviewer is parked. The run is not thinking, whoever the
    decision is waiting on."""
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    session.add(
        WorkReview(
            workspace_id=admin_ctx.workspace_id,
            task_id=task.id,
            run_id=run.id,
            trigger_key=f"tk-{new_uuid7().hex[:8]}",
            mode="blocking",
            reviewer_type=ReviewerType.HUMAN.value,
            status=WorkReviewStatus.APPROVED.value,
            requested_at=at(30),
            decided_at=at(3630),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_seconds == 30
    assert moment(row.active_run_working_since) == at(3630)


async def test_two_waits_in_one_turn_both_come_out(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    session.add_all(
        [
            Approval(
                workspace_id=admin_ctx.workspace_id,
                task_id=task.id,
                run_id=run.id,
                action_type="cli.command.execute",
                status=ApprovalStatus.APPROVED.value,
                requested_at=at(10),
                decided_at=at(70),
            ),
            UserQuestion(
                workspace_id=admin_ctx.workspace_id,
                conversation_id=conversation.id,
                task_id=task.id,
                run_id=run.id,
                agent_id=agent.id,
                question="Which repository?",
                dedupe_hash="h",
                idempotency_key=str(new_uuid7()),
                status=UserQuestionStatus.ANSWERED.value,
                asked_at=at(100),
                expires_at=at(86_400),
                answered_at=at(400),
            ),
        ]
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_seconds == 40
    assert moment(row.active_run_working_since) == at(400)


async def test_a_wait_that_expired_is_over_even_with_no_answer(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """A question the run stopped waiting on has no ``answered_at``. Reading
    that as "still waiting" would freeze the clock for good, and the run is
    demonstrably working: it went back to ``running``."""
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    question = UserQuestion(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=conversation.id,
        task_id=task.id,
        run_id=run.id,
        agent_id=agent.id,
        question="Which repository?",
        dedupe_hash="h",
        idempotency_key=str(new_uuid7()),
        status=UserQuestionStatus.EXPIRED.value,
        asked_at=at(20),
        expires_at=at(200),
        updated_at=at(200),
    )
    session.add(question)
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_since is not None
    assert row.active_run_working_seconds == 20


async def test_a_conversation_with_nothing_running_carries_no_clock(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    conversation = Conversation(
        workspace_id=admin_ctx.workspace_id,
        title="Chat",
        status=ConversationStatus.ACTIVE.value,
        primary_agent_id=agent.id,
        created_by_user_id=admin_ctx.user.id,
        last_activity_at=START,
    )
    session.add(conversation)
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_since is None
    assert row.active_run_working_seconds == 0


async def test_a_pending_row_stamped_after_the_run_ended_is_not_two_hours_of_thought(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """The second live row, and the reason the run's end is now asked for.

    Approval ``01a075a5-1c5e-7153-9343-1bba04100e4b`` is still ``pending`` and
    was requested 8361.8s after run ``01a07525-8543-75b2-9285-600edffb8054``
    started — a run that completed after 58. An approval names a run without
    being bounded by it, and reading that row as a wait banked every second up
    to the request: 8361 seconds of thinking on a turn that lived a minute.
    Nothing renders it today only because ``working_since`` also comes back
    None; the number is on the wire either way, and the first surface to read
    it prints two hours of thought that never happened.
    """
    conversation, task, run = await running_turn(
        session,
        admin_ctx,
        agent,
        run_status=RunStatus.COMPLETED.value,
        completed_at=at(57.961),
        task_state=TaskState.RUNNING.value,
    )
    session.add(
        Approval(
            workspace_id=admin_ctx.workspace_id,
            task_id=task.id,
            run_id=run.id,
            action_type="cli.command.execute",
            status=ApprovalStatus.PENDING.value,
            requested_at=at(8361.756),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_seconds == 58
    # And no instant to count from: the run is over, so a client counting from
    # one would still be counting tomorrow.
    assert row.active_run_working_since is None


async def test_a_finished_run_hands_out_no_instant_to_keep_counting_from(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    conversation, task, run = await running_turn(
        session,
        admin_ctx,
        agent,
        run_status=RunStatus.COMPLETED.value,
        completed_at=at(100),
    )
    session.add(
        Approval(
            workspace_id=admin_ctx.workspace_id,
            task_id=task.id,
            run_id=run.id,
            action_type="cli.command.execute",
            status=ApprovalStatus.APPROVED.value,
            requested_at=at(10),
            decided_at=at(70),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_working_since is None
    assert row.active_run_working_seconds == 40


async def test_a_stale_pending_row_leaves_the_number_off_rather_than_wrong(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """A turn that is genuinely running while a wait row nobody closed says it
    is parked. The pair cannot both be true and the API refuses to guess: it
    sends the thinking it can vouch for and no instant, and the client says so
    in words rather than showing a clock that vanished (see the transcript's
    working indicator)."""
    conversation, task, run = await running_turn(session, admin_ctx, agent)
    session.add(
        Approval(
            workspace_id=admin_ctx.workspace_id,
            task_id=task.id,
            run_id=run.id,
            action_type="cli.command.execute",
            status=ApprovalStatus.PENDING.value,
            requested_at=at(12),
        )
    )
    await session.commit()

    row = await project(session, admin_ctx, conversation)

    assert row.active_run_status == RunStatus.RUNNING.value
    assert row.active_run_working_since is None
    assert row.active_run_working_seconds == 12
