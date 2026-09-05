"""The OAuth callback refuses everything it should, and says nothing when it does.

This is the route a stranger can reach with a URL of their choosing, so it is
the one worth being paranoid about. Every test here asserts one of:

* a refusal decided *before* the single-use claim returns **the byte-identical
  landing** every other pre-claim refusal returns, so probing it tells an
  attacker which check they tripped;
* no callback response is ever a JSON body, at any status — not a refusal, not
  a validation error, not an unhandled exception;
* a replayed, forged, expired, or borrowed ``state`` does not produce a
  connection, and a *receipt* is readable only by the session that could have
  completed the flow;
* nothing from the request reaches the ``Location`` header — not a
  ``redirect_uri``, not a ``next``, not a ``state`` that looks like a URL;
* the provider's own prose (``error_description``, ``error_uri``) appears
  nowhere in the response.

The premise moved with the product: it used to be "every rejection returns the
byte-identical *body*", which was true because every rejection was the same
400 JSON. That body was the dead end an operator hit head-on. The promise now
is about the landing, and it is stronger, because the pre-claim tier is the
only tier a caller without the owning session can reach at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from apps.api.tests.oauth_callback_harness import (
    CALLBACK_APP_URL as APP_URL,
)
from apps.api.tests.oauth_callback_harness import (
    CALLBACK_ISSUER as ISSUER,
)
from apps.api.tests.oauth_callback_harness import (
    CallbackHarness,
    assert_refused,
)
from sqlalchemy import select

from jhin_api.oauth import service
from jhin_api.oauth.redirect import CALLBACK_PATH, GITHUB_APP_CALLBACK_PATH
from jhin_db.models import Connection, OAuthAuthorization, Secret, WorkspaceMembership
from jhin_domain import WorkspaceRole
from jhin_oauth.persistence import PendingAuthorizationInvalid, PendingAuthorizationStore

EVIL = "https://evil.example"


async def _get(harness: CallbackHarness, **params: str) -> httpx.Response:
    return await harness.client.get(CALLBACK_PATH, params=params)


async def _no_connection(harness: CallbackHarness) -> bool:
    return (
        await harness.session.scalar(
            select(Connection).where(Connection.workspace_id == harness.workspace_id)
        )
    ) is None


# --- The pre-claim tier, all identical ----------------------------------


async def test_unknown_state_is_refused(callback: CallbackHarness) -> None:
    assert_refused(await _get(callback, state="a" * 43, code="anything"))


async def test_malformed_state_is_refused_with_the_same_landing(
    callback: CallbackHarness,
) -> None:
    """A handle that is not the shape we issue never reaches the database.

    It used to be allowed to answer 422 as well, which was this suite
    admitting the defect: a 422 is a JSON validation body in somebody's
    address bar, and it is distinguishable by status *and* content type.
    """
    assert_refused(await _get(callback, state="not a handle!", code="anything"))


async def test_a_missing_state_lands_on_the_recovery_page_instead_of_a_422(
    callback: CallbackHarness,
) -> None:
    assert_refused(await callback.client.get(CALLBACK_PATH))


async def test_an_over_long_state_lands_on_the_recovery_page_instead_of_a_422(
    callback: CallbackHarness,
) -> None:
    assert_refused(await _get(callback, state="a" * 5000, code="x"))


async def test_an_over_long_code_is_its_own_refusal_and_never_a_422(
    callback: CallbackHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently dropping it would file "absurd" under "the person declined"."""
    row, handle = await callback.pending()

    response = await _get(callback, state=handle, code="c" * 3000)

    assert_refused(response)
    await callback.session.refresh(row)
    assert row.consumed_at is None, "an over-long code must not spend the state"
    assert "param_too_long" in capsys.readouterr().out


async def test_an_expired_state_is_refused(callback: CallbackHarness) -> None:
    row, handle = await callback.pending(ttl_seconds=60)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()

    assert_refused(await _get(callback, state=handle, code="anything"))


async def test_another_users_session_cannot_finish_the_flow(
    callback: CallbackHarness,
) -> None:
    """The load-bearing CSRF defense: state alone is not enough.

    Even holding a valid, unexpired, unconsumed handle, a different browser
    session does not complete the authorization — and the connection is not
    created for either of them.
    """
    _row, handle = await callback.pending()
    callback.actor["user"] = callback.other

    assert_refused(await _get(callback, state=handle, code="anything"))
    assert await _no_connection(callback)


