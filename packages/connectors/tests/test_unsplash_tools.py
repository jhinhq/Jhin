"""The model cannot manufacture a person's image choice or cross a conversation."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jhin_connectors.base import VerifyContext
from jhin_connectors.manifest import normalize_config
from jhin_connectors.unsplash import connector, tools
from jhin_connectors.unsplash.client import UnsplashError, verify_access_key
from jhin_connectors.unsplash.connector import UnsplashConnector
from jhin_connectors.unsplash.tools import AUTONOMOUS_SELECTION_KEY, answered_choice
from jhin_db.models import Agent, Conversation, User, UserQuestion, WorkspaceMembership
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7


@pytest.fixture
async def choice(context, workspace):
    db = context.session
    user = User(email="image@example.invalid", display_name="Owner", password_hash="fixture")
    agent = Agent(id=context.agent_id, workspace_id=workspace.id, name="Mindy", slug="mindy")
    db.add_all([user, agent])
    await db.flush()
    conversation = Conversation(
        workspace_id=workspace.id, title="Images", last_activity_at=datetime.now(UTC)
    )
    db.add_all(
        [
            conversation,
            WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"),
        ]
    )
    await db.flush()
    question = UserQuestion(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        task_id=context.task_id,
        agent_id=agent.id,
        kind="open",
        input_key="unsplash_photo",
        question="Choose a photo",
        dedupe_hash="f" * 64,
        idempotency_key="photo-choice",
        status="answered",
        asked_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        answered_at=datetime.now(UTC),
        answered_by_user_id=user.id,
        answer_kind="option",
        answer_option_value="photoABC",
    )
    db.add(question)
    await db.flush()
    assignment = EditorialAssignment(
        workspace_id=workspace.id,
        connection_id=new_uuid7(),
        writer_agent_id=agent.id,
        publisher_agent_id=agent.id,
        conversation_id=conversation.id,
        task_id=context.task_id,
    )
    return context, assignment, question


async def test_real_answer_is_authority(choice):
    ctx, assignment, question = choice
    assert await answered_choice(ctx, assignment, question.id, "unsplash_photo") == "photoABC"


@pytest.mark.parametrize(
    "mutation",
    ["pending", "no_person", "other_conversation", "other_agent", "cancelled", "wrong_kind"],
)
async def test_forged_or_unrelated_answer_is_denied(choice, mutation):
    ctx, assignment, question = choice
    if mutation == "pending":
        question.status = "pending"
    if mutation == "no_person":
        question.answered_by_user_id = None
    if mutation == "other_conversation":
        assignment.conversation_id = new_uuid7()
    if mutation == "other_agent":
        assignment.writer_agent_id = new_uuid7()
    if mutation == "cancelled":
        assignment.phase = "cancelled"
    if mutation == "wrong_kind":
        question.input_key = "unrelated"
    with pytest.raises(UnsplashError):
        await answered_choice(ctx, assignment, question.id, "unsplash_photo")


async def test_verification_probes_the_api_once_without_tracking_a_photo():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [], "total": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok, message, details = await verify_access_key("secret-test", client=client)

    assert ok is True
    assert len(seen) == 1
    assert seen[0].url.path == "/search/photos"
    assert seen[0].url.params["per_page"] == "1"
    assert seen[0].headers["Authorization"] == "Client-ID secret-test"
    assert "secret-test" not in str(seen[0].url)
    assert details["api_origin"] == "https://api.unsplash.com"
    assert "secret-test" not in message


async def test_connection_health_reports_a_working_key(monkeypatch):
    """A bound Unsplash connection is checkable; it must not read as unhealthy."""
    probed = []

    async def verify(access_key, *, client=None):
        probed.append(access_key)
        return True, "Unsplash accepted the Access Key", {"api_origin": "https://api.unsplash.com"}

    monkeypatch.setattr(connector, "verify_access_key", verify)
    health = await UnsplashConnector().verify_connection(
        VerifyContext(
            auth_type="api_key",
            credentials={"access_key": "synthetic-access-key"},
            config={"admin_url": "https://api.unsplash.com"},
        )
    )

    assert probed == ["synthetic-access-key"]
    assert health.ok is True
    assert health.details["api_origin"] == "https://api.unsplash.com"


@pytest.mark.parametrize("flaw", ["rejected", "no_key", "wrong_scheme", "foreign_origin"])
async def test_connection_health_fails_without_reaching_a_foreign_host(monkeypatch, flaw):
    probed = []

    async def verify(access_key, *, client=None):
        probed.append(access_key)
        return False, "Unsplash rejected the Access Key", {}

    monkeypatch.setattr(connector, "verify_access_key", verify)
    health = await UnsplashConnector().verify_connection(
        VerifyContext(
            auth_type="oauth2" if flaw == "wrong_scheme" else "api_key",
            credentials={} if flaw == "no_key" else {"access_key": "synthetic-access-key"},
            config={"admin_url": "https://api.unsplash.invalid"}
            if flaw == "foreign_origin"
            else {},
        )
    )

    assert health.ok is False
    assert "synthetic-access-key" not in health.message
    assert probed == (["synthetic-access-key"] if flaw == "rejected" else [])


@pytest.mark.parametrize("failure", ["rejected_key", "not_a_search_page"])
async def test_verification_reports_failure_without_the_key(failure):
    def handler(request):
        if failure == "rejected_key":
            return httpx.Response(401, text="Client-ID secret-test is invalid")
        return httpx.Response(200, content=json.dumps({"errors": ["nope"]}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok, message, details = await verify_access_key("secret-test", client=client)

    assert ok is False
    assert "secret-test" not in message
    assert details == {}


def test_autonomous_selection_is_a_connection_setting_not_a_tool_argument():
    """Where the approval can be written from decides who can write it.

    As a manifest setting it is reachable only through the authenticated
    connection config route. Nothing in the tool's own arguments names it, and
    a submitted key the manifest does not declare is refused outright, so a
    model asking for the mode in its arguments cannot get it.
    """
    fields = {field.name: field for field in UnsplashConnector.manifest.config_fields}
    approval = fields[AUTONOMOUS_SELECTION_KEY]
    assert approval.kind == "boolean" and approval.default is False
    assert approval.auth_types == ("api_key",)
    assert AUTONOMOUS_SELECTION_KEY not in tools.SelectInput.model_fields

    normalized = normalize_config(
        UnsplashConnector.manifest, "api_key", {AUTONOMOUS_SELECTION_KEY: True}
    )
    assert UnsplashConnector().validate_settings("api_key", normalized) == {
        AUTONOMOUS_SELECTION_KEY: True,
        "admin_url": "https://api.unsplash.com",
    }
    with pytest.raises(ValueError):
        normalize_config(UnsplashConnector.manifest, "api_key", {"photo_id": "photoABC"})
