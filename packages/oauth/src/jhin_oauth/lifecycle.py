"""OAuth tokens as an ordinary connection credential, and the machinery that
keeps them alive (``docs/architecture/oauth.md``).

There is no new crypto here and no new storage. An OAuth token set is written
through the same ``SecretStore`` a pasted API key goes through, as the same
flat ``string -> string`` JSON map every connection already stores — which is
why every timestamp in it is an ISO-8601 string and never a number. What is
new is *lifecycle*: knowing when a token is about to go stale, renewing it
without two workers fighting over the same rotating refresh token, and being
honest, loudly and early, when renewal is no longer possible.

Two independent paths do the renewing, and both are required. On use, a tool
worker calls :meth:`ConnectionTokenService.access_token`, which takes a row
lock before it looks at the clock — the lock, not a cache, is what stops two
workers from each rotating the refresh token and invalidating the other's.
Proactively, a Temporal workflow per workspace sweeps connections whose access
token expires soon, so a connection nobody has used all week is still usable
the moment somebody does.

Failure is classified, not retried blindly. ``invalid_grant`` means a human
revoked us and no amount of retrying will change that; a 503 means the
provider is having a bad minute. Treating the first like the second is how an
OAuth client gets its whole instance rate-limited.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_db.models import Connection
from jhin_domain import ConnectionStatus, SecretType
from jhin_oauth.errors import (
    ClientForgottenError,
    InvalidGrantError,
    OAuthError,
    TokenError,
    TransientOAuthError,
)
from jhin_oauth.persistence import OAuthClientStore
from jhin_oauth.tokens import refresh_access_token, revoke_token
from jhin_oauth.types import AuthorizationServerMetadata, ClientCredentials, TokenResponse
from jhin_oauth.urls import validate_oauth_url
from jhin_secrets import SecretCrypto, SecretStore, decode_string_secret_map
from jhin_secrets.material import SecretMaterialError
from jhin_secrets.store import SecretNotFoundError

# --- The credential map's keys. One place, so a typo cannot silently drop a
# --- refresh token into a field nothing reads.
ACCESS_TOKEN_KEY: Final = "access_token"
REFRESH_TOKEN_KEY: Final = "refresh_token"
TOKEN_TYPE_KEY: Final = "token_type"
EXPIRES_AT_KEY: Final = "expires_at"
REFRESH_EXPIRES_AT_KEY: Final = "refresh_expires_at"
SCOPE_KEY: Final = "scope"
ISSUER_KEY: Final = "issuer"

#: Where the non-secret half of an OAuth grant lives on ``config_json``. The
#: token endpoint is stored rather than rediscovered so a refresh needs no
#: network round trip before the one that matters — and it is re-validated
#: against the SSRF policy at every use, so tightening the policy takes effect
#: without anybody rewriting rows.
CONFIG_ISSUER_KEY: Final = "oauth_issuer"
CONFIG_RESOURCE_KEY: Final = "oauth_resource"
CONFIG_SCOPE_KEY: Final = "oauth_scope"
CONFIG_TOKEN_ENDPOINT_KEY: Final = "oauth_token_endpoint"
CONFIG_REVOCATION_ENDPOINT_KEY: Final = "oauth_revocation_endpoint"

#: Renew this long before expiry at the earliest, and at half the token's
#: original lifetime for short-lived tokens — an hour-long token is renewed
#: with thirty minutes to spare, a five-minute one with two.
REFRESH_MARGIN_SECONDS: Final[int] = 120
REFRESH_FRACTION: Final[float] = 0.5
#: Consecutive *transient* failures before we stop calling it transient. Five
#: sweeps at five-minute spacing is close to half an hour of a provider being
#: unreachable, which is long enough to be a real problem worth showing.
MAX_CONSECUTIVE_REFRESH_FAILURES: Final[int] = 5
#: How far ahead the proactive sweep looks. Comfortably more than one sweep
#: interval, so a token is picked up by two sweeps before it ever expires.
REFRESH_HORIZON_SECONDS: Final[int] = 900

#: Provider error codes that mean the grant is gone. Retrying these does not
#: recover access; it only trips the provider's abuse detection.
TERMINAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client"}
)

# --- Messages. A fixed vocabulary, Jhin-authored, never provider text. These
# --- reach models as tool errors and people as banner copy, so they say what
# --- happened and what to do, and nothing about tokens.
NEEDS_REAUTH_TOOL_MESSAGE: Final = (
    "This app needs to be reconnected. Ask an admin to reconnect {name} in Apps."
)
REVOKED_MESSAGE: Final = (
    "The provider rejected the saved permission for this app. Reconnect it to grant access again."
)
CLIENT_REJECTED_MESSAGE: Final = (
    "The provider no longer recognises this app's credentials. An admin needs to re-enter them."
)
REFRESH_EXPIRED_MESSAGE: Final = (
    "The saved permission for this app has expired. Reconnect it to grant access again."
)
NO_REFRESH_TOKEN_MESSAGE: Final = (
    "This app was connected without a way to renew its access. Reconnect it."
)
UNRENEWABLE_CONFIG_MESSAGE: Final = (
    "Jhin's saved sign-in details for this app are incomplete. Reconnect it."
)
UNREACHABLE_MESSAGE: Final = (
    "The provider could not be reached to renew this app's access. Reconnect it if this persists."
)
SAVE_FAILED_MESSAGE: Final = "Reconnecting is required: the access token could not be saved."
REFRESHED_MESSAGE: Final = "Access renewed."
NOT_DUE_MESSAGE: Final = "Access is still current."
TRANSIENT_MESSAGE: Final = "The provider could not be reached; will try again."


class ConnectionNeedsReauthError(RuntimeError):
    """Raised by connection resolution for a connection in ``needs_reauth``.

    Carries a sentence an agent can say out loud: no token, no URL, no
    provider error string, and a named next step for whoever reads it.
    """


@dataclass(frozen=True, slots=True)
class StoredTokens:
    """One decrypted OAuth token set, with its timestamps already parsed."""

    access_token: str
    token_type: str
    refresh_token: str | None
    expires_at: datetime | None
    refresh_expires_at: datetime | None
    scope: str
    issuer: str


def _parse_timestamp(raw: str) -> datetime:
    """ISO-8601 UTC in, aware datetime out. Never echoes the value it read."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("stored OAuth credential has a malformed timestamp") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_token_map(credentials: Mapping[str, str]) -> StoredTokens:
    """Read a stored credential map into a token set.

    Raises ``ValueError`` when there is no access token or a timestamp is not
    ISO-8601. The message never contains a value from the map — this runs on
    decrypted credential material, and an exception is a thing that gets
    logged.
    """
    access_token = credentials.get(ACCESS_TOKEN_KEY, "")
    if not access_token:
        raise ValueError("stored OAuth credential has no access token")
    expires_raw = credentials.get(EXPIRES_AT_KEY, "")
    refresh_expires_raw = credentials.get(REFRESH_EXPIRES_AT_KEY, "")
    return StoredTokens(
        access_token=access_token,
        token_type=credentials.get(TOKEN_TYPE_KEY) or "Bearer",
        refresh_token=credentials.get(REFRESH_TOKEN_KEY) or None,
        expires_at=_parse_timestamp(expires_raw) if expires_raw else None,
        refresh_expires_at=(_parse_timestamp(refresh_expires_raw) if refresh_expires_raw else None),
        scope=credentials.get(SCOPE_KEY, ""),
        issuer=credentials.get(ISSUER_KEY, ""),
    )