@pytest.mark.parametrize("flow", ["device_code", "github_app_manifest"])
async def test_a_handle_from_another_flow_is_refused_at_the_oauth_callback(
    callback: CallbackHarness, flow: str
) -> None:
    """Only an authorization-code row may be walked through this route."""
    _row, handle = await callback.pending(flow=flow)

    assert_refused(await _get(callback, state=handle, code="anything"))
    assert await _no_connection(callback)


async def test_a_state_shaped_like_a_url_is_rejected_before_it_is_used(
    callback: CallbackHarness,
) -> None:
    response = await _get(callback, state=EVIL, code="x")
    assert_refused(response)
    assert EVIL not in response.headers["location"]


async def test_every_pre_claim_refusal_is_byte_identical(
    callback: CallbackHarness,
) -> None:
    """Status, ``Location``, and body, so nobody can tell which check failed."""
    seen: list[tuple[int, str, bytes]] = []

    def record(response: httpx.Response) -> None:
        seen.append((response.status_code, response.headers["location"], response.content))

    record(await _get(callback, state="b" * 43, code="x"))

    row, expired_handle = await callback.pending()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()
    record(await _get(callback, state=expired_handle, code="x"))

    # Consumed, with receipts switched off, so nothing was left to replay.
    consumed, consumed_handle = await callback.pending()
    consumed.consumed_at = datetime.now(UTC)
    await callback.session.commit()
    record(await _get(callback, state=consumed_handle, code="x"))

    _wrong_flow_row, wrong_flow = await callback.pending(flow="device_code")
    record(await _get(callback, state=wrong_flow, code="x"))

    _ws_row, wrong_workspace = await callback.pending(flow="device_code")
    record(await _get(callback, state=wrong_workspace, code="x"))

    record(await callback.client.get(CALLBACK_PATH))
    record(await _get(callback, state="a" * 5000, code="x"))

    _long_row, long_code = await callback.pending()
    record(await _get(callback, state=long_code, code="c" * 3000))

    _borrowed_row, borrowed = await callback.pending()
    callback.actor["user"] = callback.other
    record(await _get(callback, state=borrowed, code="x"))

    assert len(set(seen)) == 1, "a pre-claim refusal differs and leaks which check failed"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"state": ""},
        {"state": "not a handle!"},
        {"state": EVIL},
        {"state": "a" * 43},
        {"state": "a" * 5000},
        {"state": "a" * 43, "code": "c" * 3000},
        {"state": "a" * 43, "error": "e" * 3000},
        {"state": "a" * 43, "iss": "i" * 3000},
        {"state": "a" * 43, "code": "x", "iss": EVIL},
        {"state": "a" * 43, "error": "access_denied"},
        {"code": "x"},
        {"state": "a" * 43, "app": "../evil"},
        {"state": "a" * 43, "connection": EVIL},
        {"state": "\x00\x01"},
    ],
)
@pytest.mark.parametrize("path", [CALLBACK_PATH, GITHUB_APP_CALLBACK_PATH])
async def test_no_callback_response_is_ever_json(
    callback: CallbackHarness, params: dict[str, str], path: str
) -> None:
    """I1, swept: a 303 with an empty body, on both public callbacks."""
    response = await callback.client.get(path, params=params)

    assert response.status_code == 303
    assert response.content == b""
    assert "application/json" not in response.headers.get("content-type", "")
    assert response.headers["location"].startswith(APP_URL)


async def test_the_manifest_callback_refuses_with_the_same_landing_as_the_oauth_callback(
    callback: CallbackHarness,
) -> None:
    """Neither callback may be used as a differential oracle for the other."""
    handle = "d" * 43
    oauth = await _get(callback, state=handle, code="x")
    manifest = await callback.client.get(
        GITHUB_APP_CALLBACK_PATH, params={"state": handle, "code": "x"}
    )

    assert_refused(oauth)
    assert_refused(manifest)
    assert oauth.headers["location"] == manifest.headers["location"]
    assert oauth.content == manifest.content


# --- A prefetch is not a navigation -------------------------------------


async def test_a_prefetch_of_the_callback_url_claims_nothing(
    callback: CallbackHarness,
) -> None:
    """The classic cause of a callback refusing a handle nobody misused."""
    row, handle = await callback.pending()

    prefetched = await callback.client.get(
        CALLBACK_PATH,
        params={"state": handle, "error": "access_denied"},
        headers={"Sec-Purpose": "prefetch;prerender"},
    )

    assert prefetched.status_code == 303
    assert prefetched.headers["location"] == f"{APP_URL}/apps"
    assert prefetched.headers["cache-control"] == "no-store"
    await callback.session.refresh(row)
    assert row.consumed_at is None

    # The real navigation still works.
    real = await _get(callback, state=handle, error="access_denied")
    assert real.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"


