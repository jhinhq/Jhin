"""Picking a failed turn back up, and what a person is told when they can't.

The incident these cover: a redeploy replaced the sandbox runner and the tool
worker mid-job, the tool call was reconciled as ``execution_unknown``, the run
failed, and the chat said "Run failed: tool call a34dd1dc-… execution outcome
is unknown; manual reconciliation is required" with no way forward at all.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.conversations import service
from jhin_api.deps import WorkspaceContext
from jhin_api.tasks import service as tasks_service
from jhin_db.models import Agent, AgentRun, AuditEvent, Conversation, Message, Task, ToolCall, User
from jhin_domain import (
    AgentStatus,
    ConversationStatus,
    MessageType,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
    WorkspaceRole,
    new_uuid7,
)

TURN_TEXT = "list the top level files"


class RecordingHandle:
    def __init__(self, client: RecordingTemporal, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def signal(self, name: str, *args: Any) -> None:
        self._client.signals.append((self._workflow_id, name, args))


class RecordingTemporal:
    """Records exactly what tasks.service.start_workflow / signal_task use.

    Named for what it does rather than for what it is not, matching the
    double of the same name in test_webhooks_unit.py.
    """

    def __init__(self) -> None:
        self.started: list[tuple[str, Any, str]] = []
        self.signals: list[tuple[str, str, tuple[Any, ...]]] = []

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str) -> None:
        self.started.append((name, arg, id))

    def get_workflow_handle(self, workflow_id: str) -> RecordingHandle:
        return RecordingHandle(self, workflow_id)


@pytest.fixture
def temporal() -> RecordingTemporal:
    return RecordingTemporal()


@pytest.fixture
async def agent(session: AsyncSession, admin_ctx: WorkspaceContext) -> Agent:
    row = Agent(workspace_id=admin_ctx.workspace_id, name="Bisby", slug="bisby", role_title="Eng")
    session.add(row)
    await session.flush()
    return row


async def start(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: RecordingTemporal,
    agent: Agent,
    text: str = TURN_TEXT,
) -> tuple[Conversation, service.TurnResult]:
    conversation, turn = await service.create_conversation(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        agent_id=agent.id,
        title=None,
        text=text,
        client_turn_id=None,
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert turn is not None
    return conversation, turn


async def failed_turn(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: RecordingTemporal,
    agent: Agent,
    *,
    text: str = TURN_TEXT,
    error_code: str = "tool_execution_unknown",
) -> tuple[Conversation, service.TurnResult, AgentRun]:
    """A conversation whose one turn died the way the incident's did."""
    conversation, turn = await start(session, ctx, temporal, agent, text=text)
    turn.task.state = TaskState.FAILED.value
    run = AgentRun(
        workspace_id=ctx.workspace_id,
        agent_id=agent.id,
        task_id=turn.task.id,
        status=RunStatus.FAILED.value,
        error_code=error_code,
        error_message=(
            f"tool call {new_uuid7()} execution outcome is unknown; "
            "manual reconciliation is required"
        ),
    )
    session.add(run)
    await session.flush()
    session.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=turn.task.id,
            run_id=run.id,
            conversation_id=None,
            sender_type=SenderType.SYSTEM.value,
            recipient_type="task",
            recipient_id=turn.task.id,
            message_type=MessageType.ERROR.value,
            content_json={
                "text": f"Run failed: {run.error_message}",
                "error_code": error_code,
            },
        )
    )
    await session.commit()
    return conversation, turn, run


async def called(
    session: AsyncSession,
    ctx: WorkspaceContext,
    agent: Agent,
    run: AgentRun,
    tool_name: str,
    status: str,
) -> ToolCall:
    call = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=agent.id,
        tool_name=tool_name,
        status=status,
        sanitized_input_json={},
    )
    session.add(call)
    await session.commit()
    return call


async def offer(session: AsyncSession, ctx: WorkspaceContext, conversation: Conversation) -> Any:
    detail = await service.get_detail(session, ctx.workspace_id, conversation.id)
    return detail.resume


