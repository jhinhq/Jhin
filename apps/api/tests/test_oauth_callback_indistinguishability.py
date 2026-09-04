"""Adversarial probes against the OAuth callbacks' one deliberate promise:

that no observer without the flow's own session can tell any two outcomes
apart. Each test tries to find a pair that should be indistinguishable and
is not -- a signed-in stranger reading database state off a landing, a
receipt read through the wrong callback or from another workspace, a
demoted member learning a configuration fact, a provider's own error code
reaching a Location, a handle reaching a log line.

Written during review of the change that stopped those callbacks answering
in JSON, and kept because the promise is easy to break by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from apps.api.tests.oauth_callback_harness import CALLBACK_APP_URL as APP_URL
from apps.api.tests.oauth_callback_harness import CallbackHarness, assert_refused
from sqlalchemy import select

from jhin_api.oauth.redirect import CALLBACK_PATH, GITHUB_APP_CALLBACK_PATH
from jhin_db.models import Connection, OAuthAuthorization, Workspace, WorkspaceMembership

OUTCOMES = [
    "connected",
    "denied",
    "failed",
    "client_rejected",
    "callback_mismatch",
    "redirect_changed",
    "issuer_mismatch",
    "registration_gone",
]


def sig(response: httpx.Response) -> tuple[int, str, bytes, str]:
    return (
        response.status_code,
        response.headers["location"],
        response.content,
        response.headers.get("content-type", ""),
    )


async def test_a_signed_in_stranger_cannot_distinguish_any_database_state(
    callback: CallbackHarness,
) -> None:
    """Every row shape a prober could care about, seen from the wrong session."""
    handles: list[str] = ["z" * 43]  # a handle that names nothing at all

    _live, live_handle = await callback.pending()
    handles.append(live_handle)

    expired, expired_handle = await callback.pending()
    expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()
    handles.append(expired_handle)

    consumed, consumed_handle = await callback.pending()
    consumed.consumed_at = datetime.now(UTC)
    await callback.session.commit()
    handles.append(consumed_handle)

    _dev, device_handle = await callback.pending(flow="device_code")
    handles.append(device_handle)

    connection = Connection(
        workspace_id=callback.workspace_id,
        connector_type="mcp",
        name="Owned",
        auth_type="oauth",
        config_json={},
    )
    callback.session.add(connection)
    await callback.session.commit()

    for outcome in OUTCOMES:
        _row, receipt_handle = await callback.settled(
            outcome=outcome,
            connection_id=connection.id if outcome == "connected" else None,
        )
        handles.append(receipt_handle)

    callback.actor["user"] = callback.other
    seen = {
        sig(await callback.client.get(CALLBACK_PATH, params={"state": h, "code": "x"}))
        for h in handles
    }

    assert len(seen) == 1, f"a stranger can tell these apart: {seen}"
    assert next(iter(seen))[1] == f"{APP_URL}/apps?oauth_error=expired"


async def test_a_stranger_cannot_read_a_connected_receipts_public_id(
    callback: CallbackHarness,
) -> None:
    connection = Connection(
        workspace_id=callback.workspace_id,
        connector_type="mcp",
        name="Secret Name",
        auth_type="oauth",
        config_json={},
    )
    callback.session.add(connection)
    await callback.session.commit()
    public_id = connection.public_id
    _row, handle = await callback.settled(outcome="connected", connection_id=connection.id)

    owner = await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})
    assert owner.headers["location"] == f"{APP_URL}/apps?connection={public_id}"

    callback.actor["user"] = callback.other
    stranger = await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})
    assert_refused(stranger)
    assert public_id not in stranger.headers["location"]


async def test_a_receipt_cannot_be_read_through_the_other_callback(
    callback: CallbackHarness,
) -> None:
    """A cross-flow read would make each callback an oracle for the other."""
    _oauth_row, oauth_handle = await callback.settled(outcome="denied")
    _gh_row, gh_handle = await callback.settled(
        outcome="github_app_created", flow="github_app_manifest"
    )

    crossed_a = await callback.client.get(
        GITHUB_APP_CALLBACK_PATH, params={"state": oauth_handle, "code": "x"}
    )
    crossed_b = await callback.client.get(CALLBACK_PATH, params={"state": gh_handle, "code": "x"})

    assert_refused(crossed_a)
    assert_refused(crossed_b)
    assert "github_app" not in crossed_b.headers["location"]


async def test_a_member_removed_from_the_workspace_is_not_told_a_config_fact(
    callback: CallbackHarness,
) -> None:
    """The gate must fail closed on no membership at all, not just a demotion."""
    _row, handle = await callback.settled(outcome="client_rejected")

    membership = await callback.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == callback.workspace_id,
            WorkspaceMembership.user_id == callback.admin.id,
        )
    )
    assert membership is not None
    await callback.session.delete(membership)
    await callback.session.commit()

    response = await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})
    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=failed&app=mcp"


async def test_a_receipt_pointing_at_another_workspaces_connection_discloses_nothing(
    callback: CallbackHarness,
) -> None:
    other_workspace = Workspace(name="Other", slug=f"other-{datetime.now(UTC).timestamp()}")
    callback.session.add(other_workspace)
    await callback.session.flush()
    foreign = Connection(
        workspace_id=other_workspace.id,
        connector_type="mcp",
        name="Not Yours",
        auth_type="oauth",
        config_json={},
    )
    callback.session.add(foreign)
    await callback.session.commit()
    foreign_public_id = foreign.public_id

    _row, handle = await callback.settled(outcome="connected", connection_id=foreign.id)

    response = await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})
    assert foreign_public_id not in response.headers["location"]
    assert response.headers["location"] == f"{APP_URL}/apps"


async def test_a_prefetch_never_reads_a_receipt_either(callback: CallbackHarness) -> None:
    """The guard is before everything, so it cannot be a side channel."""
    _row, handle = await callback.settled(outcome="client_rejected")

    response = await callback.client.get(
        CALLBACK_PATH,
        params={"state": handle, "code": "x"},
        headers={"Sec-Purpose": "prefetch;prerender"},
    )

    assert response.headers["location"] == f"{APP_URL}/apps"


async def test_a_provider_error_code_itself_never_reaches_the_location(
    callback: CallbackHarness,
) -> None:
    _row, handle = await callback.pending()

    response = await callback.client.get(
        CALLBACK_PATH,
        params={"state": handle, "error": "server_error\r\nX-Injected: 1"},
    )

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"
    assert "X-Injected" not in str(response.headers)


@pytest.mark.parametrize(
    "junk",
    ["../evil", "https://evil.example", "mcp evil", "MCP", "", "a" * 51, "mcp\r\nX: 1"],
)
async def test_no_hand_edited_connector_type_becomes_a_second_string_in_a_url(
    callback: CallbackHarness, junk: str
) -> None:
    row, handle = await callback.pending()
    row.connector_type = junk
    await callback.session.commit()

    response = await callback.client.get(
        CALLBACK_PATH, params={"state": handle, "error": "access_denied"}
    )

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied"
    assert response.status_code == 303


async def test_a_stranger_cannot_spend_a_live_row_even_with_a_valid_handle(
    callback: CallbackHarness,
) -> None:
    """The misdelivered-callback denial of service, asserted from both sides."""
    row, handle = await callback.pending()
    row_id = row.id

    callback.actor["user"] = callback.other
    for _ in range(5):
        assert_refused(
            await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})
        )

    callback.actor["user"] = callback.admin
    reloaded = await callback.session.get(OAuthAuthorization, row_id)
    assert reloaded is not None
    await callback.session.refresh(reloaded)
    assert reloaded.consumed_at is None
    assert reloaded.outcome is None
    assert reloaded.verifier_secret_id is not None


async def test_the_refusal_log_line_never_carries_the_handle(
    callback: CallbackHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _row, handle = await callback.pending(flow="device_code")
    await callback.client.get(CALLBACK_PATH, params={"state": handle, "code": "x"})

    out = capsys.readouterr().out
    assert "wrong_flow" in out
    assert handle not in out
    from jhin_oauth.pkce import state_hash

    assert state_hash(handle) not in out