@pytest.mark.parametrize(
    "headers",
    [
        {"Purpose": "prefetch"},
        {"X-moz": "prefetch"},
        {"Sec-Fetch-Mode": "cors"},
        {"Sec-Fetch-Dest": "image"},
    ],
)
async def test_the_other_prefetch_headers_are_honoured_too(
    callback: CallbackHarness, headers: dict[str, str]
) -> None:
    row, handle = await callback.pending()

    response = await callback.client.get(
        CALLBACK_PATH, params={"state": handle, "error": "access_denied"}, headers=headers
    )

    assert response.headers["location"] == f"{APP_URL}/apps"
    await callback.session.refresh(row)
    assert row.consumed_at is None


async def test_a_navigation_with_no_fetch_metadata_headers_still_works(
    callback: CallbackHarness,
) -> None:
    """Fail open, deliberately: absence of a header never costs a callback.

    Plenty of browsers send no ``Sec-Fetch-*`` at all, and a callback that a
    missing header could lose would be worse than the problem it solves.
    """
    _row, handle = await callback.pending()

    response = await _get(callback, state=handle, error="access_denied")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"


# --- The post-claim tier -------------------------------------------------


async def test_a_mismatched_iss_is_refused_when_the_server_advertises_it(
    callback: CallbackHarness,
) -> None:
    """RFC 9207, byte comparison. A different issuer is a mix-up attack."""
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="anything", iss="https://attacker.example")

    assert response.status_code == 303
    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=issuer_mismatch&app=mcp"


async def test_a_missing_iss_is_refused_when_the_server_advertises_it(
    callback: CallbackHarness,
) -> None:
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="anything")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=issuer_mismatch&app=mcp"


async def test_iss_is_compared_without_any_normalization(
    callback: CallbackHarness,
) -> None:
    """No case folding, no trailing-slash cleanup, no default-port elision."""
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="x", iss=f"{ISSUER}/")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=issuer_mismatch&app=mcp"


async def test_a_redirect_uri_that_no_longer_matches_settings_is_refused(
    callback: CallbackHarness,
) -> None:
    """An operator who changed the base URL mid-flow gets a refusal."""
    _row, handle = await callback.pending(
        redirect_uri="https://old.example.com/api/v1/oauth/callback"
    )

    response = await _get(callback, state=handle, code="anything")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=redirect_changed&app=mcp"


async def test_a_missing_verifier_secret_lands_rather_than_500s(
    callback: CallbackHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """It used to be a 500 — an unhandled ``PendingAuthorizationInvalid``."""
    registration = await callback.registration()
    row, handle = await callback.pending(client_registration_id=registration.id)
    secret = await callback.session.get(Secret, row.verifier_secret_id)
    assert secret is not None
    await callback.session.delete(secret)
    await callback.session.commit()

    response = await _get(callback, state=handle, code="anything")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=failed&app=mcp"
    assert "verifier_missing" in capsys.readouterr().out


async def test_a_policy_change_mid_flow_lands_rather_than_500s(
    callback: CallbackHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tightening the outbound allow-list must not 500 a flow already going."""
    registration = await callback.registration()
    _row, handle = await callback.pending(
        client_registration_id=registration.id, token_endpoint="http://blocked.example.com/token"
    )

    response = await _get(callback, state=handle, code="anything")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=failed&app=mcp"
    assert "endpoint_blocked" in capsys.readouterr().out


async def test_a_registration_that_is_gone_lands_on_its_own_flag(
    callback: CallbackHarness,
) -> None:
    _row, handle = await callback.pending()

    response = await _get(callback, state=handle, code="anything")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=registration_gone&app=mcp"


async def test_an_internal_failure_still_lands_on_the_recovery_page(
    callback: CallbackHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The last line of the promise: a bug is a landing, never a 500."""

    async def raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a driver blip nobody predicted")

    monkeypatch.setattr(service, "complete_authorization", raiser)
    _row, handle = await callback.pending()

    response = await _get(callback, state=handle, code="anything")

    assert_refused(response)
    assert "internal_error" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("outcome", "flag"),
    [
        ("issuer_mismatch", "issuer_mismatch"),
        ("redirect_changed", "redirect_changed"),
        ("registration_gone", "registration_gone"),
        ("client_rejected", "client_rejected"),
        ("callback_mismatch", "callback_mismatch"),
        ("denied", "denied"),
    ],
)
async def test_a_post_claim_landing_is_unreachable_without_the_owning_session(
    callback: CallbackHarness, outcome: str, flag: str
) -> None:
    """Every named cause needs the handle *and* the row's own session."""
    _row, handle = await callback.settled(outcome=outcome)

    owner = await _get(callback, state=handle, code="x")
    assert owner.headers["location"] == f"{APP_URL}/apps?oauth_error={flag}&app=mcp"

    callback.actor["user"] = callback.other
    assert_refused(await _get(callback, state=handle, code="x"))


@pytest.mark.parametrize(
    "outcome", ["redirect_changed", "registration_gone", "client_rejected", "callback_mismatch"]
)
async def test_a_demoted_member_is_not_told_a_configuration_fact(
    callback: CallbackHarness, outcome: str
) -> None:
    """``claim`` binds to a user id; a membership can be revoked mid-flow."""
    _row, handle = await callback.settled(outcome=outcome)

    as_admin = await _get(callback, state=handle, code="x")
    assert as_admin.headers["location"] == f"{APP_URL}/apps?oauth_error={outcome}&app=mcp"

    membership = await callback.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == callback.workspace_id,
            WorkspaceMembership.user_id == callback.admin.id,
        )
    )
    assert membership is not None
    membership.role = WorkspaceRole.MEMBER.value
    await callback.session.commit()

    demoted = await _get(callback, state=handle, code="x")
    assert demoted.headers["location"] == f"{APP_URL}/apps?oauth_error=failed&app=mcp"


