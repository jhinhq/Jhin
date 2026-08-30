"""Durable state for in-flight authorizations and client registrations
(``docs/architecture/oauth.md``).

Two stores over one ``AsyncSession``. Both follow the same rule the rest of
Jhin follows: credential material goes through ``SecretStore`` and is
referenced by row id, and the columns hold only what a decision needs.

The pending-authorization store is the security-critical half. It is what
stands between "an admin clicked Connect" and "a provider redirected a browser
back at us", and everything about its shape is chosen for that window:

* the lookup key is ``sha256(handle)``, so the table cannot be read into the
  ability to complete somebody else's authorization;
* consumption is a single conditional ``UPDATE`` that both checks and claims,
  so two concurrent callbacks cannot both win under any isolation level;
* every rejection raises one class with one message, so a caller cannot leak
  which check failed and an attacker learns nothing from the difference.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import OAuthAuthorization, OAuthClientRegistration, Secret
from jhin_domain import SecretType, new_uuid7
from jhin_oauth.pkce import generate_state, state_hash
from jhin_oauth.types import ClientCredentials
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.store import SecretNotFoundError

#: Longest handle any caller may present. The handles Jhin issues are 43
#: characters; the bound exists so a hostile callback cannot make us hash a
#: megabyte before rejecting it.
MAX_HANDLE_LENGTH: Final[int] = 256

#: Substrings that disqualify a key from the non-secret draft payload. A draft
#: is stored in a plain JSONB column, so anything credential-shaped must be
#: refused at the door rather than trusted not to arrive.
_FORBIDDEN_DRAFT_KEY_PARTS: Final[tuple[str, ...]] = ("secret", "token", "password", "key")

_MAX_DRAFT_DEPTH: Final[int] = 8

OAuthFlow = Literal["authorization_code", "device_code", "github_app_manifest"]
RegistrationSource = Literal["dcr", "manual", "static"]


def _as_utc(value: datetime) -> datetime:
    """SQLite (unit tests) hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_well_formed_handle(handle: str) -> bool:
    if not handle or len(handle) > MAX_HANDLE_LENGTH:
        return False
    return all(
        character.isascii() and (character.isalnum() or character in "-_") for character in handle
    )


def _row_matches(
    row: OAuthAuthorization,
    *,
    expected_user_id: UUID,
    expected_workspace_id: UUID | None,
    expected_flow: str | None,
) -> bool:
    """Whether this row belongs to the session, workspace, and flow asking for it.

    One predicate for both :meth:`PendingAuthorizationStore.claim` and
    :meth:`PendingAuthorizationStore.peek` so the two can never drift into
    checking different things.
    """
    if row.user_id != expected_user_id:
        return False
    if expected_workspace_id is not None and row.workspace_id != expected_workspace_id:
        return False
    return not (expected_flow is not None and row.flow != expected_flow)