def token_map(tokens: TokenResponse) -> dict[str, str]:
    """Flatten a token response for ``SecretStore``.

    Absent values are omitted rather than written as ``"None"``:
    ``decode_string_secret_map`` demands strings, and a literal ``"None"``
    refresh token would look like a refresh token to every reader.
    """
    payload = {
        ACCESS_TOKEN_KEY: tokens.access_token,
        TOKEN_TYPE_KEY: tokens.token_type or "Bearer",
        SCOPE_KEY: tokens.scope,
        ISSUER_KEY: tokens.issuer,
    }
    if tokens.refresh_token:
        payload[REFRESH_TOKEN_KEY] = tokens.refresh_token
    if tokens.expires_at is not None:
        payload[EXPIRES_AT_KEY] = _format_timestamp(tokens.expires_at)
    if tokens.refresh_expires_at is not None:
        payload[REFRESH_EXPIRES_AT_KEY] = _format_timestamp(tokens.refresh_expires_at)
    return {key: value for key, value in payload.items() if value}


def needs_refresh(
    expires_at: datetime | None,
    *,
    now: datetime,
    original_lifetime_seconds: int | None = None,
) -> bool:
    """Whether this access token is close enough to expiry to renew now.

    A token with no expiry never needs renewing. Everything else is renewed
    once it is inside ``max(120s, half its original lifetime)`` of expiry, so
    a long-running tool call cannot start with a token that dies mid-flight.
    """
    if expires_at is None:
        return False
    margin = float(REFRESH_MARGIN_SECONDS)
    if original_lifetime_seconds is not None and original_lifetime_seconds > 0:
        margin = max(margin, original_lifetime_seconds * REFRESH_FRACTION)
    return now >= expires_at - timedelta(seconds=margin)


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one refresh attempt did, in terms a caller can act on."""

    refreshed: bool
    terminal: bool
    message: str


@dataclass(frozen=True, slots=True)
class RefreshSweepResult:
    """One proactive sweep's tally, used by the workflow to decide to stop."""

    considered: int
    refreshed: int
    needs_reauth: int
    transient_failures: int
    remaining_oauth_connections: int


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _original_lifetime_seconds(connection: Connection, expires_at: datetime | None) -> int | None:
    """How long the current access token was minted for, as best we can tell."""
    if expires_at is None:
        return None
    # ``created_at`` is never null, so there is always some issue instant to
    # measure the lifetime from — a connection authorized before the first
    # refresh simply measures from when it was created.
    issued = connection.oauth_last_refresh_at or connection.created_at
    seconds = int((expires_at - _as_utc(issued)).total_seconds())
    return seconds if seconds > 0 else None