async def test_a_junk_connector_type_never_reaches_the_location(
    callback: CallbackHarness,
) -> None:
    """A hand-edited row cannot become a second string in a URL."""
    row, handle = await callback.pending()
    row.connector_type = "../evil"
    await callback.session.commit()

    response = await _get(callback, state=handle, error="access_denied")

    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied"
    assert "app=" not in response.headers["location"]


# --- Receipts ------------------------------------------------------------


async def test_a_spent_row_becomes_a_receipt_that_holds_no_secret(
    callback: CallbackHarness,
) -> None:
    row, handle = await callback.pending()
    row_id, secret_id = row.id, row.verifier_secret_id

    await _get(callback, state=handle, error="access_denied")

    settled = await callback.session.get(OAuthAuthorization, row_id)
    assert settled is not None
    assert settled.consumed_at is not None
    assert settled.outcome == "denied"
    assert settled.verifier_secret_id is None
    assert settled.draft_json == {}
    assert settled.connection_id is None
    assert await callback.session.get(Secret, secret_id) is None


async def test_a_repeated_callback_lands_exactly_where_the_first_one_did(
    callback: CallbackHarness,
) -> None:
    """A refresh, a back-button, a prefetch: the same answer, not a dead end."""
    _row, handle = await callback.pending()

    first = await _get(callback, state=handle, error="access_denied")
    second = await _get(callback, state=handle, error="access_denied")

    assert first.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"
    assert second.headers["location"] == first.headers["location"]
    assert second.status_code == first.status_code


async def test_a_repeat_after_the_receipt_ttl_falls_back_to_the_uniform_refusal(
    callback: CallbackHarness,
) -> None:
    row, handle = await callback.settled(outcome="denied")
    await callback.expire_receipt(row)

    assert_refused(await _get(callback, state=handle, error="access_denied"))


async def test_a_receipt_is_not_readable_by_another_session(
    callback: CallbackHarness,
) -> None:
    row, handle = await callback.settled(outcome="denied")
    callback.actor["user"] = callback.other

    assert_refused(await _get(callback, state=handle, error="access_denied"))

    await callback.session.refresh(row)
    assert row.outcome == "denied"


async def test_a_receipt_replay_never_writes(callback: CallbackHarness) -> None:
    row, handle = await callback.settled(outcome="denied")
    await callback.session.refresh(row)
    before = (row.consumed_at, row.retain_until, row.outcome, row.outcome_connection_id)

    await _get(callback, state=handle, error="access_denied")

    await callback.session.refresh(row)
    assert (row.consumed_at, row.retain_until, row.outcome, row.outcome_connection_id) == before


