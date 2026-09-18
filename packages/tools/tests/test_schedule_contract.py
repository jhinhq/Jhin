"""Calendar names and execution receipts must agree across tool/API boundaries."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jhin_tools.schedule_tools import OwnScheduleCreate, OwnScheduleUpdate
from jhin_tools.scheduling import ScheduleCreate, ScheduleOut, next_occurrence


@pytest.fixture
async def schedule_chat(context):
    from jhin_db.models import AgentCapabilityGrant, Conversation, Task, User, WorkspaceMembership

    db = context.session
    user = User(email=f"schedule-{uuid4()}@example.com", display_name="Owner", password_hash="x")
    db.add(user)
    await db.flush()
    membership = WorkspaceMembership(
        workspace_id=context.workspace_id, user_id=user.id, role="owner"
    )
    chat = Conversation(
        workspace_id=context.workspace_id,
        title="Schedules",
        primary_agent_id=context.agent_id,
        created_by_user_id=user.id,
        last_activity_at=datetime.now(UTC),
    )
    db.add_all([membership, chat])
    await db.flush()
    task = Task(
        id=context.task_id,
        workspace_id=context.workspace_id,
        title="Chat",
        conversation_id=chat.id,
        correlation_id=uuid4(),
        assigned_agent_id=context.agent_id,
        metadata_json={"origin": "chat"},
    )
    db.add(task)
    for capability in ("schedules.manage", "schedules.read", "organization.ask_person"):
        db.add(
            AgentCapabilityGrant(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                capability=capability,
                effect="allow",
                scope_json={},
            )
        )
    await db.flush()
    return context, user, membership


async def prepared(gateway):
    result = await gateway.request("schedules.create", json.dumps(schedule_fields(enabled=True)))
    assert result.status == "executed", result
    return result.sanitized_output


async def answered(gateway, context, user, proposal, *, answer_kind="option", value="activate"):
    from jhin_db.models import Task, UserQuestion
    from jhin_secrets.authority import attest_human_content

    result = await gateway.request(
        "organization.ask_person",
        json.dumps(
            {
                "question": "Which schedule must never run?",
                "context": "Untrusted replacement",
                "input_key": proposal["confirmation_input_key"],
                "options": [
                    {"label": "Keep paused", "value": "activate"},
                    {"label": "Activate", "value": "keep_paused"},
                ],
            }
        ),
    )
    assert result.status == "executed", result
    question = await context.session.get(UserQuestion, UUID(result.sanitized_output["question_id"]))
    question.status = "answered"
    question.answer_kind = answer_kind
    question.answer_option_value = value if answer_kind == "option" else ""
    question.answer_text = "yes" if answer_kind != "option" else "Activate recurring work"
    question.answered_by_user_id = user.id
    question.answered_at = datetime.now(UTC)
    question.granted_authority = "workspace"
    task = await context.session.get(Task, context.task_id)
    task.metadata_json = {
        **task.metadata_json,
        "required_inputs": [],
        "resolved_input_authority": {
            question.input_key: attest_human_content(
                {}, workspace_id=context.workspace_id, user_id=user.id, role="owner"
            ),
        },
    }
    await context.session.flush()
    return question


async def activate(gateway, proposal, question_id):
    return await gateway.request(
        "schedules.update",
        json.dumps(
            {
                "schedule_id": proposal["schedule"]["id"],
                "expected_version": proposal["schedule"]["version"],
                "enabled": True,
                "authorized_by_question_id": str(question_id),
            }
        ),
    )


async def test_agent_create_never_activates_from_a_model_claim(schedule_chat, gateway):
    proposal = await prepared(gateway)
    assert not proposal["schedule"]["enabled"]
    assert proposal["schedule"]["next_run_at"] is None
    assert proposal["confirmation_input_key"].startswith("schedule_activate_")
    assert proposal["activation_preview"]["weekday_names"] == ["Monday"]
    result = await activate(gateway, proposal, uuid4())
    assert result.status == "failed" and result.error_code == "schedule_confirmation_required"


async def test_canonical_question_and_authenticated_choice_activate_exact_revision(
    schedule_chat, gateway
):
    context, user, _ = schedule_chat
    proposal = await prepared(gateway)
    question = await answered(gateway, context, user, proposal)
    assert "Activate" in question.question and "never" not in question.question
    assert "Monday" in question.context and "America/Los_Angeles" in question.context
    assert schedule_fields()["brief"] in question.context
    assert question.options_json[0]["label"] == "Activate recurring work"
    assert "incomplete" in question.options_json[1]["label"]
    result = await activate(gateway, proposal, question.id)
    assert result.status == "executed", result
    assert result.sanitized_output["schedule"]["enabled"]
    assert result.sanitized_output["schedule"]["next_run_at"]
    replay = await prepared(gateway)
    assert replay["schedule"]["enabled"]
    assert "already active" in replay["detail"] and "paused" not in replay["detail"]


async def test_reserved_confirmation_ignores_model_wording_before_schema_validation(
    schedule_chat, gateway
):
    from jhin_db.models import UserQuestion

    context, _, _ = schedule_chat
    proposal = await prepared(gateway)
    result = await gateway.request(
        "organization.ask_person",
        json.dumps(
            {
                "input_key": proposal["confirmation_input_key"],
                "question": "An invented activation description. " * 100,
                "context": "Hidden misleading description. " * 100,
                "options": [{"label": "Misleading", "value": "not a valid option!"}],
                "kind": "memory_scope",
                "required": True,
                "allow_other": False,
            }
        ),
    )
    assert result.status == "executed", result
    question = await context.session.get(UserQuestion, UUID(result.sanitized_output["question_id"]))
    assert question.question == "Activate this recurring work?"
    assert question.kind == "open" and not question.required and question.allow_other
    assert "Monday" in question.context and schedule_fields()["brief"] in question.context
    assert question.options_json[0]["value"] == "activate"


@pytest.mark.parametrize("echo", ["same_values", "nullable"])
async def test_activation_accepts_strict_adapter_update_shapes(schedule_chat, gateway, echo):
    context, user, _ = schedule_chat
    proposal = await prepared(gateway)
    question = await answered(gateway, context, user, proposal)
    extras = {
        key: value
        for key, value in schedule_fields().items()
        if key in {"name", "brief", "local_time", "timezone", "weekdays"}
    }
    if echo == "nullable":
        extras = dict.fromkeys(extras)
    result = await gateway.request(
        "schedules.update",
        json.dumps(
            {
                **extras,
                "schedule_id": proposal["schedule"]["id"],
                "expected_version": 1,
                "enabled": True,
                "authorized_by_question_id": str(question.id),
            }
        ),
    )
    assert result.status == "executed", result
    assert result.sanitized_output["schedule"]["enabled"]


@pytest.mark.parametrize("authority", ["absent", "narrow_api_key", "other_user", "wrong_role"])
async def test_schedule_activation_needs_original_authority_ceiling(
    schedule_chat, gateway, authority
):
    from jhin_db.models import Task

    context, user, _ = schedule_chat
    proposal = await prepared(gateway)
    question = await answered(gateway, context, user, proposal)
    task = await context.session.get(Task, context.task_id)
    proof = task.metadata_json["resolved_input_authority"][question.input_key]
    if authority == "absent":
        proof = {}
    elif authority == "narrow_api_key":
        proof["_human_authority"].update(
            source="api_key", api_key_id=str(uuid4()), scopes=["questions:answer"]
        )
    elif authority == "other_user":
        proof["_human_authority"]["user_id"] = str(uuid4())
    elif authority == "wrong_role":
        proof["_human_authority"]["role"] = "member"
    task.metadata_json = {
        **task.metadata_json,
        "resolved_input_authority": {question.input_key: proof},
    }
    await context.session.flush()
    result = await activate(gateway, proposal, question.id)
    assert result.status == "failed" and result.error_code == "schedule_confirmation_required"


@pytest.mark.parametrize(
    "mutation", ["free_text", "decline", "member", "other_chat", "edited", "forged_question"]
)
async def test_confirmation_cannot_be_reused_or_invented(schedule_chat, gateway, mutation):
    context, user, membership = schedule_chat
    proposal = await prepared(gateway)
    question = await answered(
        gateway,
        context,
        user,
        proposal,
        answer_kind="other" if mutation == "free_text" else "option",
        value="keep_paused" if mutation == "decline" else "activate",
    )
    if mutation == "member":
        membership.role = "member"
    elif mutation == "other_chat":
        question.conversation_id = uuid4()
    elif mutation == "forged_question":
        question.context = "Approve a harmless reminder only."
    elif mutation == "edited":
        update = await gateway.request(
            "schedules.update",
            json.dumps(
                {
                    "schedule_id": proposal["schedule"]["id"],
                    "expected_version": 1,
                    "brief": "Publish to a different destination.",
                }
            ),
        )
        assert update.status == "executed", update
        proposal = update.sanitized_output
    await context.session.flush()
    result = await activate(gateway, proposal, question.id)
    assert result.status == "failed" and result.error_code == "schedule_confirmation_required"


async def test_pause_remains_available_while_required_confirmation_is_pending(
    schedule_chat, gateway
):
    _context, _, _ = schedule_chat
    proposal = await prepared(gateway)
    await gateway.request(
        "organization.ask_person",
        json.dumps(
            {
                "question": "Review this schedule",
                "input_key": proposal["confirmation_input_key"],
            }
        ),
    )
    result = await gateway.request(
        "schedules.update",
        json.dumps(
            {
                "schedule_id": proposal["schedule"]["id"],
                "expected_version": 1,
                "enabled": False,
            }
        ),
    )
    assert result.status == "executed", result
    assert result.sanitized_output["schedule"]["enabled"] is False


async def test_api_revision_retires_stale_question_then_list_can_resume(schedule_chat, gateway):
    from jhin_db.models import UserQuestion
    from jhin_tools.scheduling import ScheduleUpdate, update_schedule

    context, user, _ = schedule_chat
    proposal = await prepared(gateway)
    asked = await gateway.request(
        "organization.ask_person",
        json.dumps(
            {
                "question": "Review schedule",
                "input_key": proposal["confirmation_input_key"],
            }
        ),
    )
    old = await context.session.get(UserQuestion, UUID(asked.sanitized_output["question_id"]))
    assert not old.required
    await update_schedule(
        context.session,
        context.workspace_id,
        UUID(proposal["schedule"]["id"]),
        ScheduleUpdate(expected_version=1, brief="Revised draft-only work"),
        user_id=user.id,
    )
    assert old.status == "cancelled"
    listed = await gateway.request("schedules.list", "{}")
    item = listed.sanitized_output["items"][0]
    assert item["confirmation_input_key"] != proposal["confirmation_input_key"]
    assert item["activation_preview"]["weekday_names"] == ["Monday"]
    proposal = {"schedule": item, "confirmation_input_key": item["confirmation_input_key"]}
    question = await answered(gateway, context, user, proposal)
    result = await activate(gateway, proposal, question.id)
    assert result.status == "executed", result
    assert result.sanitized_output["schedule"]["enabled"]


async def test_cached_schedule_cannot_overwrite_a_newer_database_revision(schedule_chat, gateway):
    from sqlalchemy import update

    from jhin_db.models.schedule import AgentSchedule

    context, user, _ = schedule_chat
    proposal = await prepared(gateway)
    question = await answered(gateway, context, user, proposal)
    row = await context.session.get(AgentSchedule, UUID(proposal["schedule"]["id"]))
    await context.session.execute(
        update(AgentSchedule)
        .where(AgentSchedule.id == row.id)
        .values(version=2, brief="A newer unapproved brief")
        .execution_options(synchronize_session=False)
    )
    assert row.version == 1  # another writer changed storage, not this identity map
    result = await activate(gateway, proposal, question.id)
    assert result.status == "failed" and result.error_code == "schedule_invalid"
    await context.session.refresh(row)
    assert row.version == 2 and not row.enabled and row.brief == "A newer unapproved brief"


def schedule_fields(**changes):
    return {
        "name": "Monday draft",
        "brief": "Write a draft for review; do not publish.",
        "local_time": "09:00",
        "timezone": "America/Los_Angeles",
        "weekdays": ["Monday"],
        "idempotency_key": "monday-draft",
        **changes,
    }


def test_agent_tools_expose_weekday_names_and_convert_monday_to_zero():
    request = OwnScheduleCreate(**schedule_fields())
    assert request.weekdays == [0]
    assert next_occurrence(
        datetime(2026, 9, 13, 2, 34, tzinfo=UTC),
        request.local_time,
        request.timezone,
        request.weekdays,
    ) == datetime(2026, 9, 14, 16, tzinfo=UTC)
    schema = OwnScheduleCreate.model_json_schema()
    assert schema["properties"]["weekdays"]["items"]["enum"] == [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    updated = OwnScheduleUpdate(schedule_id=uuid4(), expected_version=1, weekdays=["Monday"])
    assert updated.weekdays == [0]


@pytest.mark.parametrize("day", range(7))
def test_legacy_numeric_clients_keep_their_weekday_mapping(day):
    request = OwnScheduleCreate(**schedule_fields(weekdays=[day]))
    assert request.weekdays == [day]
    api = ScheduleCreate(agent_id=uuid4(), **request.model_dump())
    assert api.weekdays == [day]


@pytest.mark.parametrize("days", [["Funday"], [7], [-1], [True], ["1"]])
def test_invalid_agent_weekdays_are_not_coerced(days):
    with pytest.raises(ValidationError):
        OwnScheduleCreate(**schedule_fields(weekdays=days))


def test_receipt_includes_authoritative_local_date_weekday_and_offset():
    now = datetime(2026, 9, 13, tzinfo=UTC)
    row = ScheduleOut(
        **{
            key: value
            for key, value in schedule_fields(weekdays=[0]).items()
            if key != "idempotency_key"
        },
        id=uuid4(),
        workspace_id=uuid4(),
        agent_id=uuid4(),
        version=1,
        next_run_at=datetime(2026, 9, 14, 16, tzinfo=UTC),
        last_run_at=None,
        last_status="never",
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    output = row.model_dump(mode="json")
    assert output["weekday_names"] == ["Monday"]
    assert output["next_run_local"] == "2026-09-14T09:00:00-07:00"
    assert output["next_run_display"] == (
        "Monday, September 14, 2026 at 09:00 PDT (America/Los_Angeles)"
    )
    row.next_run_at = None
    assert row.model_dump()["next_run_display"] is None