class PendingAuthorizationInvalid(Exception):
    """This authorization cannot be completed, and we will not say why.

    Unknown handle, already consumed, expired, a different user's session, a
    redirect URI that no longer matches, an issuer that does not: one class,
    one message, deliberately. Distinguishable failures would tell somebody
    probing the callback which of their guesses was closest.
    """

    MESSAGE: Final[str] = "This connection attempt is no longer valid. Start again from Apps."

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class PendingAuthorizationStore:
    """Create, claim, and clean up ``oauth_authorization`` rows."""

    def __init__(self, session: AsyncSession, crypto: SecretCrypto) -> None:
        self._session = session
        self._crypto = crypto
        self._secrets = SecretStore(session, crypto)

    async def create(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        flow: OAuthFlow,
        connector_type: str,
        ttl_seconds: int,
        connection_id: UUID | None = None,
        client_registration_id: UUID | None = None,
        issuer: str = "",
        authorization_endpoint: str = "",
        token_endpoint: str = "",
        revocation_endpoint: str | None = None,
        resource: str = "",
        scope: str = "",
        redirect_uri: str = "",
        iss_parameter_supported: bool = False,
        verifier: str | None = None,
        draft: Mapping[str, Any] | None = None,
        poll_interval_seconds: int = 5,
    ) -> tuple[OAuthAuthorization, str]:
        """Stage one pending authorization; return it and its raw handle.

        The raw handle is the caller's only copy — for authorization-code
        flows it becomes the OAuth ``state`` parameter, and for device flows
        it is the opaque poll handle. Only ``sha256(handle)`` is persisted.

        ``verifier`` (the PKCE code verifier, or the device code) is encrypted
        through ``SecretStore``. ``draft`` is the pending *non-secret*
        connection payload and is refused outright if any key looks like
        credential material — a draft column is not a place to put one.
        """
        payload = _validated_draft(draft)
        handle = generate_state()
        row_id = new_uuid7()
        now = datetime.now(UTC)

        verifier_secret_id: UUID | None = None
        if verifier is not None:
            secret = await self._secrets.create(
                workspace_id=workspace_id,
                name=f"oauth/state/{row_id.hex}",
                plaintext=verifier,
                secret_type=SecretType.OAUTH_STATE,
                created_by_user_id=user_id,
            )
            verifier_secret_id = secret.id

        row = OAuthAuthorization(
            id=row_id,
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=state_hash(handle),
            flow=flow,
            connector_type=connector_type,
            connection_id=connection_id,
            client_registration_id=client_registration_id,
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
            resource=resource,
            scope=scope,
            redirect_uri=redirect_uri,
            iss_parameter_supported=iss_parameter_supported,
            verifier_secret_id=verifier_secret_id,
            draft_json=payload,
            poll_interval_seconds=max(1, min(poll_interval_seconds, 3600)),
            created_at=now,
            expires_at=now + timedelta(seconds=max(1, ttl_seconds)),
        )
        self._session.add(row)
        await self._session.flush()
        return row, handle

    async def claim(
        self,
        *,
        handle: str,
        expected_user_id: UUID,
        expected_workspace_id: UUID | None = None,
        expected_flow: str | None = None,
    ) -> OAuthAuthorization:
        """Consume this authorization exactly once, or refuse.

        The check and the claim are one conditional ``UPDATE``: a row is
        returned only if it was unconsumed and unexpired at the moment the
        database evaluated the predicate. Two callbacks arriving together
        therefore produce one winner and one refusal, with no read-then-write
        window between them for the loser to slip through.

        ``expected_workspace_id`` is how a workspace-scoped route refuses a
        handle minted in a *different* workspace: the row carries its own
        ``workspace_id`` and the connection is created there, so a caller who
        is a member of both would otherwise attach a connection to workspace A
        through workspace B's URL. The callback route is not workspace-scoped
        and passes ``None``; every route that has a workspace in its path
        passes it. ``expected_flow`` refuses a handle minted for one flow and
        presented to another's endpoint, so a device or manifest row can never
        be walked through the authorization-code path.
        """
        if not _is_well_formed_handle(handle):
            raise PendingAuthorizationInvalid()
        now = datetime.now(UTC)
        claimed_id = await self._session.scalar(
            update(OAuthAuthorization)
            .where(
                OAuthAuthorization.state_hash == state_hash(handle),
                OAuthAuthorization.consumed_at.is_(None),
                OAuthAuthorization.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(OAuthAuthorization.id)
            .execution_options(synchronize_session=False)
        )
        if claimed_id is None:
            raise PendingAuthorizationInvalid()
        # ``populate_existing`` so an instance already in this session's
        # identity map reflects the claim rather than its pre-claim state.
        row = await self._session.scalar(
            select(OAuthAuthorization)
            .where(OAuthAuthorization.id == claimed_id)
            .execution_options(populate_existing=True)
        )
        if row is None or not _row_matches(
            row,
            expected_user_id=expected_user_id,
            expected_workspace_id=expected_workspace_id,
            expected_flow=expected_flow,
        ):
            # A valid handle presented by somebody else's browser, from another
            # workspace, or at the wrong flow's endpoint. The row is already
            # consumed, which is the right outcome: whoever holds the handle has
            # spent it, and the legitimate user starts over.
            raise PendingAuthorizationInvalid()
        return row

    async def peek(
        self,
        *,
        handle: str,
        expected_user_id: UUID,
        expected_workspace_id: UUID | None = None,
        expected_flow: str | None = None,
    ) -> OAuthAuthorization:
        """Validate without consuming, for device-code polling.

        Polling asks the same question every few seconds and must not burn the
        row doing it; the row is claimed once, at the end, when the provider
        finally hands over a token. Binds the same way :meth:`claim` does, so
        a poll cannot reach across a workspace or a flow either.
        """
        if not _is_well_formed_handle(handle):
            raise PendingAuthorizationInvalid()
        row = await self._session.scalar(
            select(OAuthAuthorization).where(OAuthAuthorization.state_hash == state_hash(handle))
        )
        if row is None or row.consumed_at is not None:
            raise PendingAuthorizationInvalid()
        if not _row_matches(
            row,
            expected_user_id=expected_user_id,
            expected_workspace_id=expected_workspace_id,
            expected_flow=expected_flow,
        ):
            raise PendingAuthorizationInvalid()
        if _as_utc(row.expires_at) <= datetime.now(UTC):
            raise PendingAuthorizationInvalid()
        return row

    async def reveal_verifier(self, row: OAuthAuthorization) -> str:
        """Decrypt the PKCE code verifier for immediate use at the token endpoint."""
        return await self._reveal_attached_secret(row)

    async def reveal_device_code(self, row: OAuthAuthorization) -> str:
        """Decrypt the device code for the next poll. Never returned to a client."""
        return await self._reveal_attached_secret(row)

    async def _reveal_attached_secret(self, row: OAuthAuthorization) -> str:
        if row.verifier_secret_id is None:
            raise PendingAuthorizationInvalid()
        try:
            plaintext: str = await self._secrets.reveal(row.workspace_id, row.verifier_secret_id)
        except SecretNotFoundError:
            raise PendingAuthorizationInvalid() from None
        return plaintext

    async def finish(self, row: OAuthAuthorization) -> None:
        """Delete the row and the secret it owns, after success or a terminal failure."""
        secret_id = row.verifier_secret_id
        workspace_id = row.workspace_id
        await self._session.delete(row)
        await self._session.flush()
        if secret_id is not None:
            await self._delete_secret(workspace_id, secret_id)

    async def purge_expired(self, *, older_than_seconds: int = 3600, limit: int = 200) -> int:
        """Opportunistic cleanup of long-dead rows; returns how many went.

        Bounded and called from the start of a new authorization rather than
        scheduled: the work is proportional to the traffic that creates it,
        and no sweeper is needed to keep a table of ten-minute rows small.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=max(0, older_than_seconds))
        rows = list(
            await self._session.scalars(
                select(OAuthAuthorization)
                .where(OAuthAuthorization.expires_at < cutoff)
                .order_by(OAuthAuthorization.expires_at)
                .limit(max(1, limit))
            )
        )
        orphaned = [
            (row.workspace_id, row.verifier_secret_id)
            for row in rows
            if row.verifier_secret_id is not None
        ]
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        for workspace_id, secret_id in orphaned:
            if secret_id is not None:
                await self._delete_secret(workspace_id, secret_id)
        return len(rows)

    async def _delete_secret(self, workspace_id: UUID, secret_id: UUID) -> None:
        try:
            await self._secrets.delete(workspace_id, secret_id)
        except SecretNotFoundError:
            # Already gone (a concurrent purge, or a cascade). Nothing to undo.
            return


def _validated_draft(draft: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a draft payload after proving it holds nothing credential-shaped."""
    if draft is None:
        return {}
    _reject_credential_keys(draft, depth=0)
    return dict(draft)


def _reject_credential_keys(value: Any, *, depth: int) -> None:
    if depth > _MAX_DRAFT_DEPTH:
        raise ValueError("draft payload is nested too deeply")
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise ValueError("draft payload keys must be strings")
            lowered = key.lower()
            if any(part in lowered for part in _FORBIDDEN_DRAFT_KEY_PARTS):
                raise ValueError("draft payload must not carry credential fields")
            _reject_credential_keys(value[key], depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_credential_keys(item, depth=depth + 1)


class OAuthClientStore:
    """Read and write one workspace's OAuth client registrations."""

    def __init__(self, session: AsyncSession, crypto: SecretCrypto) -> None:
        self._session = session
        self._crypto = crypto
        self._secrets = SecretStore(session, crypto)

    async def get(
        self, workspace_id: UUID, *, issuer: str, redirect_uri: str
    ) -> tuple[OAuthClientRegistration, ClientCredentials] | None:
        """This workspace's client at this issuer and redirect URI, if any."""
        row = await self._session.scalar(
            select(OAuthClientRegistration).where(
                OAuthClientRegistration.workspace_id == workspace_id,
                OAuthClientRegistration.issuer == issuer,
                OAuthClientRegistration.redirect_uri == redirect_uri,
            )
        )
        if row is None:
            return None
        return row, await self._credentials(row)

    async def get_by_id(
        self, workspace_id: UUID, registration_id: UUID
    ) -> tuple[OAuthClientRegistration, ClientCredentials]:
        """Load a registration by id, workspace-scoped. Raises ``LookupError``."""
        row = await self._session.scalar(
            select(OAuthClientRegistration).where(
                OAuthClientRegistration.id == registration_id,
                OAuthClientRegistration.workspace_id == workspace_id,
            )
        )
        if row is None:
            raise LookupError("no such OAuth client registration in this workspace")
        return row, await self._credentials(row)

    async def save(
        self,
        *,
        workspace_id: UUID,
        issuer: str,
        redirect_uri: str,
        credentials: ClientCredentials,
        scopes: str,
        source: RegistrationSource,
        created_by_user_id: UUID | None,
    ) -> OAuthClientRegistration:
        """Upsert on ``(workspace_id, issuer, redirect_uri)``.

        Secrets are rotated in place rather than replaced by new rows, so
        re-registering never leaves orphan ciphertext behind for a later
        operator to wonder about.
        """
        existing = await self._session.scalar(
            select(OAuthClientRegistration).where(
                OAuthClientRegistration.workspace_id == workspace_id,
                OAuthClientRegistration.issuer == issuer,
                OAuthClientRegistration.redirect_uri == redirect_uri,
            )
        )
        row = existing or OAuthClientRegistration(
            workspace_id=workspace_id,
            issuer=issuer,
            redirect_uri=redirect_uri,
            client_id=credentials.client_id,
            created_by_user_id=created_by_user_id,
        )
        row.client_id = credentials.client_id
        row.token_endpoint_auth_method = credentials.token_endpoint_auth_method
        row.source = source
        row.scopes = scopes
        row.client_secret_expires_at = credentials.client_secret_expires_at
        row.registration_client_uri = credentials.registration_client_uri
        if existing is None:
            self._session.add(row)
            await self._session.flush()

        row.client_secret_id = await self._store_optional(
            workspace_id,
            existing_id=row.client_secret_id,
            plaintext=credentials.client_secret,
            name=f"oauth/client/{row.id.hex}/secret",
            created_by_user_id=created_by_user_id,
        )
        row.registration_access_token_id = await self._store_optional(
            workspace_id,
            existing_id=row.registration_access_token_id,
            plaintext=credentials.registration_access_token,
            name=f"oauth/client/{row.id.hex}/registration",
            created_by_user_id=created_by_user_id,
        )
        await self._session.flush()
        return row

    async def forget(self, workspace_id: UUID, registration_id: UUID) -> None:
        """Delete a registration and every secret it owns.

        Used when an authorization server reports ``invalid_client`` for a
        registration it issued itself: the credentials are worthless, and
        keeping them only guarantees the next attempt fails the same way.
        """
        row = await self._session.scalar(
            select(OAuthClientRegistration).where(
                OAuthClientRegistration.id == registration_id,
                OAuthClientRegistration.workspace_id == workspace_id,
            )
        )
        if row is None:
            return
        secret_ids = [
            secret_id
            for secret_id in (row.client_secret_id, row.registration_access_token_id)
            if secret_id is not None
        ]
        await self._session.delete(row)
        await self._session.flush()
        for secret_id in secret_ids:
            with contextlib.suppress(SecretNotFoundError):
                await self._secrets.delete(workspace_id, secret_id)

    async def list(self, workspace_id: UUID) -> list[OAuthClientRegistration]:
        rows = await self._session.scalars(
            select(OAuthClientRegistration)
            .where(OAuthClientRegistration.workspace_id == workspace_id)
            .order_by(OAuthClientRegistration.created_at)
        )
        return list(rows)

    async def touch(self, row: OAuthClientRegistration) -> None:
        """Record that this registration was just used to obtain a token."""
        row.last_used_at = datetime.now(UTC)

    async def _credentials(self, row: OAuthClientRegistration) -> ClientCredentials:
        client_secret = await self._reveal_optional(row.workspace_id, row.client_secret_id)
        registration_token = await self._reveal_optional(
            row.workspace_id, row.registration_access_token_id
        )
        return ClientCredentials(
            client_id=row.client_id,
            client_secret=client_secret,
            token_endpoint_auth_method=row.token_endpoint_auth_method,
            registration_access_token=registration_token,
            registration_client_uri=row.registration_client_uri,
            client_secret_expires_at=(
                _as_utc(row.client_secret_expires_at)
                if row.client_secret_expires_at is not None
                else None
            ),
        )

    async def _reveal_optional(self, workspace_id: UUID, secret_id: UUID | None) -> str | None:
        if secret_id is None:
            return None
        try:
            plaintext: str = await self._secrets.reveal(workspace_id, secret_id)
        except SecretNotFoundError:
            return None
        return plaintext

    async def _store_optional(
        self,
        workspace_id: UUID,
        *,
        existing_id: UUID | None,
        plaintext: str | None,
        name: str,
        created_by_user_id: UUID | None,
    ) -> UUID | None:
        """Create, rotate, or drop one optional secret attached to a registration."""
        if plaintext is None:
            if existing_id is not None:
                with contextlib.suppress(SecretNotFoundError):
                    await self._secrets.delete(workspace_id, existing_id)
            return None
        if existing_id is not None:
            try:
                rotated = await self._secrets.rotate(workspace_id, existing_id, plaintext)
            except SecretNotFoundError:
                pass
            else:
                rotated_id: UUID = rotated.id
                return rotated_id
        # A name collides only with a secret this same registration owns, so
        # reuse the row rather than tripping the (workspace, name) constraint.
        collision = await self._session.scalar(
            select(Secret).where(Secret.workspace_id == workspace_id, Secret.name == name)
        )
        if collision is not None:
            replaced = await self._secrets.rotate(workspace_id, collision.id, plaintext)
            replaced_id: UUID = replaced.id
            return replaced_id
        created = await self._secrets.create(
            workspace_id=workspace_id,
            name=name,
            plaintext=plaintext,
            secret_type=SecretType.OAUTH_CLIENT,
            created_by_user_id=created_by_user_id,
        )
        created_id: UUID = created.id
        return created_id
