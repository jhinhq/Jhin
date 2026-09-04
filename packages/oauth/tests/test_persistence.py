"""A pending authorization is single-use, short-lived, and tells nobody why.

The window between "Connect" and the provider's redirect is the one an
attacker wants to walk through. These tests hold the four properties that
close it, and the fifth that keeps the closing quiet:

* the raw handle is never persisted — only its hash;
* consumption is atomic, so a replay always loses;
* an expired row cannot be claimed at all;
* another user's session cannot claim a valid handle;
* and every one of those failures raises the same class with the *same
  message string*, so nothing an attacker can observe tells them which check
  they tripped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Fully qualified: a bare ``tests`` resolves to the repository-root
# integration package, not this one.
from packages.oauth.tests.db_fixtures import (
    Tenant,
    crypto,
    session,
    session_factory,
    tenant,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import OAuthAuthorization, Secret, User
from jhin_domain import SecretType, new_uuid7
from jhin_oauth.persistence import (
    MAX_HANDLE_LENGTH,
    OAuthClientStore,
    PendingAuthorizationInvalid,
    PendingAuthorizationStore,
)
from jhin_oauth.pkce import state_hash
from jhin_oauth.types import ClientCredentials
from jhin_secrets import SecretCrypto

# Re-exported so pytest sees the fixtures this module's cases ask for.
__all__ = ["Tenant", "crypto", "session", "session_factory", "tenant"]


def _as_naive(value: Any) -> Any:
    """SQLite hands back naive datetimes; Postgres does not."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


VERIFIER = "v" * 43
ISSUER = "https://auth.example.com"
REDIRECT_URI = "https://jhin.example.com/api/v1/oauth/callback"
FAKE_CLIENT_SECRET = "not-a-real-client-secret"


async def _create(
    store: PendingAuthorizationStore, tenant: Tenant, **overrides: Any
) -> tuple[OAuthAuthorization, str]:
    payload: dict[str, Any] = {
        "workspace_id": tenant.workspace_id,
        "user_id": tenant.user_id,
        "flow": "authorization_code",
        "connector_type": "mcp",
        "ttl_seconds": 600,
        "issuer": ISSUER,
        "redirect_uri": REDIRECT_URI,
        "verifier": VERIFIER,
    }
    payload.update(overrides)
    return await store.create(**payload)


# --- The handle ---------------------------------------------------------


async def test_the_raw_handle_is_never_persisted(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A database read must not hand anybody a usable ``state``."""
    store = PendingAuthorizationStore(session, crypto)
    row, handle = await _create(store, tenant)
    await session.commit()

    assert row.state_hash == state_hash(handle)
    assert handle not in row.state_hash
    stored = await session.scalar(select(OAuthAuthorization).where(OAuthAuthorization.id == row.id))
    assert stored is not None
    assert handle not in "".join(
        str(value) for value in stored.__dict__.values() if isinstance(value, str)
    )


async def test_the_handle_is_the_shape_the_callback_accepts(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    assert 1 <= len(handle) <= MAX_HANDLE_LENGTH
    assert all(character.isalnum() or character in "-_" for character in handle)


# --- Single use ---------------------------------------------------------


async def test_a_claim_consumes_the_row_exactly_once(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()

    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)
    assert claimed.consumed_at is not None

    with pytest.raises(PendingAuthorizationInvalid):
        await store.claim(handle=handle, expected_user_id=tenant.user_id)


async def test_two_concurrent_claims_produce_exactly_one_winner(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    crypto: SecretCrypto,
    tenant: Tenant,
) -> None:
    """The conditional UPDATE is the authority, not a read-then-write.

    Two callbacks arriving together — a double-clicked redirect, or a replay
    racing the real one — must produce one connection and one refusal, with
    no window between the check and the claim for the loser to slip through.
    Each claim gets its own session, because that is what two workers have.
    """
    _row, handle = await _create(PendingAuthorizationStore(session, crypto), tenant)
    await session.commit()

    async def claim() -> str:
        async with session_factory() as own:
            store = PendingAuthorizationStore(own, crypto)
            try:
                await store.claim(handle=handle, expected_user_id=tenant.user_id)
            except PendingAuthorizationInvalid:
                return "refused"
            await own.commit()
            return "claimed"

    outcomes = await asyncio.gather(claim(), claim())

    assert sorted(outcomes) == ["claimed", "refused"]


async def test_peeking_does_not_consume_the_row(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Device polling asks the same question repeatedly and must not burn it."""
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant, flow="device_code")
    await session.commit()

    for _ in range(5):
        peeked = await store.peek(handle=handle, expected_user_id=tenant.user_id)
        assert peeked.consumed_at is None

    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)
    assert claimed.consumed_at is not None
    with pytest.raises(PendingAuthorizationInvalid):
        await store.peek(handle=handle, expected_user_id=tenant.user_id)