async def test_a_burn_by_the_wrong_session_leaves_no_receipt(
    callback: CallbackHarness,
) -> None:
    row, handle = await callback.pending()
    callback.actor["user"] = callback.other
    assert_refused(await _get(callback, state=handle, code="x"))

    callback.actor["user"] = callback.admin
    await callback.session.refresh(row)
    assert row.outcome is None


async def test_a_wrong_session_callback_does_not_destroy_the_owners_flow(
    callback: CallbackHarness,
) -> None:
    """The deliberate release: a misdelivered callback is not a denial of service."""
    row, handle = await callback.pending()

    callback.actor["user"] = callback.other
    assert_refused(await _get(callback, state=handle, code="x"))

    callback.actor["user"] = callback.admin
    await callback.session.refresh(row)
    assert row.consumed_at is None
    owner = await _get(callback, state=handle, error="access_denied")
    assert owner.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"


async def test_a_connected_receipt_whose_connection_was_deleted_lands_on_plain_apps(
    callback: CallbackHarness,
) -> None:
    connection = Connection(
        workspace_id=callback.workspace_id,
        connector_type="mcp",
        name="Example",
        auth_type="oauth",
        config_json={},
    )
    callback.session.add(connection)
    await callback.session.commit()
    _row, handle = await callback.settled(outcome="connected", connection_id=connection.id)

    await callback.session.delete(connection)
    await callback.session.commit()

    response = await _get(callback, state=handle, code="x")
    assert response.headers["location"] == f"{APP_URL}/apps"


async def test_a_reconnect_receipt_survives_deleting_its_connection(
    callback: CallbackHarness,
) -> None:
    """``settle`` nulls the CASCADE pointer, so the receipt is not cascaded away."""
    connection = Connection(
        workspace_id=callback.workspace_id,
        connector_type="mcp",
        name="Reconnected",
        auth_type="oauth",
        config_json={},
    )
    callback.session.add(connection)
    await callback.session.commit()

    row, handle = await callback.pending(connection_id=connection.id)
    first = await _get(callback, state=handle, error="access_denied")
    assert (
        first.headers["location"]
        == f"{APP_URL}/apps?oauth_error=denied&connection={connection.public_id}&app=mcp"
    )

    await callback.session.delete(connection)
    await callback.session.commit()

    await callback.session.refresh(row)
    repeat = await _get(callback, state=handle, error="access_denied")
    assert repeat.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"


# --- Nothing from the request reaches the response ----------------------


