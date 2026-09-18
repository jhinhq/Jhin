"""Terminal transcript projections use durable, workspace-bound execution facts."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.route_scopes import required_scope
from jhin_api.conversations import service
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AgentRun, Conversation, SandboxJob, Task, ToolCall
from jhin_domain import new_uuid7

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


async def seed(session: AsyncSession, ctx: WorkspaceContext):
    agent = Agent(workspace_id=ctx.workspace_id, name="Bisby", slug="bisby", role_title="Engineer")
    chat = Conversation(workspace_id=ctx.workspace_id, title="Terminal", last_activity_at=NOW)
    session.add_all([agent, chat])
    await session.flush()
    task = Task(
        workspace_id=ctx.workspace_id,
        title="Run command",
        correlation_id=new_uuid7(),
        conversation_id=chat.id,
    )
    session.add(task)
    await session.flush()
    run = AgentRun(workspace_id=ctx.workspace_id, agent_id=agent.id, task_id=task.id)
    session.add(run)
    await session.flush()
    call = ToolCall(
        workspace_id=ctx.workspace_id,
        agent_id=agent.id,
        run_id=run.id,
        tool_name="cli.command.execute",
        status="executing",
        created_at=NOW,
        sanitized_input_json={"input": {"command": "printf first; sleep 5"}},
    )
    session.add(call)
    await session.flush()
    return chat, task, run, call


async def test_running_output_updates_and_completed_history_survives_new_run(
    session: AsyncSession, admin_ctx: WorkspaceContext
):
    chat, task, run, call = await seed(session, admin_ctx)
    job = SandboxJob(
        workspace_id=admin_ctx.workspace_id,
        run_id=run.id,
        task_id=task.id,
        tool_call_id=call.id,
        image="python:3.12",
        status="running",
        stdout_tail="first\n",
        started_at=NOW,
    )
    session.add(job)
    await session.flush()
    result = (await service.list_tool_calls(session, admin_ctx.workspace_id, chat.id)).items
    assert len(result) == 1
    assert result[0].agent_name == "Bisby"
    assert result[0].task_id == task.id
    assert result[0].sandbox_job.stdout == "first\n"
    assert result[0].sandbox_job.status == "running"
    job.stdout_tail = "first\nsecond\n"
    job.exit_code = 0
    job.status = "completed"
    call.status = "completed"
    second_run = AgentRun(
        workspace_id=admin_ctx.workspace_id, agent_id=run.agent_id, task_id=task.id
    )
    session.add(second_run)
    await session.flush()
    second = ToolCall(
        workspace_id=admin_ctx.workspace_id,
        agent_id=run.agent_id,
        run_id=second_run.id,
        tool_name="cli.file.read",
        status="failed",
        created_at=NOW + timedelta(seconds=10),
    )
    session.add(second)
    await session.commit()
    result = (await service.list_tool_calls(session, admin_ctx.workspace_id, chat.id)).items
    assert [item.id for item in result] == [call.id, second.id]
    assert result[0].sandbox_job.stdout == "first\nsecond\n"
    assert result[0].sandbox_job.exit_code == 0


async def test_latest_bound_job_only_and_wrong_workspace_denied(
    session: AsyncSession, admin_ctx: WorkspaceContext
):
    chat, task, run, call = await seed(session, admin_ctx)
    other_run = AgentRun(
        workspace_id=admin_ctx.workspace_id, agent_id=run.agent_id, task_id=task.id
    )
    session.add(other_run)
    await session.flush()
    for run_id, output, seconds in [
        (run.id, "old", 0),
        (run.id, "new" + "x" * 9000, 1),
        (other_run.id, "wrong run", 2),
    ]:
        session.add(
            SandboxJob(
                workspace_id=admin_ctx.workspace_id,
                run_id=run_id,
                tool_call_id=call.id,
                image="python:3.12",
                stdout_tail=output,
                created_at=NOW + timedelta(seconds=seconds),
            )
        )
    session.add(
        ToolCall(
            workspace_id=admin_ctx.workspace_id,
            run_id=run.id,
            agent_id=run.agent_id,
            tool_name="organization.memory.write",
            status="executed",
        )
    )
    await session.flush()
    result = (await service.list_tool_calls(session, admin_ctx.workspace_id, chat.id)).items
    assert len(result) == 2
    terminal = next(item for item in result if item.id == call.id)
    assert terminal.sandbox_job.stdout == "x" * 8192
    with pytest.raises(HTTPException) as error:
        await service.list_tool_calls(session, new_uuid7(), chat.id)
    assert error.value.status_code == 404


async def test_history_limit_is_explicit_and_file_wrapper_logs_are_private(
    session: AsyncSession, admin_ctx: WorkspaceContext
):
    chat, _task, run, call = await seed(session, admin_ctx)
    call.tool_name = "cli.file.read"
    session.add(
        SandboxJob(
            workspace_id=admin_ctx.workspace_id,
            run_id=run.id,
            tool_call_id=call.id,
            image="python:3.12",
            stdout_tail="private wrapper evidence trailer",
        )
    )
    await session.flush()
    first = await service.list_tool_calls(session, admin_ctx.workspace_id, chat.id)
    assert first.items[0].sandbox_job is None
    assert "private wrapper" not in first.model_dump_json()
    for index in range(101):
        session.add(
            ToolCall(
                workspace_id=admin_ctx.workspace_id,
                run_id=run.id,
                agent_id=run.agent_id,
                tool_name="cli.command.execute",
                status="completed",
                created_at=NOW + timedelta(seconds=index + 1),
            )
        )
    await session.flush()
    recent = await service.list_tool_calls(session, admin_ctx.workspace_id, chat.id)
    assert recent.limit == 100
    assert recent.has_more is True
    assert len(recent.items) == 100
    assert call.id not in {item.id for item in recent.items}
    assert [item.created_at for item in recent.items] == sorted(
        item.created_at for item in recent.items
    )


def test_terminal_history_uses_conversation_read_scope():
    assert (
        required_scope(
            "GET", "/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/tool-calls"
        )
        == "chats:read"
    )
