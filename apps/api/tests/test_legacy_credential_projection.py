"""Read projections redact old rows without changing authoritative history."""

from datetime import UTC, datetime
from uuid import uuid4

from jhin_api.conversations.service import _preview_of
from jhin_api.conversations.timeline import public_data
from jhin_api.public_payloads import public_run_event_payload, public_tool_payload
from jhin_api.tasks.schemas import MessageOut
from jhin_db.models import Message

KEY = "ab" * 12 + ":" + "cd" * 32


def test_message_read_projection_keeps_db_input_untouched():
    payload = {"text": "My Ghost API key: " + KEY, "nested": {"value": KEY}}
    message = Message(
        id=uuid4(),
        task_id=None,
        run_id=None,
        sender_type="user",
        sender_id=uuid4(),
        message_type="text",
        content_json=payload,
        created_at=datetime.now(UTC),
    )
    result = MessageOut.model_validate(message).model_dump_json()
    assert KEY not in result and "REDACTED legacy credential" in result
    assert message.content_json == payload and KEY in payload["text"]


def test_journal_redacts_before_size_truncation_and_preserves_secure_refs():
    reference = f"[secure_input:{uuid4()}]"
    payload = {
        "visibility": "visible",
        "content_json": {"text": "x" * 65520 + " " + KEY, "reference": reference},
    }
    projected = public_data("message", payload)
    assert "ab" * 5 not in projected["content_json"]["text"]
    assert projected["content_json"]["reference"] == reference
    assert KEY in payload["content_json"]["text"]


def test_list_preview_redacts_before_short_cutoff():
    message = Message(message_type="text", content_json={"text": "x" * 140 + " " + KEY})
    assert "ab" * 5 not in _preview_of(message)


def test_tool_and_run_legacy_views_hide_nested_credentials_without_mutation():
    payload = {"text": KEY, "details": [{"password": "password is declared-private"}]}
    for projected in (
        public_tool_payload("example", payload),
        public_run_event_payload("example", payload),
    ):
        assert KEY not in str(projected) and "declared-private" not in str(projected)
    assert payload["text"] == KEY