async def resume(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: RecordingTemporal,
    conversation: Conversation,
) -> service.ResumeResult:
    return await service.resume_conversation(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        conversation.id,
        request_id=new_uuid7(),
        ip_hash="h",
    )


# --- What the chat offers -------------------------------------------------


async def test_a_failed_turn_is_offered_back_with_the_message_intact(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert resume_offer.task_id == turn.task.id
    # The whole point of the control: the words are already here, so nothing
    # has to be retyped to try again.
    assert resume_offer.instruction == TURN_TEXT
    assert resume_offer.reason.startswith("Bisby can pick this up")


async def test_a_live_turn_has_nothing_to_offer(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn = await start(session, admin_ctx, temporal, agent)

    assert await offer(session, admin_ctx, conversation) is None


async def test_a_finished_turn_has_nothing_to_offer(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, turn = await start(session, admin_ctx, temporal, agent)
    turn.task.state = TaskState.COMPLETED.value
    await session.commit()

    assert await offer(session, admin_ctx, conversation) is None


async def test_only_the_newest_turn_is_ever_offered(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A failure the conversation moved past is history, not a live offer.

    Re-running it now would be the agent answering a question two exchanges
    old, into a thread that has since gone somewhere else.
    """
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    later = await service.send_turn(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        conversation.id,
        text="never mind, what time is it?",
        client_turn_id=None,
        request_id=new_uuid7(),
        ip_hash="h",
    )
    later.task.state = TaskState.COMPLETED.value
    await session.commit()

    assert await offer(session, admin_ctx, conversation) is None


async def test_a_colleagues_failed_task_is_not_a_turn_to_offer_back(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A delegated task carries the chat's id so its answer lands here.

    That is not the same as being something a person typed, and offering to
    "send it again" would re-run somebody else's brief.
    """
    conversation, turn = await start(session, admin_ctx, temporal, agent)
    turn.task.state = TaskState.COMPLETED.value
    session.add(
        Task(
            workspace_id=admin_ctx.workspace_id,
            title="Check the repo",
            description="Check the repo",
            assigned_agent_id=agent.id,
            conversation_id=conversation.id,
            parent_task_id=turn.task.id,
            correlation_id=new_uuid7(),
            state=TaskState.FAILED.value,
            metadata_json={"origin": "delegation"},
        )
    )
    await session.commit()

    assert await offer(session, admin_ctx, conversation) is None


async def test_a_turn_with_no_words_left_is_no_control_rather_than_a_sad_one(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    turn.task.description = "   "
    await session.commit()

    assert await offer(session, admin_ctx, conversation) is None


async def test_a_paused_agent_is_named_instead_of_offering_a_dead_button(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    agent.status = AgentStatus.PAUSED.value
    await session.commit()

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "unavailable"
    assert resume_offer.reason == (
        "Bisby is paused by an admin, so this can't be picked up right now."
    )


async def test_an_archived_chat_says_what_to_restore(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    conversation.status = ConversationStatus.ARCHIVED.value
    await session.commit()

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "unavailable"
    assert "Restore it" in resume_offer.reason


# --- What must not be repeated -------------------------------------------


async def test_a_call_nobody_can_account_for_is_refused_and_explained(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """The push case: the platform cannot say the effect did not happen.

    The refusal names what the step was doing rather than the tool, and says
    what the person can do instead — the control must never quietly repeat a
    call that may already have reached the outside world.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session,
        admin_ctx,
        agent,
        run,
        "github.pull_request.create",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )

    resume_offer = await offer(session, admin_ctx, conversation)
    assert resume_offer is not None
    assert resume_offer.state == "blocked"
    assert "making a change in GitHub" in resume_offer.reason
    assert "Check how it turned out" in resume_offer.reason
    assert "tell Bisby what to do next" in resume_offer.reason
    assert resume_offer.unreconciled_tool_call_id is not None

    started = len(temporal.started)
    with pytest.raises(HTTPException) as refusal:
        await resume(session, admin_ctx, temporal, conversation)
    assert refusal.value.status_code == 409
    # Same sentence from the endpoint as from the offer: a control that said
    # "check first" must not answer in different words when it is pressed.
    assert refusal.value.detail == resume_offer.reason
    assert len(temporal.started) == started


async def test_a_dispatched_call_left_hanging_is_refused_too(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """``executing`` on a dead run is the same doubt, one step earlier.

    The worker died before anything reconciled the row. Nothing about it is
    more knowable than an ``execution_unknown`` row.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session, admin_ctx, agent, run, "cli.repository.push", ToolCallStatus.EXECUTING.value
    )

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None and resume_offer.state == "blocked"


async def test_a_read_only_step_left_unreconciled_is_not_a_reason_to_refuse(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """The operator's own failure, and the reason this is not a status check.

    A redeploy killed the worker mid-``cli.repository.checkout`` and left the
    row ``execution_unknown``. Every byte of that tool is a read — its
    definition says so, in the field recovery itself acts on — so there is
    nothing out there to have happened twice, and telling the person that
    trying again "could repeat it" is both false and the end of the road.

    The status set alone cannot see that: it is a sound proxy only for rows
    the current recovery path wrote, and this row was written by a process
    that died.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session,
        admin_ctx,
        agent,
        run,
        "cli.repository.checkout",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert resume_offer.unreconciled_tool_call_id is None

    # And the endpoint agrees with its own control: pressing it works.
    result = await resume(session, admin_ctx, temporal, conversation)
    assert result.created is True


async def test_the_offer_answers_the_doubt_the_failure_raised(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A safe button, said to be safe.

    The card above this offer says a step "was cut short before it could
    report back, so there is no record of whether it finished" — and then
    holds out "Try again". The reason that press is safe is that the step in
    doubt declares a repeat safe; without saying so the card raises a doubt,
    never resolves it, and a careful person hesitates over a control that is
    fine. (They are right to hesitate: the *other* branch refuses the press
    for exactly that doubt.)
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session,
        admin_ctx,
        agent,
        run,
        "cli.repository.checkout",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert "safe to run again" in resume_offer.reason
    assert "nothing can happen twice" in resume_offer.reason
    # The step is named as a phrase, never by its tool name.
    assert "reading the code" in resume_offer.reason
    assert "cli.repository.checkout" not in resume_offer.reason
    # And it still says the thing the offer is for.
    assert "nothing to retype" in resume_offer.reason


async def test_a_turn_with_nothing_in_doubt_says_nothing_about_repeating(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """Reassurance is for a doubt that was raised. A turn that failed with no
    unaccounted-for step never raised one, and answering it would invent it."""
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert "safe to run again" not in resume_offer.reason
    assert resume_offer.reason.endswith("there's nothing to retype.")


async def test_one_step_that_cannot_be_repeated_blocks_a_turn_full_of_reads(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """Repeatable calls are excused one at a time, not by majority.

    A turn is offered back only when *nothing* on it is unaccounted for, so a
    single push among the reads is still the whole answer.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session,
        admin_ctx,
        agent,
        run,
        "cli.repository.checkout",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )
    push = await called(
        session,
        admin_ctx,
        agent,
        run,
        "cli.repository.push",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )
    await called(
        session, admin_ctx, agent, run, "cli.file.read", ToolCallStatus.EXECUTION_UNKNOWN.value
    )

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "blocked"
    # The one call that blocks it, not the newest one on the run.
    assert resume_offer.unreconciled_tool_call_id == push.id


async def test_a_tool_the_registry_does_not_know_is_never_assumed_repeatable(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """No declaration is not the same as a declaration of yes.

    An MCP tool discovered per workspace, or a name from a connector that has
    since been removed, has said nothing about repeats. Recovery never
    guesses there, and neither does the offer.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session,
        admin_ctx,
        agent,
        run,
        "mcp.acme.invoice.send",
        ToolCallStatus.EXECUTION_UNKNOWN.value,
    )

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None and resume_offer.state == "blocked"


async def test_a_claimed_call_is_not_a_reason_to_refuse(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """``claimed`` is the gateway's proof that nothing was dispatched.

    It re-executes such a call itself, so a turn holding one is exactly as
    safe to send again as a turn holding none. Reading the same classification
    from this end is what keeps the two decisions from drifting apart.
    """
    conversation, _turn, run = await failed_turn(session, admin_ctx, temporal, agent)
    await called(
        session, admin_ctx, agent, run, "cli.repository.checkout", ToolCallStatus.CLAIMED.value
    )
    await called(session, admin_ctx, agent, run, "cli.file.read", ToolCallStatus.COMPLETED.value)
    await called(session, admin_ctx, agent, run, "web.fetch", ToolCallStatus.FAILED.value)

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None and resume_offer.state == "ready"


# --- Picking it up --------------------------------------------------------


async def test_resuming_starts_a_new_turn_carrying_the_same_message(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    result = await resume(session, admin_ctx, temporal, conversation)

    assert result.created is True
    assert result.resumed_task.id == turn.task.id
    assert result.task.id != turn.task.id
    assert result.task.description == TURN_TEXT
    assert result.task.conversation_id == conversation.id
    assert result.task.metadata_json[service.RESUME_OF_KEY] == str(turn.task.id)
    assert [wid for _, _, wid in temporal.started][-1] == f"task-{result.task.id}"

    # The failure keeps its row: somebody coming back tomorrow is entitled to
    # see that it happened.
    assert turn.task.state == TaskState.FAILED.value
    assert turn.task.metadata_json[service.RESUMED_BY_KEY] == str(result.task.id)

    audited = set(
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_id == conversation.id)
        )
    )
    assert "conversation.resumed" in audited


async def test_the_resumed_turn_is_shaped_exactly_like_a_chat_turn(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """The seed message moves onto the new episode, byte for byte.

    The agent worker decides the prompt shape by checking that this task's own
    transcript opens with the person's message and that it matches
    ``task.description`` (``_is_chat_turn``). A resumed turn that failed that
    check would get its question restated as a brief *ahead* of everything
    said earlier — the shape that has agents answering the previous question.
    """
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    result = await resume(session, admin_ctx, temporal, conversation)

    seed = await session.get(Message, turn.message.id)
    assert seed is not None
    assert seed.task_id == result.task.id
    assert seed.message_type == MessageType.TEXT.value
    assert str(seed.content_json["text"]).strip() == result.task.description.strip()


async def test_the_person_s_message_is_not_repeated_in_the_transcript(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """Moving it rather than copying it is what keeps the thread honest."""
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    await resume(session, admin_ctx, temporal, conversation)

    messages = await service.list_messages(session, admin_ctx.workspace_id, conversation.id)
    said = [
        m for m in messages if m.sender_type == SenderType.USER.value and "text" in m.content_json
    ]
    assert [str(m.content_json["text"]) for m in said] == [TURN_TEXT]
    assert turn.message.id in {m.id for m in messages}

    # And the thread says out loud that it is being tried again, so a second
    # reader is not left wondering why the agent answered twice.
    notes = [m for m in messages if m.content_json.get("kind") == "turn_resumed"]
    assert len(notes) == 1
    assert notes[0].task_id is None
    assert notes[0].conversation_id == conversation.id
    assert notes[0].content_json["text"] == f"Trying “{TURN_TEXT}” again."


async def test_pressing_it_twice_starts_one_run(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    first = await resume(session, admin_ctx, temporal, conversation)
    started = len(temporal.started)
    second = await resume(session, admin_ctx, temporal, conversation)

    assert second.task.id == first.task.id
    assert second.created is False
    assert len(temporal.started) == started
    tasks = await service._conversation_tasks(session, admin_ctx.workspace_id, conversation.id)
    assert len(tasks) == 2


async def test_a_press_that_lands_second_finds_the_successor_it_did_not_load(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    temporal: RecordingTemporal,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping presses, from the loser's point of view.

    The endpoint lists the conversation's tasks, then re-reads the failed one
    ``FOR UPDATE``. In a real double-press the second request lists them
    *before* the first commits — so its snapshot is a single failed turn — and
    then blocks on the lock until the winner's ``resumed_by_task_id`` is in
    the row. Everything after that point is decided from the locked read, and
    it only works if that read is actually read: the row is already in this
    session's identity map, and an ORM query that answers from there takes the
    lock in the database and discards the row it was granted. Both presses
    then miss the stamp, both create a successor, and both start a workflow.

    Both halves of the race are played here against the one session the test
    harness has: the winner's commit is a Core UPDATE left deliberately
    unsynchronized (what a concurrent commit looks like from inside a session
    — the database has moved on and the mapped instance has not), and the
    loser's earlier snapshot is the task list it captured before that.
    """
    conversation, turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    snapshot = await service._conversation_tasks(session, admin_ctx.workspace_id, conversation.id)
    assert [t.id for t in snapshot] == [turn.task.id]

    winner = Task(
        workspace_id=admin_ctx.workspace_id,
        title=turn.task.title,
        description=TURN_TEXT,
        assigned_agent_id=agent.id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
        metadata_json={
            "origin": "conversation",
            "conversation_id": str(conversation.id),
            service.RESUME_OF_KEY: str(turn.task.id),
        },
    )
    session.add(winner)
    await session.flush()
    await session.execute(
        update(Task)
        .where(Task.id == turn.task.id)
        .values(metadata_json={**turn.task.metadata_json, service.RESUMED_BY_KEY: str(winner.id)})
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    # The stamp is in the database and not on the instance: exactly the state
    # the losing press is about to take the lock from.
    assert service.RESUMED_BY_KEY not in turn.task.metadata_json

    async def snapshot_taken_before_the_winner_committed(*_: Any, **__: Any) -> list[Task]:
        return snapshot

    monkeypatch.setattr(service, "_conversation_tasks", snapshot_taken_before_the_winner_committed)

    started = len(temporal.started)
    result = await resume(session, admin_ctx, temporal, conversation)

    assert result.created is False
    assert result.task.id == winner.id
    assert result.resumed_task.id == turn.task.id
    assert len(temporal.started) == started
    monkeypatch.undo()
    after = await service._conversation_tasks(session, admin_ctx.workspace_id, conversation.id)
    assert len(after) == 2


async def test_the_offer_goes_away_once_it_has_been_taken(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    await resume(session, admin_ctx, temporal, conversation)

    assert await offer(session, admin_ctx, conversation) is None


async def test_a_resumed_turn_that_fails_again_is_offered_again(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """Nothing is ever left with no way forward, however many attempts it takes."""
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    first = await resume(session, admin_ctx, temporal, conversation)
    first.task.state = TaskState.FAILED.value
    await session.commit()

    resume_offer = await offer(session, admin_ctx, conversation)

    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert resume_offer.task_id == first.task.id
    assert resume_offer.instruction == TURN_TEXT


async def test_resuming_a_chat_with_nothing_wrong_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn = await start(session, admin_ctx, temporal, agent)

    with pytest.raises(HTTPException) as refusal:
        await resume(session, admin_ctx, temporal, conversation)

    assert refusal.value.status_code == 409
    assert "nothing to pick up" in str(refusal.value.detail)


async def test_resuming_someone_else_s_chat_is_refused_for_a_member(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A chat is a person's own workspace, and so is retrying inside one."""
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)
    colleague = User(
        email=f"m-{new_uuid7().hex[:8]}@example.com", display_name="M", password_hash="x"
    )
    session.add(colleague)
    await session.flush()
    member = WorkspaceContext(
        user=colleague, workspace_id=admin_ctx.workspace_id, role=WorkspaceRole.MEMBER
    )

    with pytest.raises(HTTPException) as refusal:
        await resume(session, member, temporal, conversation)

    assert refusal.value.status_code == 403


# --- What the failure itself says ----------------------------------------


async def test_the_failure_row_is_projected_in_the_products_own_voice(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    messages = await service.list_messages(session, admin_ctx.workspace_id, conversation.id)
    projected = await service.project_messages(session, admin_ctx.workspace_id, messages)
    [failure] = [m for m in projected if m.failure is not None]

    assert failure.failure is not None
    notice = failure.failure
    assert notice.code == "tool_execution_unknown"
    assert notice.summary.startswith("A step was cut short")
    assert "reconciliation" not in notice.summary
    # The identifier is still there for support — it just stops leading.
    assert UUID(notice.reference)
    assert notice.reference not in notice.summary
    # And the row itself is untouched: it is the record.
    assert str(failure.content_json["text"]).startswith("Run failed: tool call ")


async def test_the_card_quotes_the_provider_and_not_the_run_s_status_line(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """Where the failure's own words come from.

    The transcript row is written as ``f"Run {status}: {message}"``, so a
    notice built from it quoted the run's status line back at a person under
    a heading that already said the agent could not finish: "Run failed:
    openai: HTTP 429…" — three statements of failure, one of them internal.
    The run's ``error_message`` is the failure's own words, and it is the
    column the activity feed has always read.
    """
    conversation, turn, run = await failed_turn(
        session, admin_ctx, temporal, agent, error_code="step_failed"
    )
    run.error_message = "openai: HTTP 429 rate limit exceeded, retry after 20s"
    session.add(run)
    await session.execute(
        update(Message)
        .where(Message.task_id == turn.task.id, Message.message_type == MessageType.ERROR.value)
        .values(
            content_json={
                "text": f"Run failed: {run.error_message}",
                "error_code": "step_failed",
            }
        )
    )
    await session.commit()

    messages = await service.list_messages(session, admin_ctx.workspace_id, conversation.id)
    projected = await service.project_messages(session, admin_ctx.workspace_id, messages)
    [failure] = [m for m in projected if m.failure is not None]

    assert failure.failure is not None
    assert failure.failure.detail == "openai: HTTP 429 rate limit exceeded, retry after 20s"
    assert "Run failed" not in failure.failure.detail
    # The row keeps its own text: that is the record.
    assert str(failure.content_json["text"]).startswith("Run failed: ")


async def test_a_failure_with_no_run_behind_it_still_drops_the_status_line(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A turn that never reached the agent leaves an ``error`` row and no run
    at all, so the row's text is all there is — and the framing still comes
    off it."""
    conversation, turn = await start(session, admin_ctx, temporal, agent)
    session.add(
        Message(
            workspace_id=admin_ctx.workspace_id,
            task_id=turn.task.id,
            run_id=None,
            conversation_id=conversation.id,
            sender_type=SenderType.SYSTEM.value,
            recipient_type="task",
            recipient_id=turn.task.id,
            message_type=MessageType.ERROR.value,
            content_json={"text": "Run failed: the sky fell in", "error_code": "step_failed"},
        )
    )
    await session.commit()

    messages = await service.list_messages(session, admin_ctx.workspace_id, conversation.id)
    projected = await service.project_messages(session, admin_ctx.workspace_id, messages)
    [failure] = [m for m in projected if m.failure is not None]

    assert failure.failure is not None
    assert failure.failure.detail == "the sky fell in"


async def test_an_ordinary_message_carries_no_failure(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, turn = await start(session, admin_ctx, temporal, agent)

    projected = await service.project_messages(session, admin_ctx.workspace_id, [turn.message])

    assert projected[0].failure is None
    assert conversation.id is not None


async def test_the_chat_list_says_the_failure_the_way_the_card_does(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """The preview is the widest surface a failure has.

    The rail, the agent page and the attention inbox all show this one
    string, so the sentence the card was written to replace was still the
    first thing on screen — clipped at 160 characters, identifier first.
    """
    conversation, _turn, _run = await failed_turn(session, admin_ctx, temporal, agent)

    [row] = await service.project_conversations(session, admin_ctx.workspace_id, [conversation])

    assert row.last_message_preview == (
        "A step was cut short before it could report back, so there is no record "
        "of whether it finished."
    )
    assert row.last_message_sender_type == "system"


async def test_an_ordinary_preview_is_still_the_words_that_were_said(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn = await start(session, admin_ctx, temporal, agent)

    [row] = await service.project_conversations(session, admin_ctx.workspace_id, [conversation])

    assert row.last_message_preview == TURN_TEXT


# --- When it stopped before it started ------------------------------------


class UnreachableTemporal(RecordingTemporal):
    """Temporal during a redeploy: the client cannot reach a server."""

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str) -> None:
        raise OSError("connection refused")


async def test_a_turn_that_never_reached_an_agent_says_so_in_the_chat(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    """A redeploy between the commit and the start, which is the same event
    that caused the incident — one beat earlier.

    The task is marked failed and the caller gets 503, but the caller is a
    browser request that is already gone. Without a row in the transcript the
    person sees their own message, no card, no button, and an agent that
    looks like it is still thinking.
    """
    conversation, _turn = await service.create_conversation(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        agent_id=agent.id,
        title=None,
        text=None,
        client_turn_id=None,
        request_id=new_uuid7(),
        ip_hash="h",
    )

    with pytest.raises(HTTPException) as refusal:
        await service.send_turn(
            session,
            admin_ctx,
            UnreachableTemporal(),  # type: ignore[arg-type]
            conversation.id,
            text=TURN_TEXT,
            client_turn_id=None,
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert refusal.value.status_code == 503

    messages = await service.list_messages(session, admin_ctx.workspace_id, conversation.id)
    projected = await service.project_messages(session, admin_ctx.workspace_id, messages)
    [failure] = [m for m in projected if m.failure is not None]

    assert failure.failure is not None
    assert failure.failure.code == "workflow_start_failed"
    assert failure.failure.summary == "This never reached the agent, so nothing has run yet."
    # Jhin wrote the text itself, so repeating it under the summary would only
    # put "Temporal" back on somebody's screen.
    assert failure.failure.detail == ""

    # And the way out is on the same card: the offer names the very turn the
    # failure belongs to, which is how the button lands on it.
    resume_offer = await offer(session, admin_ctx, conversation)
    assert resume_offer is not None
    assert resume_offer.state == "ready"
    assert resume_offer.task_id == failure.task_id
    assert resume_offer.instruction == TURN_TEXT


async def test_the_list_preview_shows_that_failure_too(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: RecordingTemporal, agent: Agent
) -> None:
    conversation, _turn = await service.create_conversation(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        agent_id=agent.id,
        title=None,
        text=None,
        client_turn_id=None,
        request_id=new_uuid7(),
        ip_hash="h",
    )
    with pytest.raises(HTTPException):
        await service.send_turn(
            session,
            admin_ctx,
            UnreachableTemporal(),  # type: ignore[arg-type]
            conversation.id,
            text=TURN_TEXT,
            client_turn_id=None,
            request_id=new_uuid7(),
            ip_hash="h",
        )

    [row] = await service.project_conversations(session, admin_ctx.workspace_id, [conversation])

    assert row.last_message_preview == "This never reached the agent, so nothing has run yet."


async def test_work_with_no_conversation_gets_no_transcript_row(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """A task created straight through the API has no thread to say it in,
    and its caller already has the 503."""
    task = Task(
        workspace_id=admin_ctx.workspace_id,
        title="Nightly report",
        description="Nightly report",
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()

    tasks_service.record_start_failure(session, task)
    await session.commit()

    rows = list(
        await session.scalars(select(Message).where(Message.workspace_id == admin_ctx.workspace_id))
    )
    assert rows == []
