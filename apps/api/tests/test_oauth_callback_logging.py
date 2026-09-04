"""Why a callback refused is answered in the log, and only in the log.

The browser is told the same thing for every pre-claim refusal — that is the
point of ``docs/architecture/oauth.md``'s two tiers. So the operator's own
instance is the only place the distinction survives, and if it is not recorded
there it is not recorded anywhere. That was the second defect behind the
operator's dead end: they clicked Connect, got a raw JSON body, and nothing in
the server log could say whether the state had expired, been spent twice, or
never existed.

Every test here asserts one of:

* the reason vocabulary the service emits is exactly the one ``events.py``
  registers — because an unregistered value is *dropped* by
  ``normalize_log_field``, not recorded as prose, so drift is silent;
* each refusal logs its own reason, once;
* the line carries no handle, no hash, no provider prose, no ids.

The logging runtime is bootstrapped for real here rather than mocked, so the
assertions run against the same JSON an operator greps. ``conftest.py``'s
``restore_process_logging_globals`` puts the process back afterwards.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from apps.api.tests.oauth_callback_harness import (
    CALLBACK_ISSUER as ISSUER,
)
from apps.api.tests.oauth_callback_harness import (
    CallbackHarness,
)
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.redirect import CALLBACK_PATH, GITHUB_APP_CALLBACK_PATH
from jhin_db.models import Secret
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_oauth.pkce import state_hash
from jhin_observability import events
from jhin_observability.logging import configure_json_logging

REFUSED_EVENT = "oauth.callback_refused"


@pytest.fixture
def json_logs() -> Any:
    """The real JSON pipeline, including the events registry's own filtering.

    The root handler's stream is swapped for a buffer rather than read out of
    pytest's capture, so what these tests assert on is the same bytes an
    operator greps — normalization, allow-lists and all.
    """
    configure_json_logging("jhin-api", "test", level="DEBUG")
    stream = io.StringIO()
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(stream)

    def read() -> list[dict[str, Any]]:
        raw = stream.getvalue()
        stream.seek(0)
        stream.truncate(0)
        return [json.loads(line) for line in raw.splitlines() if line.lstrip().startswith("{")]

    return read


def _events(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == name]


# --- The registry and the service agree ---------------------------------


def test_the_registered_reasons_are_exactly_the_services_own() -> None:
    """Drift here is silent: an unregistered value vanishes from the log.

    ``normalize_log_field`` *drops* a value absent from a per-event allow-list
    rather than mapping it to ``"other"``, which is the behaviour wanted (no
    prose ever reaches a log line) and exactly why this parity test exists.
    """
    assert events._OAUTH_CALLBACK_REASONS == service.CALLBACK_REASONS
    assert events._OAUTH_CALLBACK_LANDINGS == service.CALLBACK_OUTCOMES
    assert events._OAUTH_FLOWS == service.CALLBACK_FLOWS


def test_the_three_callback_events_are_registered() -> None:
    for name in (REFUSED_EVENT, "oauth.callback_replayed", "oauth.callback_prefetch_ignored"):
        assert name in events.EVENT_FIELD_RULES, name


# --- Each refusal names itself ------------------------------------------


async def test_an_unknown_state_is_recorded_as_state_unknown(
    callback: CallbackHarness, json_logs: Any
) -> None:
    await callback.client.get(CALLBACK_PATH, params={"state": "a" * 43, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "state_unknown"
    assert record["flow"] == "authorization_code"
    assert record["connector_type"] == "other"


async def test_an_expired_state_is_recorded_as_expired_not_unknown(
    callback: CallbackHarness, json_logs: Any
) -> None:
    """The distinction the operator's instance could not make."""
    row, handle = await callback.pending()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "state_expired"


async def test_a_spent_state_with_no_receipt_is_recorded_as_consumed(
    callback: CallbackHarness, json_logs: Any
) -> None:
    row, handle = await callback.pending()
    row.consumed_at = datetime.now(UTC)
    await callback.session.commit()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "state_consumed"


