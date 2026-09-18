"""Committed sequence replay and public item boundaries."""

from datetime import UTC, datetime

import pytest

from jhin_api.conversations import timeline
from jhin_domain import new_uuid7


def test_hidden_messages_and_private_file_wrappers_never_become_items():
    assert timeline.public_data("message", {"visibility": "internal"}) is None
    assert (
        timeline.public_data("sandbox_job", {"command": "private", "tool_name": "cli.file.read"})
        is None
    )


def test_projection_allowlists_fields_instead_of_exposing_provider_payloads():
    data = timeline.public_data(
        "generation",
        {"text": "Visible", "status": "running", "provider_request": {"secret": "private"}},
    )
    assert data == {"text": "Visible", "status": "running"}


def test_generation_snapshot_is_replacement_not_delta():
    from jhin_db.models.timeline import ConversationEvent

    event = ConversationEvent(
        workspace_id=new_uuid7(),
        conversation_id=new_uuid7(),
        sequence=7,
        source_kind="generation",
        source_id=new_uuid7(),
        operation="upsert",
        payload_json={"text": "Hello", "status": "running", "agent_id": str(new_uuid7())},
        created_at=datetime.now(UTC),
    )
    item = timeline.project_event(event)
    assert item.kind == "generation"
    assert item.revision == 7
    assert item.data["text"] == "Hello"
    assert item.id.startswith("generation:")


@pytest.mark.parametrize("operation,visibility", [("delete", "visible"), ("upsert", "internal")])
def test_removed_agent_reply_keeps_only_the_identity_needed_to_withdraw_its_generation(
    operation, visibility
):
    from jhin_db.models.timeline import ConversationEvent

    actor, task, run = new_uuid7(), new_uuid7(), new_uuid7()
    original_time = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    event = ConversationEvent(
        workspace_id=new_uuid7(),
        conversation_id=new_uuid7(),
        sequence=10,
        source_kind="message",
        source_id=new_uuid7(),
        operation=operation,
        payload_json={
            "sender_type": "agent",
            "sender_id": str(actor),
            "task_id": str(task),
            "run_id": str(run),
            "created_at": original_time.isoformat(),
            "message_type": "text",
            "visibility": visibility,
            "content_json": {"text": "withdrawn private content"},
            "provider_request": {"secret": "must never appear"},
        },
        created_at=datetime.now(UTC),
    )
    item = timeline.project_event(event)
    assert item.status == "removed"
    assert item.actor.type == "agent" and item.actor.id == actor
    assert item.task_id == task and item.run_id == run
    assert item.created_at == original_time
    assert item.data == {"message_type": "text"}
    assert "withdrawn private content" not in item.model_dump_json()
    assert "must never appear" not in item.model_dump_json()


@pytest.mark.parametrize("human", [False, True])
def test_work_review_uses_real_author_and_decision_fields(human):
    from jhin_db.models.timeline import ConversationEvent

    author, reviewer = new_uuid7(), new_uuid7()
    payload = {
        "subject_agent_id": str(author),
        "reviewer_agent_id": str(reviewer),
        "status": "pending",
        "feedback": "",
        "verdict": None,
        "evidence_json": {"private": "must not appear"},
    }
    event = ConversationEvent(
        workspace_id=new_uuid7(),
        conversation_id=new_uuid7(),
        sequence=1,
        source_kind="work_review",
        source_id=new_uuid7(),
        operation="upsert",
        payload_json=payload,
        created_at=datetime.now(UTC),
    )
    item = timeline.project_event(event)
    assert item.actor.type == "agent" and item.actor.id == author
    decided = {
        **payload,
        "status": "approved",
        "verdict": "approve",
        "feedback": "Ready to publish",
        "decided_by_user_id" if human else "decided_by_agent_id": str(reviewer),
    }
    event.payload_json = decided
    item = timeline.project_event(event)
    assert item.actor.type == ("user" if human else "agent") and item.actor.id == reviewer
    assert item.data["feedback"] == "Ready to publish" and item.data["verdict"] == "approve"
    assert "evidence_json" not in item.data


@pytest.mark.asyncio
async def test_snapshot_and_replay_keep_unique_items_and_order(session, admin_ctx):
    from jhin_db.models import Conversation
    from jhin_db.models.timeline import ConversationEvent

    chat = Conversation(
        workspace_id=admin_ctx.workspace_id, title="Journal", last_activity_at=datetime.now(UTC)
    )
    session.add(chat)
    await session.flush()
    first, second = new_uuid7(), new_uuid7()
    for sequence, source, text in [
        (1, first, "hello"),
        (2, second, "world"),
        (3, first, "updated"),
    ]:
        session.add(
            ConversationEvent(
                workspace_id=admin_ctx.workspace_id,
                conversation_id=chat.id,
                sequence=sequence,
                source_kind="message",
                source_id=source,
                operation="upsert",
                payload_json={"visibility": "visible", "content_json": {"text": text}},
            )
        )
    await session.commit()
    snapshot = await timeline.snapshot(session, admin_ctx.workspace_id, chat.id, limit=1)
    assert snapshot.cursor == 3 and snapshot.has_more
    assert snapshot.items[0].data["content_json"]["text"] == "world"
    older = await timeline.snapshot(
        session, admin_ctx.workspace_id, chat.id, before=snapshot.next_before
    )
    assert len(older.items) == 1
    assert older.items[0].id == f"message:{first}"
    assert older.items[0].data["content_json"]["text"] == "updated"
    events = await timeline.replay(session, admin_ctx.workspace_id, chat.id, after=1)
    assert [event.sequence for event in events] == [2, 3]


