"""Real PostgreSQL ordering/rollback checks; use only an isolated test database."""

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    ConversationEvent,
    Message,
    SandboxJob,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import new_uuid7

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated TEST_DATABASE_URL required")


@pytest.fixture
async def journal_db():
    engine = create_async_engine(URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        workspace = Workspace(name="Journal test", slug=f"journal-{new_uuid7().hex}")
        session.add(workspace)
        await session.flush()
        chat = Conversation(
            workspace_id=workspace.id, title="Journal", last_activity_at=datetime.now(UTC)
        )
        session.add(chat)
        await session.commit()
    yield factory, workspace.id, chat.id
    async with factory() as session:
        await session.execute(delete(Workspace).where(Workspace.id == workspace.id))
        await session.commit()
    await engine.dispose()


def message(workspace, chat, text):
    return Message(
        workspace_id=workspace,
        conversation_id=chat,
        sender_type="user",
        recipient_type="agent",
        content_json={"text": text},
        visibility="visible",
    )


async def test_concurrent_writers_replay_commit_order_and_rollback_does_not_leave_gap(journal_db):
    factory, workspace, chat = journal_db
    async with factory() as first:
        first.add(message(workspace, chat, "first"))
        await first.flush()
        async with factory() as reader:
            assert (
                list(
                    await reader.scalars(
                        select(ConversationEvent).where(ConversationEvent.conversation_id == chat)
                    )
                )
                == []
            )
        entered = asyncio.Event()

        async def competing():
            async with factory() as second:
                entered.set()
                second.add(message(workspace, chat, "second"))
                await second.commit()

        future = asyncio.create_task(competing())
        await entered.wait()
        await first.commit()
        await future
    async with factory() as rolled_back:
        rolled_back.add(message(workspace, chat, "discard"))
        await rolled_back.flush()
        await rolled_back.rollback()
    async with factory() as last:
        last.add(message(workspace, chat, "third"))
        await last.commit()
        events = list(
            await last.scalars(
                select(ConversationEvent)
                .where(ConversationEvent.conversation_id == chat)
                .order_by(ConversationEvent.sequence)
            )
        )
        assert [row.sequence for row in events] == [1, 2, 3]
        assert [row.payload_json["content_json"]["text"] for row in events] == [
            "first",
            "second",
            "third",
        ]


async def test_job_progress_is_bound_to_cli_call_and_final_call_keeps_output(journal_db):
    factory, workspace, chat = journal_db
    async with factory() as session:
        agent = Agent(workspace_id=workspace, name="Agent", slug="journal-agent")
        task = Task(
            workspace_id=workspace, conversation_id=chat, title="Job", correlation_id=new_uuid7()
        )
        session.add_all([agent, task])
        await session.flush()
        run = AgentRun(workspace_id=workspace, agent_id=agent.id, task_id=task.id)
        session.add(run)
        await session.flush()
        call = ToolCall(
            workspace_id=workspace,
            agent_id=agent.id,
            run_id=run.id,
            tool_name="cli.command.execute",
            status="executing",
        )
        session.add(call)
        await session.flush()
        job = SandboxJob(
            workspace_id=workspace,
            run_id=run.id,
            task_id=task.id,
            tool_call_id=call.id,
            image="test",
            stdout_tail="first",
        )
        session.add(job)
        await session.flush()
        job.stdout_tail = "first\nlast"
        job.exit_code = 0
        job.status = "completed"
        await session.flush()
        call.status = "completed"
        await session.commit()
        events = list(
            await session.scalars(
                select(ConversationEvent)
                .where(
                    ConversationEvent.conversation_id == chat,
                    ConversationEvent.source_kind == "tool_call",
                )
                .order_by(ConversationEvent.sequence)
            )
        )
        assert events[-1].payload_json["sandbox_job"]["stdout"] == "first\nlast"
        assert events[-1].payload_json["sandbox_job"]["exit_code"] == 0
        assert all(event.source_id == call.id for event in events)


async def test_executing_gateway_allows_independent_workspace_transaction(journal_db):
    from pydantic import BaseModel

    from jhin_policy import RiskLevel, ToolDefinition
    from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
    from jhin_tools.gateway import ToolGateway

    class Input(BaseModel):
        pass

    class Output(BaseModel):
        executed: bool

    factory, workspace, chat = journal_db
    async with factory() as db:
        agent = Agent(workspace_id=workspace, name="Agent", slug="gateway-agent")
        task = Task(
            workspace_id=workspace, conversation_id=chat, title="Job", correlation_id=new_uuid7()
        )
        db.add_all([agent, task])
        await db.flush()
        run = AgentRun(workspace_id=workspace, agent_id=agent.id, task_id=task.id)
        db.add(run)
        await db.flush()
        call = ToolCall(
            workspace_id=workspace,
            agent_id=agent.id,
            run_id=run.id,
            tool_name="test.workspace",
            status="claimed",
        )
        db.add(call)
        await db.commit()
        context = ToolExecutionContext(
            session=db,
            session_factory=factory,
            workspace_id=workspace,
            task_id=task.id,
            run_id=run.id,
            agent_id=agent.id,
            agent_name=agent.name,
        )

        async def executor(ctx, payload):
            # Connector reads trigger autoflush. They must not leave an
            # uncommitted journal update blocking the separate disk lease.
            await ctx.session.scalar(select(Workspace).where(Workspace.id == workspace))
            async with factory() as independent:
                await independent.scalar(
                    select(Conversation).where(Conversation.id == chat).with_for_update(nowait=True)
                )
                await independent.commit()
            return Output(executed=True)

        definition = ToolDefinition(
            name="test.workspace",
            description="Journal lease regression",
            risk=RiskLevel.READ,
            input_model=Input,
            output_model=Output,
            required_capability="test.workspace",
        )
        catalog = ToolCatalog()
        catalog.register(definition, executor)
        outcome = await ToolGateway(context, catalog)._run_executor(
            definition,
            call,
            Input(),
            executor=executor,
            session=db,
            commit_terminal=True,
            authorized_by=[],
        )
        assert outcome.status == "executed"