class ConnectionTokenService:
    """Read, renew, and retire one workspace's OAuth connection tokens."""

    def __init__(
        self, session: AsyncSession, crypto: SecretCrypto, http_client: httpx.AsyncClient
    ) -> None:
        self._session = session
        self._crypto = crypto
        self._http = http_client
        self._secrets = SecretStore(session, crypto)
        self._clients = OAuthClientStore(session, crypto)

    # --- Reading -------------------------------------------------------

    async def access_token(self, connection: Connection) -> str:
        """A usable access token, renewing first if this one is nearly stale.

        The row lock comes *before* the staleness check, and that ordering is
        the whole point. Two tool workers reaching this line together would
        otherwise both see a stale token, both call the token endpoint, and —
        against any provider that rotates refresh tokens — the second would
        invalidate the first's. With the lock, the second worker waits, then
        re-reads and finds the token the first one just saved.
        """
        if connection.status == ConnectionStatus.NEEDS_REAUTH.value:
            raise ConnectionNeedsReauthError(NEEDS_REAUTH_TOOL_MESSAGE.format(name=connection.name))
        locked = await self._lock(connection)
        tokens = await self._read_tokens(locked)
        if not needs_refresh(
            tokens.expires_at,
            now=datetime.now(UTC),
            original_lifetime_seconds=_original_lifetime_seconds(locked, tokens.expires_at),
        ):
            return tokens.access_token
        outcome = await self.refresh(locked)
        if outcome.terminal:
            raise ConnectionNeedsReauthError(NEEDS_REAUTH_TOOL_MESSAGE.format(name=locked.name))
        if not outcome.refreshed:
            raise TransientOAuthError(UNREACHABLE_MESSAGE)
        return (await self._read_tokens(locked)).access_token

    async def _lock(self, connection: Connection) -> Connection:
        """Re-read this connection under a row lock, so refresh is single-flight."""
        locked = await self._session.scalar(
            select(Connection)
            .where(Connection.id == connection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return locked if locked is not None else connection

    async def _read_tokens(self, connection: Connection) -> StoredTokens:
        if connection.encrypted_secret_id is None:
            raise ConnectionNeedsReauthError(NEEDS_REAUTH_TOOL_MESSAGE.format(name=connection.name))
        try:
            plaintext = await self._secrets.reveal(
                connection.workspace_id, connection.encrypted_secret_id
            )
            return parse_token_map(decode_string_secret_map(plaintext))
        except (SecretNotFoundError, SecretMaterialError, ValueError) as exc:
            raise ConnectionNeedsReauthError(
                NEEDS_REAUTH_TOOL_MESSAGE.format(name=connection.name)
            ) from exc

    # --- Writing -------------------------------------------------------

    async def store_tokens(
        self,
        connection: Connection,
        tokens: TokenResponse,
        *,
        registration_id: UUID | None,
        resource: str,
        issuer: str,
        authorized_by_user_id: UUID | None,
    ) -> None:
        """Persist a freshly issued token set and everything true about it.

        The secret and every connection column describing the grant move in
        one transaction. A row that claims to be active and a secret that
        holds a dead token are the two halves of the same lie, so they are
        never written apart.
        """
        plaintext = json.dumps(token_map(tokens))
        if connection.encrypted_secret_id is None:
            secret = await self._secrets.create(
                workspace_id=connection.workspace_id,
                name=f"connection/{connection.public_id}/credentials",
                plaintext=plaintext,
                secret_type=SecretType.CONNECTION_CREDENTIALS,
                created_by_user_id=authorized_by_user_id,
            )
            connection.encrypted_secret_id = secret.id
        else:
            await self._secrets.rotate(
                connection.workspace_id, connection.encrypted_secret_id, plaintext
            )
        connection.status = ConnectionStatus.ACTIVE.value
        connection.last_error = None
        connection.oauth_client_registration_id = registration_id
        connection.oauth_issuer = issuer or None
        connection.oauth_resource = resource or None
        connection.oauth_scope = tokens.scope or None
        connection.oauth_expires_at = tokens.expires_at
        connection.oauth_refresh_expires_at = tokens.refresh_expires_at
        connection.oauth_last_refresh_at = datetime.now(UTC)
        connection.oauth_refresh_failures = 0
        if authorized_by_user_id is not None:
            connection.oauth_authorized_by_user_id = authorized_by_user_id
        await self._session.commit()

    async def mark_needs_reauth(self, connection: Connection, message: str) -> None:
        """Say so on the row, in one committed write, and stop pretending."""
        connection.status = ConnectionStatus.NEEDS_REAUTH.value
        connection.last_error = message
        connection.oauth_refresh_failures = 0
        await self._session.commit()

    # --- Renewing ------------------------------------------------------

    async def refresh(self, connection: Connection) -> RefreshOutcome:
        """Renew one connection's access token, persisting the rotation atomically."""
        now = datetime.now(UTC)
        if connection.oauth_refresh_expires_at is not None and now >= _as_utc(
            connection.oauth_refresh_expires_at
        ):
            # Pre-emptive: the refresh token is past its own expiry, so the
            # request would fail and count against us for nothing.
            await self.mark_needs_reauth(connection, REFRESH_EXPIRED_MESSAGE)
            return RefreshOutcome(False, True, REFRESH_EXPIRED_MESSAGE)

        try:
            tokens = await self._read_tokens(connection)
        except ConnectionNeedsReauthError:
            await self.mark_needs_reauth(connection, UNRENEWABLE_CONFIG_MESSAGE)
            return RefreshOutcome(False, True, UNRENEWABLE_CONFIG_MESSAGE)
        if not tokens.refresh_token:
            await self.mark_needs_reauth(connection, NO_REFRESH_TOKEN_MESSAGE)
            return RefreshOutcome(False, True, NO_REFRESH_TOKEN_MESSAGE)

        prepared = await self._prepare_refresh(connection)
        if prepared is None:
            await self.mark_needs_reauth(connection, UNRENEWABLE_CONFIG_MESSAGE)
            return RefreshOutcome(False, True, UNRENEWABLE_CONFIG_MESSAGE)
        metadata, registration_id, credentials, source = prepared

        try:
            renewed = await refresh_access_token(
                self._http,
                metadata,
                credentials=credentials,
                refresh_token=tokens.refresh_token,
                resource=connection.oauth_resource or "",
                scope=connection.oauth_scope or None,
            )
        except ClientForgottenError:
            # The client id itself is gone. Re-registering would mint a new
            # client, and a new client cannot inherit this user's grant — so
            # the honest outcome is "somebody has to authorize again", and
            # the dead DCR registration is cleared out of the way first.
            if source == "dcr":
                with contextlib.suppress(Exception):
                    await self._clients.forget(connection.workspace_id, registration_id)
                    connection.oauth_client_registration_id = None
            await self.mark_needs_reauth(connection, CLIENT_REJECTED_MESSAGE)
            return RefreshOutcome(False, True, CLIENT_REJECTED_MESSAGE)
        except InvalidGrantError:
            await self.mark_needs_reauth(connection, REVOKED_MESSAGE)
            return RefreshOutcome(False, True, REVOKED_MESSAGE)
        except TokenError as exc:
            if exc.error_code in TERMINAL_ERROR_CODES:
                await self.mark_needs_reauth(connection, REVOKED_MESSAGE)
                return RefreshOutcome(False, True, REVOKED_MESSAGE)
            return await self._record_transient(connection)
        except (TransientOAuthError, httpx.HTTPError):
            return await self._record_transient(connection)
        except OAuthError:
            await self.mark_needs_reauth(connection, UNRENEWABLE_CONFIG_MESSAGE)
            return RefreshOutcome(False, True, UNRENEWABLE_CONFIG_MESSAGE)

        return await self._persist_refresh(connection, renewed, registration_id=registration_id)

    async def _persist_refresh(
        self, connection: Connection, renewed: TokenResponse, *, registration_id: UUID | None
    ) -> RefreshOutcome:
        """Save a rotated token set, or make the failure to save visible.

        The provider has already rotated by the time we get here: on most
        providers the old refresh token is dead the moment the new one is
        issued, and Atlassian gives no grace period at all. So a commit that
        fails means access is genuinely lost, and the one unacceptable
        outcome is a row that still says ``active``.
        """
        try:
            await self.store_tokens(
                connection,
                renewed,
                registration_id=registration_id,
                resource=connection.oauth_resource or "",
                issuer=connection.oauth_issuer or renewed.issuer,
                authorized_by_user_id=None,
            )
        except Exception:
            await self._recover_after_failed_save(connection)
            return RefreshOutcome(False, True, SAVE_FAILED_MESSAGE)
        return RefreshOutcome(True, False, REFRESHED_MESSAGE)

    async def _recover_after_failed_save(self, connection: Connection) -> None:
        """Roll the poisoned transaction back and record the truth in a new one."""
        with contextlib.suppress(Exception):
            await self._session.rollback()
        with contextlib.suppress(Exception):
            fresh = await self._session.get(Connection, connection.id, populate_existing=True)
            if fresh is not None:
                fresh.status = ConnectionStatus.NEEDS_REAUTH.value
                fresh.last_error = SAVE_FAILED_MESSAGE
                fresh.oauth_refresh_failures = 0
                await self._session.commit()

    async def _record_transient(self, connection: Connection) -> RefreshOutcome:
        """Count one reachable-provider failure, and give up after enough of them."""
        connection.oauth_refresh_failures = (connection.oauth_refresh_failures or 0) + 1
        if connection.oauth_refresh_failures >= MAX_CONSECUTIVE_REFRESH_FAILURES:
            await self.mark_needs_reauth(connection, UNREACHABLE_MESSAGE)
            return RefreshOutcome(False, True, UNREACHABLE_MESSAGE)
        await self._session.commit()
        return RefreshOutcome(False, False, TRANSIENT_MESSAGE)

    async def _prepare_refresh(
        self, connection: Connection
    ) -> tuple[AuthorizationServerMetadata, UUID, ClientCredentials, str] | None:
        """Rebuild everything the token endpoint needs, re-validating the URLs.

        The endpoints were validated once when this connection was authorized.
        They are validated again here, at use time, so an operator who narrows
        the outbound allow-list narrows it for connections that already exist
        — the same reasoning that makes the MCP client re-validate its server
        URL on every call rather than trusting the stored row.
        """
        registration_id = connection.oauth_client_registration_id
        if registration_id is None:
            return None
        try:
            row, credentials = await self._clients.get_by_id(
                connection.workspace_id, registration_id
            )
        except LookupError:
            return None
        config = connection.config_json or {}
        raw_token_endpoint = config.get(CONFIG_TOKEN_ENDPOINT_KEY)
        if not isinstance(raw_token_endpoint, str) or not raw_token_endpoint:
            return None
        raw_revocation = config.get(CONFIG_REVOCATION_ENDPOINT_KEY)
        try:
            token_endpoint = validate_oauth_url(raw_token_endpoint, kind="token endpoint")
            revocation_endpoint = (
                validate_oauth_url(raw_revocation, kind="revocation endpoint")
                if isinstance(raw_revocation, str) and raw_revocation
                else None
            )
        except EndpointPolicyError:
            return None
        metadata = AuthorizationServerMetadata(
            issuer=connection.oauth_issuer or "",
            # An authorization endpoint is never dialled during a refresh; the
            # token endpoint is the only URL this metadata is used for, and it
            # is the one that was just re-validated.
            authorization_endpoint=token_endpoint,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
        )
        await self._clients.touch(row)
        return metadata, registration_id, credentials, row.source

    # --- Retiring ------------------------------------------------------

    async def revoke_and_clear(self, connection: Connection) -> None:
        """Tell the provider we are done, then destroy our copy either way.

        Revocation is best-effort by design: a provider that is down must not
        stop an admin from disconnecting an app, and the local secret going
        away is the part that is actually under our control.
        """
        secret_id = connection.encrypted_secret_id
        if secret_id is None:
            return
        with contextlib.suppress(Exception):
            tokens = await self._read_tokens(connection)
            prepared = await self._prepare_refresh(connection)
            if prepared is not None and prepared[0].revocation_endpoint:
                metadata, _registration_id, credentials, _source = prepared
                if tokens.refresh_token:
                    await revoke_token(
                        self._http,
                        metadata,
                        credentials=credentials,
                        token=tokens.refresh_token,
                        token_type_hint="refresh_token",
                    )
                await revoke_token(
                    self._http,
                    metadata,
                    credentials=credentials,
                    token=tokens.access_token,
                    token_type_hint="access_token",
                )
        connection.encrypted_secret_id = None
        connection.oauth_expires_at = None
        connection.oauth_refresh_expires_at = None
        with contextlib.suppress(SecretNotFoundError):
            await self._secrets.delete(connection.workspace_id, secret_id)
        await self._session.commit()


async def refresh_due_connections(
    session_factory: Callable[[], AsyncSession],
    crypto: SecretCrypto,
    http_client: httpx.AsyncClient,
    *,
    workspace_id: UUID,
    horizon_seconds: int = REFRESH_HORIZON_SECONDS,
    limit: int = 200,
) -> RefreshSweepResult:
    """One proactive pass over a workspace's soon-to-expire OAuth connections.

    Each connection is refreshed in its own session and its own transaction.
    That isolation is not tidiness: a rotated refresh token that is rolled
    back because a *different* connection failed is access permanently lost,
    for a connection that had nothing wrong with it.
    """
    horizon = datetime.now(UTC) + timedelta(seconds=max(0, horizon_seconds))
    async with session_factory() as session:
        due_ids = list(
            await session.scalars(
                select(Connection.id)
                .where(
                    Connection.workspace_id == workspace_id,
                    Connection.status == ConnectionStatus.ACTIVE.value,
                    Connection.oauth_expires_at.is_not(None),
                    Connection.oauth_expires_at < horizon,
                )
                .order_by(Connection.oauth_expires_at)
                .limit(max(1, limit))
            )
        )

    refreshed = 0
    needs_reauth = 0
    transient = 0
    for connection_id in due_ids:
        async with session_factory() as session:
            connection = await session.scalar(
                select(Connection).where(Connection.id == connection_id).with_for_update()
            )
            if connection is None or connection.status != ConnectionStatus.ACTIVE.value:
                continue
            service = ConnectionTokenService(session, crypto, http_client)
            outcome = await service.refresh(connection)
            if outcome.refreshed:
                refreshed += 1
            elif outcome.terminal:
                needs_reauth += 1
            else:
                transient += 1

    async with session_factory() as session:
        remaining = len(
            list(
                await session.scalars(
                    select(Connection.id).where(
                        Connection.workspace_id == workspace_id,
                        Connection.oauth_expires_at.is_not(None),
                        Connection.status == ConnectionStatus.ACTIVE.value,
                    )
                )
            )
        )

    return RefreshSweepResult(
        considered=len(due_ids),
        refreshed=refreshed,
        needs_reauth=needs_reauth,
        transient_failures=transient,
        remaining_oauth_connections=remaining,
    )