@pytest.mark.asyncio
async def test_snapshot_and_replay_withdraw_a_reply_without_its_deleted_text(session, admin_ctx):
    from jhin_db.models import Conversation
    from jhin_db.models.timeline import ConversationEvent

    original_time = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    chat = Conversation(
        workspace_id=admin_ctx.workspace_id, title="Withdraw reply", last_activity_at=original_time
    )
    session.add(chat)
    await session.flush()
    source, actor, run = new_uuid7(), new_uuid7(), new_uuid7()
    for sequence, operation in [(1, "upsert"), (2, "delete")]:
        session.add(
            ConversationEvent(
                workspace_id=admin_ctx.workspace_id,
                conversation_id=chat.id,
                sequence=sequence,
                source_kind="message",
                source_id=source,
                operation=operation,
                payload_json={
                    "sender_type": "agent",
                    "sender_id": str(actor),
                    "run_id": str(run),
                    "message_type": "text",
                    "visibility": "visible",
                    "created_at": original_time.isoformat(),
                    "content_json": {"text": "Deleted reply content"},
                },
            )
        )
    await session.commit()
    snapshot = await timeline.snapshot(session, admin_ctx.workspace_id, chat.id)
    replay = await timeline.replay(session, admin_ctx.workspace_id, chat.id, after=1)
    for item in [snapshot.items[0], replay[0]]:
        assert item.status == "removed" and item.id == f"message:{source}"
        assert item.run_id == run and item.actor.id == actor and item.created_at == original_time
        assert item.data["message_type"] == "text"
        assert "Deleted reply content" not in item.model_dump_json()


def test_human_terminal_projection_excludes_connection_capabilities_and_source_manifest():
    user_id = str(new_uuid7())
    public = timeline.public_data(
        "runtime_session",
        {
            "kind": "terminal",
            "status": "running",
            "user_id": user_id,
            "ticket_hash": "private-hash",
            "workspace_key": "private-disk",
            "config_json": {"command": "echo hello", "manifest": {"private": "revision"}},
            "state_json": {"runner_credentials": "private"},
        },
    )
    assert public == {
        "kind": "terminal",
        "status": "running",
        "user_id": user_id,
        "command": "echo hello",
    }


async def test_loaded_item_recovery_returns_latest_older_items_without_loading_gap(
    session, admin_ctx
):
    from jhin_db.models import Conversation, ConversationEvent

    chats = [
        Conversation(
            workspace_id=admin_ctx.workspace_id, title=str(n), last_activity_at=datetime.now(UTC)
        )
        for n in range(2)
    ]
    session.add_all(chats)
    await session.flush()
    older, newer, foreign = new_uuid7(), new_uuid7(), new_uuid7()
    for chat, sequence, source, text in [
        (chats[0], 1, older, "working"),
        (chats[0], 2, newer, "recent message"),
        (chats[0], 3, older, "completed while disconnected"),
        (chats[1], 1, foreign, "another chat"),
    ]:
        session.add(
            ConversationEvent(
                workspace_id=admin_ctx.workspace_id,
                conversation_id=chat.id,
                sequence=sequence,
                source_kind="message",
                source_id=source,
                operation="upsert",
                payload_json={"visibility": "visible", "content_json": {"text": text}},
            )
        )
    await session.commit()
    latest = await timeline.snapshot(session, admin_ctx.workspace_id, chats[0].id, limit=1)
    assert latest.items[0].id == f"message:{newer}"
    recovered = await timeline.snapshot(
        session,
        admin_ctx.workspace_id,
        chats[0].id,
        limit=1,
        item_ids=[f"message:{older}", f"message:{newer}", f"message:{foreign}"],
    )
    assert recovered.cursor == 3 and not recovered.has_more and recovered.next_before is None
    assert {item.id for item in recovered.items} == {f"message:{older}", f"message:{newer}"}
    assert recovered.items[0].data["content_json"]["text"] == "completed while disconnected"


@pytest.mark.parametrize("ids", [["bad"], ["message:not-a-uuid"], ["x:" + str(new_uuid7())] * 101])
async def test_loaded_item_recovery_rejects_malformed_or_unbounded_ids(session, admin_ctx, ids):
    from fastapi import HTTPException

    from jhin_db.models import Conversation

    chat = Conversation(
        workspace_id=admin_ctx.workspace_id,
        title="Bounded recovery",
        last_activity_at=datetime.now(UTC),
    )
    session.add(chat)
    await session.commit()
    with pytest.raises(HTTPException) as error:
        await timeline.snapshot(session, admin_ctx.workspace_id, chat.id, item_ids=ids)
    assert error.value.status_code == 422
