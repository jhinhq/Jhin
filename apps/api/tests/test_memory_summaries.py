from datetime import UTC, datetime

import pytest
from fastapi.encoders import jsonable_encoder

from jhin_api.memory.router import get_memory, list_memories
from jhin_api.memory.summary_router import build_summary, rebuild, summary
from jhin_db.models import Agent, Conversation, MemoryRecord, Message, Task
from jhin_domain import MemoryScope, new_uuid7
from jhin_memory.policy import content_hash


async def test_summary_is_supported_versioned_and_immediately_current(session, admin_ctx):
    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    session.add(agent)
    await session.flush()

    def memory(text, actor):
        return MemoryRecord(
            workspace_id=admin_ctx.workspace_id,
            scope="agent",
            scope_id=agent.id,
            kind="preference",
            content=text,
            content_hash=content_hash(text),
            visibility="agent",
            status="active",
            created_by_type=actor,
        )

    supported = memory(
        "Draft daily at 09:00 America/Los_Angeles; director publishes after review.", "user"
    )
    unsupported = memory("Ghost is automatically publishing.", "agent")
    session.add_all([supported, unsupported])
    await session.flush()
    first = await build_summary(session, admin_ctx.workspace_id, MemoryScope.AGENT, agent.id)
    assert first.coverage_count == 1 and first.source_count == 1
    assert "Ghost is automatically" not in first.summary
    supported.status = "superseded"
    replacement = memory(
        "Draft daily at 10:00 America/Los_Angeles; director publishes after review.", "user"
    )
    session.add(replacement)
    await session.flush()
    second = await build_summary(session, admin_ctx.workspace_id, MemoryScope.AGENT, agent.id)
    assert second.version != first.version and not second.stale
    assert "10:00" in second.summary and "09:00" not in second.summary


@pytest.mark.parametrize("endpoint", ["list", "detail", "summary", "rebuild"])
async def test_legacy_human_memory_is_redacted_only_on_public_serialization(
    session, admin_ctx, endpoint
):
    """Legacy human evidence stays eligible, but never sends old credentials to clients."""
    ghost_key = "a" * 24 + ":" + "b" * 64  # Synthetic; never an actual provider credential.
    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    session.add(agent)
    await session.flush()
    conversation = Conversation(
        workspace_id=admin_ctx.workspace_id,
        title="Legacy setup",
        primary_agent_id=agent.id,
        last_activity_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    task = Task(
        workspace_id=admin_ctx.workspace_id,
        title="Remember setup",
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    message = Message(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=conversation.id,
        task_id=task.id,
        sender_type="user",
        sender_id=admin_ctx.user.id,
        recipient_type="agent",
        recipient_id=agent.id,
        content_json={"text": "Remember the confirmed setup."},
    )
    session.add(message)
    await session.flush()
    event_id = new_uuid7()
    original_content = f"Ghost setup uses {ghost_key}; drafts need review."
    original_policy = {
        "legacy_note": {ghost_key: [f"Old key: {ghost_key}"]},
        "source_id": str(message.id),
    }
    record = MemoryRecord(
        workspace_id=admin_ctx.workspace_id,
        scope="agent",
        scope_id=agent.id,
        kind="fact",
        subject=f"Ghost {ghost_key}",
        content=original_content,
        content_hash=content_hash(original_content),
        tags_json=["ghost", ghost_key],
        policy_json=original_policy,
        visibility="agent",
        status="active",
        created_by_type="user",
        created_by_id=admin_ctx.user.id,
        source_conversation_id=conversation.id,
        source_message_id=message.id,
        source_task_id=task.id,
        source_event_id=event_id,
    )
    session.add(record)
    await session.commit()

    if endpoint == "list":
        result = await list_memories(admin_ctx, session, agent_id=agent.id)
    elif endpoint == "detail":
        result = await get_memory(record.id, admin_ctx, session)
    elif endpoint == "summary":
        result = await summary(admin_ctx, session, MemoryScope.AGENT, agent.id)
    else:
        result = await rebuild(admin_ctx, session, MemoryScope.AGENT, agent.id)
    public = jsonable_encoder(result)
    assert ghost_key not in result.model_dump_json()
    assert "b" * 64 not in result.model_dump_json()
    item = public if endpoint == "detail" else public["items"][0]
    assert "REDACTED legacy credential" in item["content"]
    assert "drafts need review" in item["content"]
    assert item["id"] == str(record.id)
    assert item["version"] == record.version
    assert item["source_conversation_id"] == str(conversation.id)
    assert item["source_message_id"] == str(message.id)
    assert item["source_task_id"] == str(task.id)
    if endpoint in {"list", "detail"}:
        assert item["source_event_id"] == str(event_id)
        assert item["evidence_status"] == "supported"
        assert item["tags_json"][0] == "ghost"
        assert "REDACTED legacy credential" in item["tags_json"][1]
        assert "REDACTED legacy credential" in item["subject"]
        assert item["policy_json"]["source_id"] == str(message.id)
        assert "REDACTED legacy credential" in next(iter(item["policy_json"]["legacy_note"]))
    else:
        assert public["source_count"] == 1
        assert "REDACTED legacy credential" in public["summary"]

    await session.refresh(record)
    assert record.content == original_content
    assert record.content_hash == content_hash(original_content)
    assert record.subject == f"Ghost {ghost_key}"
    assert record.tags_json == ["ghost", ghost_key]
    assert record.policy_json == original_policy
