"""Every chat entry path must sanitize before persistence and Temporal input."""

import json

import pytest
from apps.api.tests.test_conversations_unit import FakeTemporal
from sqlalchemy import select

from jhin_api.conversations import service
from jhin_db.models import Agent, AuditEvent, Conversation, Message, Task
from jhin_db.models.variables import SecureInputCapture
from jhin_domain import new_uuid7

KEY = "ab" * 12 + ":" + "cd" * 32


@pytest.fixture
async def writer(session, admin_ctx):
    row = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    session.add(row)
    await session.flush()
    return row


async def assert_no_plaintext(session, temporal):
    for model, attributes in (
        (Conversation, ("title",)),
        (Task, ("title", "description", "metadata_json")),
        (Message, ("content_json",)),
        (AuditEvent, ("metadata_json",)),
    ):
        for row in (await session.scalars(select(model))).all():
            assert KEY not in json.dumps(
                {key: getattr(row, key) for key in attributes}, default=str
            )
    assert KEY not in repr(temporal.started) and KEY not in repr(temporal.signals)


async def test_create_encrypts_before_title_and_task_and_retries_return_same_chat(
    session, admin_ctx, writer, crypto
):
    temporal = FakeTemporal()
    args = {
        "agent_id": writer.id,
        "title": None,
        "text": "Here is my Ghost API key: " + KEY,
        "client_turn_id": "create-once",
        "request_id": new_uuid7(),
        "ip_hash": "h",
        "crypto": crypto,
    }
    chat, turn = await service.create_conversation(session, admin_ctx, temporal, **args)
    again, repeated = await service.create_conversation(session, admin_ctx, temporal, **args)
    assert chat.id == again.id and turn.message.id == repeated.message.id
    assert "[secure_input:" in turn.message.content_json["text"]
    assert turn.message.content_json["_human_authority"]["source"] == "browser"
    assert turn.message.content_json["_human_authority"]["role"] == "admin"
    assert turn.task.metadata_json["required_inputs"][0]["key"] == "ghost_admin_url"
    assert len((await session.scalars(select(SecureInputCapture))).all()) == 1
    assert len(temporal.started) == 1
    await assert_no_plaintext(session, temporal)