async def test_another_users_callback_is_recorded_as_wrong_user(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending()
    callback.actor["user"] = callback.other

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "wrong_user"


async def test_a_handle_from_another_flow_is_recorded_as_wrong_flow(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending(flow="device_code")

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "wrong_flow"


async def test_a_dead_session_is_recorded_as_no_session(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending()
    callback.sign_out()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "no_session"


async def test_a_malformed_state_is_recorded_as_malformed(
    callback: CallbackHarness, json_logs: Any
) -> None:
    await callback.client.get(CALLBACK_PATH, params={"state": "not a handle!"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "state_malformed"


async def test_an_over_long_parameter_is_recorded_as_param_too_long(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "iss": "i" * 3000})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "param_too_long"


async def test_a_declined_consent_is_recorded_too(
    callback: CallbackHarness, json_logs: Any
) -> None:
    """So the "why did nothing connect" grep is complete rather than partial."""
    _row, handle = await callback.pending()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "error": "access_denied"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "provider_denied"
    assert record["connector_type"] == "mcp"


async def test_an_issuer_mismatch_is_recorded_apart_from_a_missing_issuer(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, present = await callback.pending(iss_parameter_supported=True)
    await callback.client.get(
        CALLBACK_PATH, params={"state": present, "code": "x", "iss": "https://attacker.example"}
    )
    (mismatch,) = _events(json_logs(), REFUSED_EVENT)
    assert mismatch["reason"] == "issuer_mismatch"

    _row2, absent = await callback.pending(iss_parameter_supported=True)
    await callback.client.get(CALLBACK_PATH, params={"state": absent, "code": "x"})
    (missing,) = _events(json_logs(), REFUSED_EVENT)
    assert missing["reason"] == "issuer_missing"


async def test_a_redirect_uri_change_is_recorded(callback: CallbackHarness, json_logs: Any) -> None:
    _row, handle = await callback.pending(redirect_uri="https://old.example.com/cb")

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "redirect_uri_changed"


async def test_a_missing_registration_is_recorded(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "registration_missing"


async def test_a_missing_verifier_is_recorded(callback: CallbackHarness, json_logs: Any) -> None:
    registration = await callback.registration()
    row, handle = await callback.pending(client_registration_id=registration.id)
    secret = await callback.session.get(Secret, row.verifier_secret_id)
    assert secret is not None
    await callback.session.delete(secret)
    await callback.session.commit()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "verifier_missing"


async def test_a_blocked_endpoint_is_recorded(callback: CallbackHarness, json_logs: Any) -> None:
    registration = await callback.registration()
    _row, handle = await callback.pending(
        client_registration_id=registration.id, token_endpoint="http://blocked.example.com/token"
    )

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "endpoint_blocked"


async def test_an_internal_failure_is_recorded_without_the_exception(
    callback: CallbackHarness, json_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver's message is not ours to render, so only our own word is kept."""

    async def raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a-driver-message-nobody-should-see")

    monkeypatch.setattr(service, "complete_authorization", raiser)
    _row, handle = await callback.pending()

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    records = json_logs()
    (record,) = _events(records, REFUSED_EVENT)
    assert record["reason"] == "internal_error"
    assert "a-driver-message-nobody-should-see" not in json.dumps(records)


async def test_a_manifest_refusal_names_the_manifest_flow(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending(flow="github_app_manifest")

    await callback.client.get(GITHUB_APP_CALLBACK_PATH, params={"state": handle})

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "manifest_no_code"
    assert record["flow"] == "github_app_manifest"


# --- Nothing that could identify the flow reaches the line --------------


async def test_the_refusal_line_never_carries_the_handle_or_the_providers_prose(
    callback: CallbackHarness, json_logs: Any
) -> None:
    """``audit_phase10_logging.py`` checks registration; this checks payload."""
    row, handle = await callback.pending(iss_parameter_supported=True)
    row_id = str(row.id)

    await callback.client.get(
        CALLBACK_PATH,
        params={
            "state": handle,
            "code": "authorization-code-value",
            "iss": "https://attacker.example",
            "error_description": "attacker-authored-error-description",
            "error_uri": "https://evil.example/why",
        },
    )

    records = _events(json_logs(), REFUSED_EVENT)
    assert len(records) == 1
    line = json.dumps(records[0])
    for forbidden in (
        handle,
        state_hash(handle),
        "authorization-code-value",
        "attacker-authored-error-description",
        "evil.example",
        "attacker.example",
        ISSUER,
        row_id,
        str(callback.admin.id),
    ):
        assert forbidden not in line, forbidden


# --- Prefetch, replay, and the diagnostic itself ------------------------


async def test_a_prefetch_logs_only_the_debug_line_and_no_refusal(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.pending()

    await callback.client.get(
        CALLBACK_PATH,
        params={"state": handle, "code": "x"},
        headers={"Sec-Purpose": "prefetch;prerender"},
    )

    records = json_logs()
    assert _events(records, REFUSED_EVENT) == []
    (record,) = _events(records, "oauth.callback_prefetch_ignored")
    assert record["flow"] == "authorization_code"


async def test_a_replay_logs_replayed_and_not_refused(
    callback: CallbackHarness, json_logs: Any
) -> None:
    _row, handle = await callback.settled(outcome="denied")

    await callback.client.get(CALLBACK_PATH, params={"state": handle, "error": "access_denied"})

    records = json_logs()
    assert _events(records, REFUSED_EVENT) == []
    (record,) = _events(records, "oauth.callback_replayed")
    assert record["landing"] == "denied"
    assert record["flow"] == "authorization_code"
    assert record["connector_type"] == "mcp"


# --- The device poll shares the event -----------------------------------


async def test_a_device_poll_refusal_is_recorded(
    callback: CallbackHarness, json_logs: Any, session: AsyncSession
) -> None:
    """One grep answers "why did nothing connect" for every flow."""
    ctx = WorkspaceContext(
        user=callback.admin, workspace_id=callback.workspace_id, role=WorkspaceRole.ADMIN
    )
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(HTTPException) as raised:
            await service.poll_device_flow(
                session,
                callback.crypto,
                ctx,
                http_client,
                "z" * 43,
                request_id=new_uuid7(),
                ip_hash="0" * 64,
            )
    assert raised.value.status_code == 410

    (record,) = _events(json_logs(), REFUSED_EVENT)
    assert record["reason"] == "state_unknown"
    assert record["flow"] == "device_code"
