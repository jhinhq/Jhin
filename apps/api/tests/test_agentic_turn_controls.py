from unittest.mock import AsyncMock

import pytest
from apps.api.tests.test_conversations_unit import FakeTemporal
from fastapi import HTTPException
from sqlalchemy import select

from jhin_api.conversations import service
from jhin_api.conversations.schemas import TurnIn
from jhin_db.models import Agent, AgentRun, ChatProject, Conversation, FileCheckpoint, Message, Task
from jhin_domain import new_uuid7


async def seeded(session, ctx, mode="act"):
    agent = Agent(workspace_id=ctx.workspace_id, name="Bisby", slug="agentic-turn")
    session.add(agent)
    await session.flush()
    temporal = FakeTemporal()
    chat, turn = await service.create_conversation(
        session,
        ctx,
        temporal,
        agent_id=agent.id,
        title=None,
        text="Initial work",
        client_turn_id="first",
        request_id=new_uuid7(),
        ip_hash="test",
        execution_mode=mode,
    )
    return chat, turn, temporal


async def send(session, ctx, temporal, chat, **kwargs):
    return await service.send_turn(
        session,
        ctx,
        temporal,
        chat.id,
        text=kwargs.pop("text", "Next work"),
        client_turn_id=kwargs.pop("client_turn_id", None),
        request_id=new_uuid7(),
        ip_hash="test",
        **kwargs,
    )


async def test_queue_preserves_modes_fifo_and_idempotency(session, admin_ctx):
    chat, first, temporal = await seeded(session, admin_ctx, "plan")
    first.task.state = "running"
    await session.commit()
    second = await send(
        session,
        admin_ctx,
        temporal,
        chat,
        execution_mode="act",
        delivery="queue",
        client_turn_id="second",
    )
    third = await send(session, admin_ctx, temporal, chat, delivery="queue")
    duplicate = await send(
        session,
        admin_ctx,
        temporal,
        chat,
        execution_mode="act",
        delivery="queue",
        client_turn_id="second",
    )
    assert second.task.id == duplicate.task.id
    assert second.task.metadata_json["queue_after_task_id"] == str(first.task.id)
    assert third.task.metadata_json["queue_after_task_id"] == str(second.task.id)
    assert first.task.metadata_json["execution_mode"] == "plan"
    assert len(temporal.started) == 3
    with pytest.raises(HTTPException) as error:
        await send(session, admin_ctx, temporal, chat, execution_mode="act", delivery="steer")
    assert error.value.status_code == 409


async def test_queued_edit_and_remove_refuse_started_work(session, admin_ctx):
    chat, first, temporal = await seeded(session, admin_ctx)
    queued = await send(session, admin_ctx, temporal, chat, delivery="queue")
    result = await service.change_queued(
        session, admin_ctx, chat.id, queued.task.id, text="Changed request"
    )
    assert result["text"] == "Changed request"
    await session.refresh(queued.message)
    assert queued.message.content_json["text"] == "Changed request"
    session.add(
        AgentRun(
            workspace_id=admin_ctx.workspace_id,
            agent_id=chat.primary_agent_id,
            task_id=first.task.id,
        )
    )
    await session.commit()
    with pytest.raises(HTTPException) as error:
        await service.change_queued(session, admin_ctx, chat.id, first.task.id, text=None)
    assert error.value.status_code == 409
    cancelled = await service.change_queued(session, admin_ctx, chat.id, queued.task.id, text=None)
    assert cancelled["status"] == "cancelled"


async def test_file_reference_errors_are_scoped_and_blank_file_turn_is_accepted(session, admin_ctx):
    chat, _, temporal = await seeded(session, admin_ctx)
    assert TurnIn(attachment_ids=[new_uuid7()]).text == ""
    with pytest.raises(HTTPException) as error:
        await send(session, admin_ctx, temporal, chat, attachment_ids=[new_uuid7()])
    assert error.value.status_code == 404


async def test_saved_project_context_is_pinned_to_turn_without_mutating_agent(session, admin_ctx):
    chat, first, temporal = await seeded(session, admin_ctx)
    project = ChatProject(
        workspace_id=admin_ctx.workspace_id, name="Launch", context="Use the local analysis data"
    )
    session.add(project)
    await session.flush()
    chat.project_id = project.id
    first.task.state = "completed"
    await session.commit()
    turn = await send(session, admin_ctx, temporal, chat)
    assert turn.task.metadata_json["context_refs"][0]["context"] == project.context
    assert turn.task.metadata_json["context_refs"][0]["id"] == str(project.id)
    with pytest.raises(HTTPException):
        await send(
            session,
            admin_ctx,
            temporal,
            chat,
            context_refs=[{"type": "agent", "id": str(new_uuid7())}],
        )


async def test_branch_copies_recorded_history_and_checkpoint_without_replaying_actions(
    session, admin_ctx, monkeypatch, tmp_path
):
    import jhin_api.runtime.service as runtime
    from jhin_db.models import AuditEvent, ManagedFile
    from jhin_media.managed_files import pin_attachments, publish_file

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    seed_workspace = AsyncMock()
    monkeypatch.setattr(runtime, "seed_branch_workspace", seed_workspace, raising=False)
    chat, first, _ = await seeded(session, admin_ctx)
    original_file = await publish_file(
        session, admin_ctx.workspace_id, chat.id, "source.csv", b"name,value\nx,1\n", kind="upload"
    )
    original_refs = await pin_attachments(
        session, admin_ctx.workspace_id, chat.id, [original_file.id]
    )
    first.message.content_json = {**first.message.content_json, "attachments": original_refs}
    checkpoint = FileCheckpoint(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=chat.id,
        label="Source",
        manifest_json={},
        excluded_json=[],
    )
    session.add(checkpoint)
    await session.commit()
    result = await service.branch(
        session,
        admin_ctx,
        chat.id,
        message_id=first.message.id,
        checkpoint_id=checkpoint.id,
        title=None,
    )
    from uuid import UUID

    target = await session.get(Conversation, UUID(result["conversation_id"]))
    assert (
        target.source_message_id == first.message.id
        and target.source_checkpoint_id == checkpoint.id
    )
    copied = list(
        await session.scalars(select(Message).where(Message.conversation_id == target.id))
    )
    assert len(copied) == 1 and copied[0].task_id is None
    assert copied[0].content_json["branched_from_message_id"] == str(first.message.id)
    assert list(await session.scalars(select(Task).where(Task.conversation_id == target.id))) == []
    attachment = copied[0].content_json["attachments"][0]
    assert attachment["id"] != str(original_file.id)
    inherited = await session.get(ManagedFile, UUID(attachment["id"]))
    assert inherited.conversation_id == target.id
    seed_workspace.assert_awaited_once()
    receipts = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == admin_ctx.workspace_id,
                AuditEvent.target_type == "conversation",
                AuditEvent.target_id == target.id,
                AuditEvent.action == "chat.project.seeded",
            )
        )
    )
    assert len(receipts) == 1
    assert receipts[0].actor_id == admin_ctx.user.id
    assert receipts[0].metadata_json == {
        "source_kind": "branch",
        "source_checkpoint_id": str(checkpoint.id),
    }
