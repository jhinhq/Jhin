"""Token storage and refresh-ahead: the shape on disk and the moment to renew.

An OAuth token set is an ordinary connection credential — the same flat
``string -> string`` map a pasted API key uses — and that constraint is not
cosmetic: ``decode_string_secret_map`` refuses anything else, so a timestamp
written as a number would make the credential unreadable the next time
anybody needed it.

The renewal timing is the other half. A token renewed too late dies inside a
tool call; renewed too eagerly it burns a rotating provider's refresh token
for nothing. The boundary is asserted here rather than trusted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import Connection, Secret
from jhin_domain import ConnectionStatus, SecretType, new_uuid7
from jhin_oauth.lifecycle import (
    ACCESS_TOKEN_KEY,
    EXPIRES_AT_KEY,
    REFRESH_MARGIN_SECONDS,
    REFRESH_TOKEN_KEY,
    ConnectionTokenService,
    StoredTokens,
    needs_refresh,
    parse_token_map,
    token_map,
)
from jhin_oauth.types import TokenResponse
from jhin_secrets import SecretCrypto, decode_string_secret_map

# Re-exported so pytest sees the fixtures this module's cases ask for.
__all__ = ["Tenant", "crypto", "session", "session_factory", "tenant"]

ACCESS = "fake-access-token-for-tests"
REFRESH = "fake-refresh-token-for-tests"
ISSUER = "https://auth.example.com"
RESOURCE = "https://mcp.example.com"


def _tokens(**overrides: object) -> TokenResponse:
    payload: dict[str, object] = {
        "access_token": ACCESS,
        "token_type": "Bearer",
        "refresh_token": REFRESH,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "scope": "read write",
        "issuer": ISSUER,
    }
    payload.update(overrides)
    return TokenResponse(**payload)  # type: ignore[arg-type]


async def _connection(session: AsyncSession, tenant: Tenant, **overrides: object) -> Connection:
    connection = Connection(
        workspace_id=tenant.workspace_id,
        connector_type="mcp",
        name="Example MCP",
        auth_type="oauth",
        config_json={"server_url": "https://mcp.example.com/mcp"},
        **overrides,  # type: ignore[arg-type]
    )
    session.add(connection)
    await session.flush()
    return connection


# --- The map on disk ----------------------------------------------------


def test_the_token_map_is_strings_all_the_way_down() -> None:
    """``decode_string_secret_map`` refuses anything else, so this is load-bearing."""
    mapped = token_map(_tokens())
    assert all(isinstance(key, str) for key in mapped)
    assert all(isinstance(value, str) for value in mapped.values())
    assert decode_string_secret_map(json.dumps(mapped)) == mapped


def test_absent_values_are_omitted_rather_than_written_as_none() -> None:
    """A literal ``"None"`` refresh token looks like a refresh token."""
    mapped = token_map(_tokens(refresh_token=None, expires_at=None))
    assert REFRESH_TOKEN_KEY not in mapped
    assert EXPIRES_AT_KEY not in mapped
    assert "None" not in mapped.values()


def test_timestamps_are_iso_8601_utc_strings() -> None:
    expires = datetime(2026, 8, 29, 18, 4, 11, tzinfo=UTC)
    assert token_map(_tokens(expires_at=expires))[EXPIRES_AT_KEY] == "2026-08-29T18:04:11Z"


def test_a_map_round_trips_through_parse_token_map() -> None:
    original = _tokens()
    parsed = parse_token_map(token_map(original))
    assert isinstance(parsed, StoredTokens)
    assert parsed.access_token == ACCESS
    assert parsed.refresh_token == REFRESH
    assert parsed.expires_at is not None
    assert parsed.expires_at.tzinfo is not None


def test_a_map_with_no_access_token_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_token_map({"token_type": "Bearer"})


def test_a_malformed_timestamp_is_refused_without_echoing_the_value() -> None:
    """This runs on decrypted material; an exception is a thing that gets logged."""
    marker = "not-a-timestamp-and-also-secret"
    with pytest.raises(ValueError) as excinfo:
        parse_token_map({ACCESS_TOKEN_KEY: ACCESS, EXPIRES_AT_KEY: marker})
    assert marker not in str(excinfo.value)


def test_no_exception_message_ever_carries_the_access_token() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_token_map({ACCESS_TOKEN_KEY: ACCESS, EXPIRES_AT_KEY: "nope"})
    assert ACCESS not in str(excinfo.value)


# --- When to renew ------------------------------------------------------


def test_a_token_with_no_expiry_never_needs_renewing() -> None:
    assert needs_refresh(None, now=datetime.now(UTC)) is False


def test_the_margin_boundary_is_exactly_two_minutes() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    expires = now + timedelta(seconds=REFRESH_MARGIN_SECONDS)
    assert needs_refresh(expires, now=now) is True
    assert needs_refresh(expires + timedelta(seconds=1), now=now) is False


def test_a_short_lived_token_renews_at_half_its_lifetime() -> None:
    """Two minutes of margin on a five-minute token is most of the token."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    lifetime = 3600
    expires = now + timedelta(seconds=lifetime // 2)
    assert needs_refresh(expires, now=now, original_lifetime_seconds=lifetime) is True
    assert (
        needs_refresh(expires + timedelta(seconds=1), now=now, original_lifetime_seconds=lifetime)
        is False
    )


def test_an_expired_token_always_needs_renewing() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    assert needs_refresh(now - timedelta(hours=1), now=now) is True


# --- Storing ------------------------------------------------------------


async def test_storing_tokens_writes_the_secret_and_every_column_at_once(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A row that says active and a secret holding a dead token are one lie."""
    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())
    refresh_expires = datetime.now(UTC) + timedelta(days=30)

    await service.store_tokens(
        connection,
        _tokens(refresh_expires_at=refresh_expires),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )

    assert connection.status == ConnectionStatus.ACTIVE.value
    assert connection.last_error is None
    assert connection.oauth_issuer == ISSUER
    assert connection.oauth_resource == RESOURCE
    assert connection.oauth_scope == "read write"
    assert connection.oauth_expires_at is not None
    assert connection.oauth_refresh_expires_at is not None
    assert connection.oauth_last_refresh_at is not None
    assert connection.oauth_refresh_failures == 0
    assert connection.oauth_authorized_by_user_id == tenant.user_id
    assert connection.encrypted_secret_id is not None


async def test_the_stored_credential_is_encrypted_and_typed_as_connection_credentials(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """An OAuth token set *is* connection credentials; no new secret type."""
    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())

    await service.store_tokens(
        connection,
        _tokens(),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )

    assert connection.encrypted_secret_id is not None
    stored = await session.get(Secret, connection.encrypted_secret_id)
    assert stored is not None
    assert stored.type == SecretType.CONNECTION_CREDENTIALS.value
    assert ACCESS.encode() not in stored.ciphertext
    assert REFRESH.encode() not in stored.ciphertext


async def test_storing_again_rotates_the_same_secret_rather_than_making_another(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())
    await service.store_tokens(
        connection,
        _tokens(),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )
    first_secret_id = connection.encrypted_secret_id

    await service.store_tokens(
        connection,
        _tokens(access_token=f"{ACCESS}-2"),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=None,
    )

    assert connection.encrypted_secret_id == first_secret_id
    held = list(
        await session.scalars(select(Secret).where(Secret.workspace_id == tenant.workspace_id))
    )
    assert len(held) == 1


async def test_an_active_token_is_returned_without_a_refresh_attempt(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """No network client is even needed when the token is nowhere near stale."""
    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())
    await service.store_tokens(
        connection,
        _tokens(),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )

    assert await service.access_token(connection) == ACCESS


async def test_a_connection_marked_needs_reauth_refuses_by_name(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """The agent gets one sentence naming the app and the fix. No token, no URL."""
    from jhin_oauth.lifecycle import ConnectionNeedsReauthError

    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())
    await service.store_tokens(
        connection,
        _tokens(),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )
    await service.mark_needs_reauth(connection, "gone")

    with pytest.raises(ConnectionNeedsReauthError) as excinfo:
        await service.access_token(connection)

    message = str(excinfo.value)
    assert "Example MCP" in message
    assert ACCESS not in message
    assert REFRESH not in message
    assert "http" not in message


async def test_marking_needs_reauth_records_the_reason_and_clears_the_counter(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    connection = await _connection(session, tenant, oauth_refresh_failures=4)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())

    await service.mark_needs_reauth(connection, "The provider rejected it.")

    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    assert connection.last_error == "The provider rejected it."
    assert connection.oauth_refresh_failures == 0


async def test_revoking_clears_the_local_secret_even_with_no_provider_to_call(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    crypto: SecretCrypto,
    tenant: Tenant,
) -> None:
    """Our copy going away is the part actually under our control."""
    connection = await _connection(session, tenant)
    async with httpx.AsyncClient() as client:
        service = ConnectionTokenService(session, crypto, client)
        await service.store_tokens(
            connection,
            _tokens(),
            registration_id=None,
            resource=RESOURCE,
            issuer=ISSUER,
            authorized_by_user_id=tenant.user_id,
        )
        secret_id = connection.encrypted_secret_id
        await service.revoke_and_clear(connection)

    assert connection.encrypted_secret_id is None
    assert secret_id is not None
    assert await session.get(Secret, secret_id) is None


# --- The refusal the tool path gives ------------------------------------


async def test_resolving_a_connection_that_needs_reauth_refuses_by_name(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant
) -> None:
    """A dead grant fails at resolution, not half-way through a provider call.

    ``ConnectionNeedsReauthError`` sits on the existing refusal path, so every
    caller that already handles a resolution failure handles this one — and
    the message is a sentence an agent can repeat: it names the app and the
    fix, and carries no token, no URL, and no provider error text.
    """
    from jhin_connectors.execution import (
        ConnectionNeedsReauthError,
        ConnectionResolutionError,
        resolve_connection,
    )
    from jhin_tools.builtin import ToolExecutionContext

    connection = await _connection(session, tenant)
    service = ConnectionTokenService(session, crypto, httpx.AsyncClient())
    await service.store_tokens(
        connection,
        _tokens(),
        registration_id=None,
        resource=RESOURCE,
        issuer=ISSUER,
        authorized_by_user_id=tenant.user_id,
    )
    await service.mark_needs_reauth(connection, "revoked")

    ctx = ToolExecutionContext(
        session=session,
        workspace_id=tenant.workspace_id,
        task_id=new_uuid7(),
        run_id=new_uuid7(),
        agent_id=new_uuid7(),
        agent_name="Tester",
        crypto=crypto,
    )
    with pytest.raises(ConnectionNeedsReauthError) as excinfo:
        await resolve_connection(ctx, connection.id, connector_type="mcp")

    assert issubclass(ConnectionNeedsReauthError, ConnectionResolutionError)
    message = str(excinfo.value)
    assert "Example MCP" in message
    assert "reconnect" in message.lower()
    assert ACCESS not in message
    assert REFRESH not in message