# --- Expiry and ownership ----------------------------------------------


async def test_an_expired_row_cannot_be_claimed(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    row, handle = await _create(store, tenant)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.claim(handle=handle, expected_user_id=tenant.user_id)
    with pytest.raises(PendingAuthorizationInvalid):
        await store.peek(handle=handle, expected_user_id=tenant.user_id)


async def test_a_different_user_cannot_claim_a_valid_handle(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """State plus session binding, not state alone. This is the CSRF defense."""
    other = User(
        email=f"other-{new_uuid7().hex[:8]}@example.com",
        display_name="Someone Else",
        password_hash="x",
    )
    session.add(other)
    await session.flush()
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.claim(handle=handle, expected_user_id=other.id)


# --- Workspace and flow binding ----------------------------------------


async def test_a_handle_cannot_be_claimed_through_another_workspace(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """The row's own workspace is not the one the route was called on.

    A person who administers two workspaces could otherwise present workspace
    A's handle to workspace B's poll route: the connection is created in the
    row's workspace, serialized into B's response, and B's refresher is the
    one that gets started, so A's new connection is never swept.
    """
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant, flow="device_code")
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.claim(
            handle=handle,
            expected_user_id=tenant.user_id,
            expected_workspace_id=new_uuid7(),
        )
    # Spent by the attempt, exactly as a wrong-session claim is: whoever holds
    # the handle has used it up, and the legitimate flow starts over. The
    # device path never reaches this, because ``peek`` refuses the same
    # mismatch first, without consuming anything.
    stored = await session.scalar(
        select(OAuthAuthorization).where(OAuthAuthorization.state_hash == state_hash(handle))
    )
    assert stored is not None
    assert stored.consumed_at is not None


async def test_a_handle_cannot_be_claimed_through_another_flows_endpoint(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A device or manifest row must not survive the authorization-code path."""
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant, flow="github_app_manifest")
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.claim(
            handle=handle,
            expected_user_id=tenant.user_id,
            expected_flow="authorization_code",
        )


async def test_peeking_binds_workspace_and_flow_the_same_way_claiming_does(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Two checks that drifted apart would be a hole in whichever is laxer."""
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant, flow="device_code")
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.peek(
            handle=handle,
            expected_user_id=tenant.user_id,
            expected_workspace_id=new_uuid7(),
        )
    with pytest.raises(PendingAuthorizationInvalid):
        await store.peek(
            handle=handle,
            expected_user_id=tenant.user_id,
            expected_flow="authorization_code",
        )
    peeked = await store.peek(
        handle=handle,
        expected_user_id=tenant.user_id,
        expected_workspace_id=tenant.workspace_id,
        expected_flow="device_code",
    )
    assert peeked.consumed_at is None


# --- One refusal, one message ------------------------------------------


async def test_every_failure_raises_the_same_class_with_the_same_message(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A distinguishable failure tells a prober which guess was closest."""
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    other = User(
        email=f"other-{new_uuid7().hex[:8]}@example.com",
        display_name="Someone Else",
        password_hash="x",
    )
    session.add(other)
    await session.flush()

    messages: list[str] = []

    async def capture(coro: Any) -> None:
        with pytest.raises(PendingAuthorizationInvalid) as excinfo:
            await coro
        messages.append(str(excinfo.value))

    # 1. malformed handle
    await capture(store.claim(handle="not a handle!", expected_user_id=tenant.user_id))
    # 2. unknown handle
    await capture(store.claim(handle="z" * 43, expected_user_id=tenant.user_id))
    # 3. already consumed
    _row, spent = await _create(store, tenant)
    await session.commit()
    await store.claim(handle=spent, expected_user_id=tenant.user_id)
    await capture(store.claim(handle=spent, expected_user_id=tenant.user_id))
    # 4. expired
    expired_row, expired = await _create(store, tenant)
    expired_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await capture(store.claim(handle=expired, expected_user_id=tenant.user_id))
    # 5. wrong user
    _row2, borrowed = await _create(store, tenant)
    await session.commit()
    await capture(store.claim(handle=borrowed, expected_user_id=other.id))

    assert len(messages) == 5
    assert len(set(messages)) == 1
    assert messages[0] == PendingAuthorizationInvalid.MESSAGE


# --- The verifier -------------------------------------------------------


async def test_the_verifier_round_trips_through_real_encryption(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    row, _handle = await _create(store, tenant)
    await session.commit()

    assert row.verifier_secret_id is not None
    stored = await session.get(Secret, row.verifier_secret_id)
    assert stored is not None
    assert stored.type == SecretType.OAUTH_STATE.value
    assert VERIFIER.encode() not in stored.ciphertext
    assert await store.reveal_verifier(row) == VERIFIER


async def test_a_row_with_no_verifier_refuses_rather_than_returning_empty(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    row, _handle = await _create(store, tenant, verifier=None)
    await session.commit()

    with pytest.raises(PendingAuthorizationInvalid):
        await store.reveal_verifier(row)


# --- The draft payload --------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        {"api_key": "x"},
        {"token": "x"},
        {"client_secret": "x"},
        {"password": "x"},
        {"config": {"nested_token": "x"}},
        {"config": {"headers": [{"secret_value": "x"}]}},
    ],
)
async def test_a_credential_shaped_draft_key_is_refused(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, draft: dict[str, Any]
) -> None:
    """``draft_json`` is a plain column; nothing credential-shaped goes in it."""
    store = PendingAuthorizationStore(session, crypto)
    with pytest.raises(ValueError):
        await _create(store, tenant, draft=draft)


async def test_an_ordinary_draft_is_stored_intact(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    row, _handle = await _create(
        store,
        tenant,
        draft={"name": "Linear", "config": {"server_url": "https://mcp.linear.app/mcp"}},
    )
    assert row.draft_json["name"] == "Linear"


# --- Cleanup ------------------------------------------------------------


async def test_finishing_deletes_the_row_and_its_secret(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    row, _handle = await _create(store, tenant)
    await session.commit()
    secret_id = row.verifier_secret_id
    row_id = row.id

    await store.finish(row)
    await session.commit()

    assert await session.get(OAuthAuthorization, row_id) is None
    assert secret_id is not None
    assert await session.get(Secret, secret_id) is None


async def test_purging_expired_rows_leaves_no_orphan_ciphertext(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Opportunistic cleanup that leaves secrets behind is not cleanup."""
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    for _ in range(3):
        row, _handle = await _create(store, tenant)
        # ``retain_until`` is the garbage horizon the sweep reads; while a row
        # is pending the two columns mean the same instant.
        row.expires_at = datetime.now(UTC) - timedelta(hours=3)
        row.retain_until = row.expires_at
    fresh_row, _fresh = await _create(store, tenant)
    await session.commit()

    purged = await store.purge_expired(older_than_seconds=3600, limit=200)
    await session.commit()

    assert purged == 3
    remaining_rows = await session.scalar(
        select(func.count()).select_from(select(OAuthAuthorization).subquery())
    )
    assert remaining_rows == 1
    remaining_secrets = await session.scalar(
        select(func.count()).select_from(
            select(Secret).where(Secret.type == SecretType.OAUTH_STATE.value).subquery()
        )
    )
    assert remaining_secrets == 1
    assert fresh_row.verifier_secret_id is not None


async def test_purging_leaves_rows_that_are_merely_expired_but_recent(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A row that expired a minute ago may still be explaining itself."""
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    row, _handle = await _create(store, tenant)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    row.retain_until = row.expires_at
    await session.commit()

    assert await store.purge_expired(older_than_seconds=3600) == 0


# --- Receipts -----------------------------------------------------------


async def test_settling_a_row_destroys_its_verifier_and_keeps_only_a_constant(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A receipt is a projection of what the owning session already sees."""
    from datetime import UTC, datetime

    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()
    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)
    secret_id = claimed.verifier_secret_id

    await store.settle(claimed, outcome="denied", connection_id=None, receipt_ttl_seconds=600)
    await session.commit()

    assert claimed.outcome == "denied"
    assert claimed.verifier_secret_id is None
    assert claimed.draft_json == {}
    assert claimed.connection_id is None
    assert claimed.outcome_connection_id is None
    assert _as_naive(claimed.retain_until) > _as_naive(datetime.now(UTC))
    assert await session.get(Secret, secret_id) is None


async def test_settling_with_no_receipt_window_deletes_the_row(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """``0`` disables receipts, and every repeat gets the uniform refusal."""
    store = PendingAuthorizationStore(session, crypto)
    row, handle = await _create(store, tenant)
    row_id = row.id
    await session.commit()
    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)

    await store.settle(claimed, outcome="denied", connection_id=None, receipt_ttl_seconds=0)
    await session.commit()

    assert await session.get(OAuthAuthorization, row_id) is None


async def test_a_receipt_is_recalled_only_by_the_session_that_could_have_finished(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Bound by exactly what ``claim`` binds by: the handle, the user, the flow."""
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()
    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)
    await store.settle(claimed, outcome="denied", connection_id=None, receipt_ttl_seconds=600)
    await session.commit()

    assert (
        await store.recall(
            handle=handle, expected_user_id=tenant.user_id, expected_flow="authorization_code"
        )
        is not None
    )
    assert (
        await store.recall(
            handle=handle, expected_user_id=new_uuid7(), expected_flow="authorization_code"
        )
        is None
    )
    assert (
        await store.recall(
            handle=handle, expected_user_id=tenant.user_id, expected_flow="device_code"
        )
        is None
    )
    assert (
        await store.recall(
            handle="not a handle!",
            expected_user_id=tenant.user_id,
            expected_flow="authorization_code",
        )
        is None
    )


async def test_a_pending_row_has_no_receipt_to_recall(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()

    assert (
        await store.recall(
            handle=handle, expected_user_id=tenant.user_id, expected_flow="authorization_code"
        )
        is None
    )


async def test_a_receipt_stops_being_readable_at_its_horizon_and_reading_never_extends_it(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Not a sliding window: a replay is a read, and reads change nothing."""
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    _row, handle = await _create(store, tenant)
    await session.commit()
    claimed = await store.claim(handle=handle, expected_user_id=tenant.user_id)
    await store.settle(claimed, outcome="denied", connection_id=None, receipt_ttl_seconds=600)
    await session.commit()

    before = (claimed.consumed_at, claimed.retain_until, claimed.outcome)
    recalled = await store.recall(
        handle=handle, expected_user_id=tenant.user_id, expected_flow="authorization_code"
    )
    assert recalled is not None
    assert (recalled.consumed_at, recalled.retain_until, recalled.outcome) == before

    claimed.retain_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert (
        await store.recall(
            handle=handle, expected_user_id=tenant.user_id, expected_flow="authorization_code"
        )
        is None
    )


async def test_purging_keeps_a_live_receipt_and_removes_an_aged_one(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A sweep must not take a receipt out from under a refresh in flight."""
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)
    _live, live_handle = await _create(store, tenant)
    await session.commit()
    live = await store.claim(handle=live_handle, expected_user_id=tenant.user_id)
    await store.settle(live, outcome="denied", connection_id=None, receipt_ttl_seconds=600)

    _aged, aged_handle = await _create(store, tenant)
    await session.commit()
    aged = await store.claim(handle=aged_handle, expected_user_id=tenant.user_id)
    await store.settle(aged, outcome="denied", connection_id=None, receipt_ttl_seconds=600)
    aged.retain_until = datetime.now(UTC) - timedelta(hours=3)
    await session.commit()

    assert await store.purge_expired(older_than_seconds=3600) == 1
    await session.commit()
    assert await session.get(OAuthAuthorization, live.id) is not None


async def test_a_failing_diagnostic_degrades_to_state_unknown(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that only ever feeds a log line must never cost a response."""
    store = PendingAuthorizationStore(session, crypto)

    async def exploding(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the diagnostic query fell over")

    monkeypatch.setattr(store._session, "scalar", exploding)
    assert await store._diagnose("a" * 43) == "state_unknown"


async def test_every_claim_refusal_carries_a_reason_and_the_same_message(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """The reason is for the log. The message is the constant everybody gets.

    This is the test that stops the reason leaking into the message later:
    the whole design rests on the browser being told one thing while the
    server records another.
    """
    from datetime import UTC, datetime, timedelta

    store = PendingAuthorizationStore(session, crypto)

    async def refusal(**kwargs: Any) -> str:
        try:
            await store.claim(**kwargs)
        except PendingAuthorizationInvalid as exc:
            assert str(exc) == PendingAuthorizationInvalid.MESSAGE
            return exc.reason
        raise AssertionError("the claim was not refused")

    assert (
        await refusal(handle="not a handle!", expected_user_id=tenant.user_id) == "state_malformed"
    )
    assert await refusal(handle="a" * 43, expected_user_id=tenant.user_id) == "state_unknown"

    expired, expired_handle = await _create(store, tenant)
    expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    assert await refusal(handle=expired_handle, expected_user_id=tenant.user_id) == "state_expired"

    spent, spent_handle = await _create(store, tenant)
    spent.consumed_at = datetime.now(UTC)
    await session.commit()
    assert await refusal(handle=spent_handle, expected_user_id=tenant.user_id) == "state_consumed"

    _flow_row, flow_handle = await _create(store, tenant, flow="device_code")
    await session.commit()
    assert (
        await refusal(
            handle=flow_handle,
            expected_user_id=tenant.user_id,
            expected_flow="authorization_code",
        )
        == "wrong_flow"
    )
    await session.rollback()

    _ws_row, ws_handle = await _create(store, tenant)
    await session.commit()
    assert (
        await refusal(
            handle=ws_handle,
            expected_user_id=tenant.user_id,
            expected_workspace_id=new_uuid7(),
        )
        == "wrong_workspace"
    )
    await session.rollback()

    _other_row, other_handle = await _create(store, tenant)
    await session.commit()
    assert await refusal(handle=other_handle, expected_user_id=new_uuid7()) == "wrong_user"
    await session.rollback()


# --- Client registrations -----------------------------------------------


async def test_a_registration_is_keyed_by_workspace_issuer_and_redirect_uri(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Changing the redirect URI must force a fresh registration, not reuse."""
    store = OAuthClientStore(session, crypto)
    credentials = ClientCredentials(client_id="id-a")
    await store.save(
        workspace_id=tenant.workspace_id,
        issuer=ISSUER,
        redirect_uri=REDIRECT_URI,
        credentials=credentials,
        scopes="read",
        source="dcr",
        created_by_user_id=tenant.user_id,
    )
    await session.commit()

    assert (
        await store.get(tenant.workspace_id, issuer=ISSUER, redirect_uri=REDIRECT_URI)
    ) is not None
    assert (
        await store.get(
            tenant.workspace_id, issuer=ISSUER, redirect_uri="https://moved.example.com/cb"
        )
    ) is None
    assert (
        await store.get(
            tenant.workspace_id, issuer="https://other.example.com", redirect_uri=REDIRECT_URI
        )
    ) is None


async def test_a_client_secret_round_trips_and_is_never_stored_in_the_clear(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = OAuthClientStore(session, crypto)
    row = await store.save(
        workspace_id=tenant.workspace_id,
        issuer=ISSUER,
        redirect_uri=REDIRECT_URI,
        credentials=ClientCredentials(
            client_id="id",
            client_secret=FAKE_CLIENT_SECRET,
            token_endpoint_auth_method="client_secret_post",
        ),
        scopes="",
        source="manual",
        created_by_user_id=tenant.user_id,
    )
    await session.commit()

    assert row.client_secret_id is not None
    stored = await session.get(Secret, row.client_secret_id)
    assert stored is not None
    assert FAKE_CLIENT_SECRET.encode() not in stored.ciphertext

    found = await store.get(tenant.workspace_id, issuer=ISSUER, redirect_uri=REDIRECT_URI)
    assert found is not None
    assert found[1].client_secret == FAKE_CLIENT_SECRET


async def test_forgetting_a_registration_removes_every_secret_it_owned(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    store = OAuthClientStore(session, crypto)
    row = await store.save(
        workspace_id=tenant.workspace_id,
        issuer=ISSUER,
        redirect_uri=REDIRECT_URI,
        credentials=ClientCredentials(
            client_id="id",
            client_secret=FAKE_CLIENT_SECRET,
            token_endpoint_auth_method="client_secret_post",
            registration_access_token="not-a-real-registration-token",
            registration_client_uri=f"{ISSUER}/register/id",
        ),
        scopes="",
        source="dcr",
        created_by_user_id=tenant.user_id,
    )
    await session.commit()
    registration_id = row.id

    await store.forget(tenant.workspace_id, registration_id)
    await session.commit()

    held = await session.scalar(
        select(func.count()).select_from(
            select(Secret).where(Secret.type == SecretType.OAUTH_CLIENT.value).subquery()
        )
    )
    assert held == 0


async def test_a_registration_from_another_workspace_is_not_readable(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """Workspaces are the tenancy boundary here as everywhere else."""
    from jhin_db.models import Workspace

    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    store = OAuthClientStore(session, crypto)
    row = await store.save(
        workspace_id=tenant.workspace_id,
        issuer=ISSUER,
        redirect_uri=REDIRECT_URI,
        credentials=ClientCredentials(client_id="id"),
        scopes="",
        source="dcr",
        created_by_user_id=tenant.user_id,
    )
    await session.commit()

    assert await store.get(other.id, issuer=ISSUER, redirect_uri=REDIRECT_URI) is None
    with pytest.raises(LookupError):
        await store.get_by_id(other.id, row.id)