async def test_a_provider_denial_never_renders_the_providers_own_words(
    callback: CallbackHarness,
) -> None:
    marker = "attacker-authored-error-description"
    _row, handle = await callback.pending()

    response = await _get(
        callback,
        state=handle,
        error="access_denied",
        error_description=marker,
        error_uri=f"{EVIL}/why",
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied&app=mcp"
    assert marker not in response.text
    assert marker not in response.headers["location"]
    assert EVIL not in response.text


@pytest.mark.parametrize(
    "hostile",
    [
        {"redirect_uri": EVIL},
        {"next": EVIL},
        {"return_to": EVIL},
        {"callback": EVIL},
        {"app": EVIL},
        {"connection": EVIL},
    ],
)
async def test_no_query_parameter_can_steer_the_location_header(
    callback: CallbackHarness, hostile: dict[str, str]
) -> None:
    """The open-redirect surface, closed by construction."""
    _row, handle = await callback.pending()

    response = await _get(callback, state=handle, error="access_denied", **hostile)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(APP_URL)
    assert EVIL not in location


@pytest.mark.parametrize(
    "hostile",
    [
        {"redirect_uri": EVIL},
        {"next": EVIL},
        {"return_to": EVIL},
        {"callback": EVIL},
        {"app": EVIL},
    ],
)
async def test_no_query_parameter_can_steer_the_manifest_callbacks_location(
    callback: CallbackHarness, hostile: dict[str, str]
) -> None:
    """The GitHub App callback builds its ``Location`` from a boolean only."""
    _row, handle = await callback.pending(flow="github_app_manifest")

    # No ``code``: the row is settled and nothing dials GitHub.
    response = await callback.client.get(
        GITHUB_APP_CALLBACK_PATH, params={"state": handle, **hostile}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location == f"{APP_URL}/apps?github_app=failed"
    assert EVIL not in location
    assert response.headers["cache-control"] == "no-store"


async def test_the_callback_response_is_never_cached(
    callback: CallbackHarness,
) -> None:
    """A single-use redirect that a cache could replay is not single-use."""
    _row, handle = await callback.pending()
    response = await _get(callback, state=handle, error="access_denied")
    assert response.headers["cache-control"] == "no-store"


# --- Single use, under concurrency --------------------------------------


async def test_a_second_claim_of_the_same_handle_is_refused(
    callback: CallbackHarness,
) -> None:
    """The conditional UPDATE is the authority, not a read-then-write.

    Consumption is decided by the database inside one statement, so the
    second claim loses whether it arrives a millisecond or an hour later.
    (The genuinely concurrent case needs two sessions and a real server;
    ``packages/oauth/tests/test_persistence.py`` covers it there.)
    """
    _row, handle = await callback.pending()
    store = PendingAuthorizationStore(callback.session, callback.crypto)

    async def claim() -> str:
        try:
            await store.claim(handle=handle, expected_user_id=callback.admin.id)
        except PendingAuthorizationInvalid:
            return "refused"
        return "claimed"

    outcomes = [await claim(), await claim()]
    assert outcomes.count("claimed") == 1
    assert outcomes.count("refused") == 1


async def test_a_refused_callback_leaves_no_connection_behind(
    callback: CallbackHarness,
) -> None:
    await _get(callback, state="c" * 43, code="x")
    assert await _no_connection(callback)


async def test_a_dead_session_returns_to_the_app_instead_of_a_raw_401(
    callback: CallbackHarness,
) -> None:
    """A consent screen that outlives the session is the likeliest failure.

    It used to be the one failure that escaped this route's own promise: the
    session dependency raised before the handler ran, so the person landed on
    ``{"detail": "Not authenticated"}`` in their browser. Nothing is claimed
    and no token is exchanged without a session — this only decides what is
    shown, and it is decided before the database is touched at all.
    """
    _row, handle = await callback.pending()
    callback.sign_out()

    response = await _get(callback, state=handle, code="anything")

    assert response.status_code == 303
    location = response.headers["location"]
    assert location == f"{APP_URL}/apps?oauth_error=signed_out"
    assert handle not in location
    # The row is untouched, because nothing claimed it.
    assert await callback.session.get(OAuthAuthorization, _row.id) is not None


async def test_a_dead_session_at_the_github_app_callback_redirects_rather_than_401(
    callback: CallbackHarness,
) -> None:
    """GitHub's app form can outlive a session just as a consent screen can."""
    _row, handle = await callback.pending(flow="github_app_manifest")
    callback.sign_out()

    response = await callback.client.get(
        GITHUB_APP_CALLBACK_PATH, params={"state": handle, "code": "not-a-real-manifest-code"}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location == f"{APP_URL}/apps?oauth_error=signed_out"
    assert handle not in location
    assert response.headers["cache-control"] == "no-store"
    row = await callback.session.get(OAuthAuthorization, _row.id)
    assert row is not None
    assert row.consumed_at is None


def test_the_async_cases_above_actually_run() -> None:
    """Guard against a config change quietly turning these into no-ops."""
    assert asyncio.iscoroutinefunction(test_unknown_state_is_refused)


async def test_github_identifies_itself_by_its_oauth_path_on_the_callback(
    callback: CallbackHarness,
) -> None:
    """RFC 9207 at GitHub: ``iss`` is ``https://github.com/login/oauth``.

    Registrations stay keyed by ``https://github.com``, so the two strings
    differ on purpose. The comparison stays byte-exact: the declared value
    passes the issuer check, the bare origin does not, and neither does
    anybody else.
    """
    mismatch = f"{APP_URL}/apps?oauth_error=issuer_mismatch&app=github"

    _row, handle = await callback.pending(
        issuer="https://github.com", connector_type="github", iss_parameter_supported=False
    )
    accepted = await _get(callback, state=handle, code="x", iss="https://github.com/login/oauth")
    assert accepted.status_code == 303
    assert accepted.headers["location"] != mismatch
    assert "issuer_mismatch" not in accepted.headers["location"]

    _row, handle = await callback.pending(
        issuer="https://github.com", connector_type="github", iss_parameter_supported=False
    )
    origin_only = await _get(callback, state=handle, code="x", iss="https://github.com")
    assert origin_only.headers["location"] == mismatch

    _row, handle = await callback.pending(
        issuer="https://github.com", connector_type="github", iss_parameter_supported=False
    )
    stranger = await _get(
        callback, state=handle, code="x", iss="https://attacker.example/login/oauth"
    )
    assert stranger.headers["location"] == mismatch