async def test_typed_key_with_explicit_connect_ghost_url_does_not_ask_for_same_url(
    session, admin_ctx, writer, crypto
):
    temporal = FakeTemporal()
    _chat, turn = await service.create_conversation(
        session,
        admin_ctx,
        temporal,
        agent_id=writer.id,
        title=None,
        text=(
            "Save this private key, then connect Ghost at http://jhin-ghost-acceptance:2368 "
            "using that new variable."
        ),
        secure_inputs=[{"name": "ghost_failure_key", "value": KEY}],
        crypto=crypto,
        client_turn_id="typed-explicit-url",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert not turn.task.metadata_json.get("required_inputs")
    assert turn.message.content_json["secure_inputs"][0]["kind"] == "ghost_admin_key"
    await assert_no_plaintext(session, temporal)


async def test_ingress_keeps_api_key_ceiling_through_send_queue_edit_and_legacy_task(
    session, admin_ctx, writer
):
    from dataclasses import replace

    from jhin_api.access.keys import ApiKeyPrincipal
    from jhin_api.tasks import service as tasks
    from jhin_db.models import ApiKey, WorkspaceMembership
    from jhin_domain import TaskPriority, WorkspaceRole
    from jhin_secrets.authority import human_message_authorized

    session.add(
        WorkspaceMembership(
            workspace_id=admin_ctx.workspace_id, user_id=admin_ctx.user.id, role="owner"
        )
    )
    key = ApiKey(
        workspace_id=admin_ctx.workspace_id,
        created_by_user_id=admin_ctx.user.id,
        name="Chat only",
        prefix="chat-only",
        key_hash="unused",
        role_ceiling="admin",
        scopes_json=["chats:write"],
    )
    session.add(key)
    await session.flush()
    ctx = replace(
        admin_ctx,
        api_key=ApiKeyPrincipal(
            id=key.id,
            workspace_id=key.workspace_id,
            name=key.name,
            prefix=key.prefix,
            role_ceiling=WorkspaceRole.ADMIN,
            scopes=frozenset({"chats:write"}),
        ),
    )
    temporal = FakeTemporal()
    chat, turn = await service.create_conversation(
        session,
        ctx,
        temporal,
        agent_id=writer.id,
        title=None,
        text="Save this company-wide",
        client_turn_id="ceiling",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert turn.message.content_json["_human_authority"]["source"] == "api_key"
    assert not await human_message_authorized(
        session, turn.message, required_scope="variables:write"
    )
    queued = await service.send_turn(
        session,
        admin_ctx,
        temporal,
        chat.id,
        text="Store company-wide",
        client_turn_id="queued-authorized",
        delivery="queue",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert await human_message_authorized(session, queued.message, required_scope="variables:write")
    await service.change_queued(
        session, ctx, chat.id, queued.task.id, text="Share this key company-wide"
    )
    assert not await human_message_authorized(
        session, queued.message, required_scope="variables:write"
    )
    task = await tasks.assign_task(
        session,
        ctx,
        temporal,
        writer.id,
        values={
            "title": "Set variable",
            "description": "Store this company-wide",
            "priority": TaskPriority.NORMAL,
        },
        request_id=new_uuid7(),
        ip_hash="h",
    )
    message = await session.scalar(
        select(Message).where(Message.task_id == task.id, Message.sender_type == "user")
    )
    assert message is not None and not await human_message_authorized(
        session, message, required_scope="variables:write"
    )


async def test_send_steer_queue_and_queued_edit_capture_before_receipts(
    session, admin_ctx, writer, crypto
):
    temporal = FakeTemporal()
    chat, first = await service.create_conversation(
        session,
        admin_ctx,
        temporal,
        agent_id=writer.id,
        title=None,
        text="Prepare a blog",
        client_turn_id="first",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    kwargs = {"request_id": new_uuid7(), "ip_hash": "h", "crypto": crypto}
    steer = await service.send_turn(
        session,
        admin_ctx,
        temporal,
        chat.id,
        text="Ghost API key: " + KEY,
        client_turn_id="steer",
        **kwargs,
    )
    retry = await service.send_turn(
        session,
        admin_ctx,
        temporal,
        chat.id,
        text="Ghost API key: " + KEY,
        client_turn_id="steer",
        **kwargs,
    )
    assert steer.message.id == retry.message.id
    assert first.task.metadata_json["required_inputs"][0]["key"] == "ghost_admin_url"
    queued = await service.send_turn(
        session,
        admin_ctx,
        temporal,
        chat.id,
        text="Use this later",
        client_turn_id="queue",
        delivery="queue",
        secure_inputs=[{"name": "ghost.admin_key", "value": KEY}],
        **kwargs,
    )
    receipt = await service.change_queued(
        session,
        admin_ctx,
        chat.id,
        queued.task.id,
        text="The Ghost API key is " + KEY,
        crypto=crypto,
    )
    assert KEY not in receipt["text"]
    await assert_no_plaintext(session, temporal)


async def test_missing_crypto_never_commits_a_secret_chat(session, admin_ctx, writer):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await service.create_conversation(
            session,
            admin_ctx,
            FakeTemporal(),
            agent_id=writer.id,
            title=KEY,
            text="Ghost API key: " + KEY,
            client_turn_id="no-key",
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert error.value.status_code == 503
    await session.rollback()
    assert not (await session.scalars(select(Message))).all()


async def test_explicit_admin_url_satisfies_only_same_message_context(
    session, admin_ctx, writer, crypto
):
    _chat, turn = await service.create_conversation(
        session,
        admin_ctx,
        FakeTemporal(),
        agent_id=writer.id,
        title=None,
        text="Ghost Admin URL: https://admin.example.test\nGhost API key: " + KEY,
        client_turn_id="complete",
        request_id=new_uuid7(),
        ip_hash="h",
        crypto=crypto,
    )
    assert "required_inputs" not in turn.task.metadata_json
    assert KEY not in turn.message.content_json["text"]


async def test_question_answer_encrypts_before_required_resolution_and_retry(
    session, admin_ctx, writer, crypto
):
    from datetime import UTC, datetime, timedelta

    from apps.api.tests.test_questions_unit import FakeTemporal as QuestionTemporal

    from jhin_api.questions import service as questions
    from jhin_api.questions.schemas import AnswerQuestionIn
    from jhin_db.models import UserQuestion

    temporal = FakeTemporal()
    chat, turn = await service.create_conversation(
        session,
        admin_ctx,
        temporal,
        agent_id=writer.id,
        title=None,
        text="Set up the blog",
        client_turn_id="question-start",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    question = UserQuestion(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=chat.id,
        task_id=turn.task.id,
        agent_id=writer.id,
        kind="clarification",
        question="Provide the setup details",
        context="",
        options_json=[],
        allow_other=True,
        dedupe_hash=new_uuid7().hex,
        idempotency_key=str(new_uuid7()),
        status="pending",
        asked_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        required=True,
        input_key="setup_details",
        value_type="text",
    )
    session.add(question)
    await session.commit()
    args = {"request_id": new_uuid7(), "ip_hash": "h", "crypto": crypto}
    payload = AnswerQuestionIn(other_text="Ghost API key: " + KEY)
    question_temporal = QuestionTemporal()
    result, _ = await questions.answer(
        session, admin_ctx, question_temporal, question.id, payload, **args
    )
    again, _ = await questions.answer(
        session, admin_ctx, question_temporal, question.id, payload, **args
    )
    assert result.answer_text == again.answer_text
    assert KEY not in result.answer_text and "[secure_input:" in result.answer_text
    assert len((await session.scalars(select(SecureInputCapture))).all()) == 1
    assert turn.task.metadata_json["required_inputs"][0]["key"] == "ghost_admin_url"
    assert KEY not in repr(question_temporal.signals)
    await assert_no_plaintext(session, temporal)


async def test_legacy_message_task_and_instruction_intake_is_secure(
    session, admin_ctx, writer, crypto
):
    from jhin_api.tasks import service as tasks
    from jhin_domain import TaskPriority

    temporal = FakeTemporal()
    args = {"request_id": new_uuid7(), "ip_hash": "h", "crypto": crypto}
    message_task = await tasks.message_agent(
        session, admin_ctx, temporal, writer.id, text="Ghost API key: " + KEY, **args
    )
    task = await tasks.create_task(
        session,
        admin_ctx,
        temporal,
        values={
            "title": "Ghost API key: " + KEY,
            "description": "Set this up",
            "agent_id": writer.id,
            "priority": TaskPriority.NORMAL,
        },
        **args,
    )
    assert task.conversation_id is not None
    await tasks.send_instruction(
        session, admin_ctx, temporal, message_task.id, text="Ghost API key: " + KEY, **args
    )
    assert task.metadata_json["required_inputs"][0]["key"] == "ghost_admin_url"
    await assert_no_plaintext(session, temporal)
