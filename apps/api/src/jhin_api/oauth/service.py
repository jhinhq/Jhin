"""The OAuth flows, end to end (``docs/architecture/oauth.md``).

Four things happen in this module and nothing else does: a connector is
probed to find out how it actually signs in, an authorization is started, a
callback is judged, and a device grant is polled. The router is a thin
translation layer over these functions; the protocol lives in ``jhin_oauth``
and the storage in ``jhin_oauth.persistence``.

The callback is the security-critical half, and its rules are absolute:

* five checks, every one of which must pass — a well-formed handle, a row that
  exists, an atomic single-use claim, the *initiating user's own session*, and
  an ``iss`` that byte-matches the issuer recorded before we redirected;
* one refusal, byte-identical for all five, so nobody can tell which failed;
* no value from the request ever reaches a ``Location`` header, an exception
  message, a log line, or a stored row. Not ``error_description``, not
  ``error_uri``, not a ``next`` parameter somebody hoped we would honour.

Jhin is a leaf OAuth client, never a proxy: it exposes no authorization,
token, or registration endpoint of its own, and accepts no ``redirect_uri``,
``client_id``, or return URL from any request. That absence is what makes the
confused-deputy attack structurally impossible here, and no endpoint may
reintroduce it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.connections import service as connections
from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import redirect as redirect_module
from jhin_api.oauth.schemas import (
    ConnectMethod,
    GitHubAppManifestIn,
    GitHubAppManifestOut,
    OAuthClientCreate,
    OAuthClientOut,
    OAuthDeviceStartIn,
    OAuthDeviceStartOut,
    OAuthProbeIn,
    OAuthProbeOut,
    OAuthStartIn,
    OAuthStartOut,
    RegistrationSource,
)
from jhin_api.settings import Settings
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_db.models import Connection, OAuthAuthorization
from jhin_oauth.discovery import (
    probe_mcp_endpoint,
    select_scopes,
)
from jhin_oauth.errors import (
    ClientForgottenError,
    DeviceAuthorizationDenied,
    DeviceCodeExpired,
    OAuthError,
    TokenError,
    TransientOAuthError,
)
from jhin_oauth.lifecycle import (
    CONFIG_ISSUER_KEY,
    CONFIG_RESOURCE_KEY,
    CONFIG_REVOCATION_ENDPOINT_KEY,
    CONFIG_SCOPE_KEY,
    CONFIG_TOKEN_ENDPOINT_KEY,
    ConnectionTokenService,
)
from jhin_oauth.persistence import (
    OAuthClientStore,
    PendingAuthorizationInvalid,
    PendingAuthorizationStore,
)
from jhin_oauth.pkce import generate_pkce
from jhin_oauth.registration import register_client
from jhin_oauth.tokens import (
    build_authorization_url,
    exchange_code,
    next_poll_interval,
    poll_device_token,
    start_device_authorization,
)
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    ClientCredentials,
    DeviceTokenPending,
    TokenResponse,
)
from jhin_oauth.urls import canonical_resource_uri, validate_oauth_url
from jhin_observability import get_logger
from jhin_secrets import SecretCrypto

logger = get_logger(__name__)

MCP_CONNECTOR_TYPE: Final[str] = "mcp"
OAUTH_AUTH_TYPE: Final[str] = "oauth"
DEVICE_FLOW_MAX_TTL_SECONDS: Final[int] = 1_800
GITHUB_MANIFEST_TTL_SECONDS: Final[int] = 3_600
#: GitHub speaks OAuth from one origin; DCR credentials are keyed by it.
GITHUB_ISSUER: Final[str] = "https://github.com"
PURGE_OLDER_THAN_SECONDS: Final[int] = 3_600
PURGE_LIMIT: Final[int] = 200

_INVALID_ATTEMPT_DETAIL: Final[str] = PendingAuthorizationInvalid.MESSAGE


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def invalid_attempt() -> HTTPException:
    """The one refusal every failed callback produces, byte for byte."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_ATTEMPT_DETAIL)


def _upstream_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


def _display_host(url: str) -> str:
    """Host only. A full provider URL in our own UI copy is a phishing surface."""
    with contextlib.suppress(ValueError):
        return (urlsplit(url).hostname or "").lower()
    return ""


@dataclass(frozen=True, slots=True)
class DiscoveredTarget:
    """One resolved authorization target: who to ask, for what, about which resource."""

    metadata: AuthorizationServerMetadata
    resource: str
    scope: str
    challenge_scope: str | None
    #: True when this came from the shipped provider table rather than
    #: discovery. Such a provider never does DCR, so a missing client is an
    #: admin task, not something to attempt automatically.
    is_static: bool = False


# --- Probing -----------------------------------------------------------


async def probe(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    settings: Settings,
    payload: OAuthProbeIn,
) -> OAuthProbeOut:
    """Ask the server how it signs in, rather than guessing from a catalog hint.

    The catalog's ``auth_hint`` is wrong more often than right as a routing
    signal — it is a label for the Apps library, not a protocol fact — so
    nothing here consults it. For an MCP server we make one unauthenticated
    request and read the answer; for a native connector we look up the
    provider table the connector ships.
    """
    connector_type = payload.connector_type
    if connector_type != MCP_CONNECTOR_TYPE:
        return await _static_provider_probe(db, crypto, ctx, settings, connector_type)

    if not payload.server_url:
        return OAuthProbeOut(
            method="api_key",
            supports_oauth=False,
            supports_dcr=False,
            reason="server_url_required",
        )
    try:
        result = await probe_mcp_endpoint(http_client, payload.server_url)
    except EndpointPolicyError:
        return OAuthProbeOut(
            method="api_key",
            supports_oauth=False,
            supports_dcr=False,
            reason="server_url_not_allowed",
        )
    except (OAuthError, httpx.HTTPError):
        return OAuthProbeOut(
            method="api_key",
            supports_oauth=False,
            supports_dcr=False,
            reason="discovery_failed",
        )

    metadata = result.authorization_server
    if not result.supports_oauth or metadata is None:
        return OAuthProbeOut(
            method="api_key",
            supports_oauth=False,
            supports_dcr=False,
            reason="no_oauth_offered" if result.requires_auth else "connector_has_no_oauth",
        )

    # The canonical URI of the MCP server itself, never the PRM document's own
    # ``resource`` field. RFC 9728 lets one document cover a whole subtree, so
    # a server at ``https://host/mcp`` may legitimately publish a PRM naming
    # ``https://host``; sending that as the RFC 8707 indicator would both
    # deviate from the MCP rule and mint a token whose recorded audience can
    # never byte-match what ``oauth_auth_headers`` recomputes at call time.
    resource = canonical_resource_uri(result.server_url)
    scope = select_scopes(
        challenge_scope=result.challenge_scope,
        resource_scopes=(
            result.protected_resource.scopes_supported
            if result.protected_resource is not None
            else ()
        ),
        server_scopes=metadata.scopes_supported,
        want_offline_access=metadata.supports_refresh(),
    )
    clients = OAuthClientStore(db, crypto)
    existing = await clients.get(
        ctx.workspace_id,
        issuer=metadata.issuer,
        redirect_uri=redirect_module.redirect_uri(settings),
    )
    client_configured = existing is not None
    method: ConnectMethod
    if client_configured or metadata.supports_dcr():
        method = "oauth_discovery"
        reason = ""
    else:
        method = "oauth_needs_client"
        reason = "needs_client_credentials"
    return OAuthProbeOut(
        method=method,
        supports_oauth=True,
        supports_dcr=metadata.supports_dcr(),
        issuer=metadata.issuer,
        authorization_server_display=_display_host(metadata.issuer),
        scopes=scope.split() if scope else [],
        resource=resource,
        client_configured=client_configured,
        requires_client_secret=False,
        reason=reason,
    )


async def _static_provider_probe(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    settings: Settings,
    connector_type: str,
) -> OAuthProbeOut:
    """How a native connector signs in, from the provider table it ships.

    Resolved lazily so this module keeps working while the provider table is
    being built out: a connector with no entry answers ``api_key``, which is
    exactly the honest answer for a connector with no OAuth support.

    ``client_configured`` is looked up for real, exactly as the MCP branch
    does. A statically-known provider never supports DCR, so this flag is the
    only thing that tells the panel whether the workspace has already
    registered a client at this issuer; answering a blanket ``False`` sent an
    admin who had just registered one back to the registration form for good.
    """
    provider = _static_provider_for(connector_type)
    if provider is None:
        return OAuthProbeOut(
            method="api_key",
            supports_oauth=False,
            supports_dcr=False,
            reason="connector_has_no_oauth",
        )
    clients = OAuthClientStore(db, crypto)
    existing = await clients.get(
        ctx.workspace_id,
        issuer=provider.issuer,
        redirect_uri=redirect_module.redirect_uri(settings),
    )
    client_configured = existing is not None
    if provider.device_authorization_endpoint:
        method: ConnectMethod = "device_code"
    else:
        method = "oauth_static"
    return OAuthProbeOut(
        method=method,
        supports_oauth=True,
        supports_dcr=False,
        issuer=provider.issuer,
        authorization_server_display=_display_host(provider.issuer),
        scopes=list(provider.default_scopes),
        resource="",
        client_configured=client_configured,
        requires_client_secret=provider.requires_client_secret,
        reason="" if client_configured else "needs_client_credentials",
    )


def _static_provider_for(connector_type: str) -> Any:
    """The connector's static OAuth provider entry, or ``None``."""
    try:
        from jhin_connectors.oauth_providers import STATIC_PROVIDERS
    except ImportError:  # pragma: no cover - provider table not installed
        return None
    for provider in STATIC_PROVIDERS.values():
        if provider.connector_type == connector_type:
            return provider
    return None


def _static_provider_by_key(provider_key: str) -> Any:
    try:
        from jhin_connectors.oauth_providers import STATIC_PROVIDERS
    except ImportError:  # pragma: no cover - provider table not installed
        return None
    return STATIC_PROVIDERS.get(provider_key)


# --- Starting an authorization -----------------------------------------


async def start_authorization(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    settings: Settings,
    payload: OAuthStartIn,
    *,
    request_id: UUID,
    ip_hash: str,
) -> OAuthStartOut:
    """Mint one pending authorization and the URL that begins it.

    Discovery, registration, PKCE, state, and the pending row are all one
    transaction: either the browser gets a URL whose ``state`` names a row we
    can honour, or nothing was written at all.
    """
    connector_type = payload.connector_type
    connections.get_connector(connector_type)
    uri = redirect_module.redirect_uri(settings)

    pending_store = PendingAuthorizationStore(db, crypto)
    # Opportunistic, bounded cleanup: the work is proportional to the traffic
    # that creates it, so a table of ten-minute rows needs no sweeper.
    with contextlib.suppress(Exception):
        await pending_store.purge_expired(
            older_than_seconds=PURGE_OLDER_THAN_SECONDS, limit=PURGE_LIMIT
        )

    connection = await _target_connection(db, ctx, payload.connection_id)
    target = await _resolve_target(http_client, settings, payload, connection)
    registration_id, credentials, source = await _client_for(
        db, crypto, ctx, http_client, settings, target=target
    )

    pkce = generate_pkce()
    row, state = await pending_store.create(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        flow="authorization_code",
        connector_type=connector_type,
        ttl_seconds=settings.oauth_state_ttl_seconds,
        connection_id=connection.id if connection is not None else None,
        client_registration_id=registration_id,
        issuer=target.metadata.issuer,
        authorization_endpoint=target.metadata.authorization_endpoint,
        token_endpoint=target.metadata.token_endpoint,
        revocation_endpoint=target.metadata.revocation_endpoint,
        resource=target.resource,
        scope=target.scope,
        redirect_uri=uri,
        iss_parameter_supported=(target.metadata.authorization_response_iss_parameter_supported),
        verifier=pkce.verifier,
        draft={"name": payload.name, "config": dict(payload.config)},
    )

    provider = _static_provider_by_key(payload.provider_key) if payload.provider_key else None
    extra_params = dict(provider.extra_authorize_params) if provider is not None else None
    try:
        authorization_url = build_authorization_url(
            target.metadata,
            client_id=credentials.client_id,
            redirect_uri=uri,
            state=state,
            pkce=pkce,
            scope=target.scope,
            resource=target.resource,
            extra_params=extra_params,
        )
    except (ValueError, EndpointPolicyError) as exc:
        raise _bad_request(
            "This app's authorization service could not be used as configured."
        ) from exc

    audit.record(
        db,
        action="connection.oauth_started",
        target_type="connection",
        target_id=connection.id if connection is not None else None,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "connector_type": connector_type,
            "issuer": target.metadata.issuer,
            "resource": target.resource,
            "scopes": target.scope,
            "registration_source": source,
        },
    )
    await db.commit()
    return OAuthStartOut(
        authorization_url=authorization_url,
        state_expires_at=row.expires_at,
        issuer=target.metadata.issuer,
        scopes=target.scope.split() if target.scope else [],
        resource=target.resource,
        authorized_as_user_id=ctx.user.id,
        client_source=source,
    )


async def _target_connection(
    db: AsyncSession, ctx: WorkspaceContext, connection_id: UUID | None
) -> Connection | None:
    if connection_id is None:
        return None
    return await connections.get_connection(db, ctx.workspace_id, connection_id)


async def _resolve_target(
    http_client: httpx.AsyncClient,
    settings: Settings,
    payload: OAuthStartIn,
    connection: Connection | None,
) -> DiscoveredTarget:
    """Work out which authorization server to send the browser to, and for what.

    The issuer recorded here is the *validated* one — the value that
    byte-matched the metadata document's own ``issuer`` field — because that
    is what the callback compares ``iss`` against and what client credentials
    are keyed by.

    A native connector is resolved from its own connector type when the caller
    names no ``provider_key``. The key is a detail of this module's table, not
    something a browser should have to know, and requiring it meant a static
    provider fell through to the MCP branch and was refused for having no
    server address.
    """
    provider = (
        _static_provider_by_key(payload.provider_key)
        if payload.provider_key
        else _static_provider_for(payload.connector_type)
    )
    if payload.provider_key and provider is None:
        raise _bad_request("That provider is not one this Jhin install knows how to connect.")
    if provider is not None:
        from jhin_connectors.oauth_providers import provider_metadata

        static_metadata = provider_metadata(provider)
        scope = " ".join(provider.default_scopes)
        return DiscoveredTarget(
            metadata=static_metadata,
            resource="",
            scope=scope,
            challenge_scope=None,
            is_static=True,
        )

    server_url = _server_url_for(payload, connection)
    if not server_url:
        raise _bad_request("This connection needs a server address before it can be connected.")
    try:
        probed = await probe_mcp_endpoint(http_client, server_url)
    except EndpointPolicyError as exc:
        raise _bad_request(
            "That server address is not one this Jhin install is allowed to reach."
        ) from exc
    metadata = probed.authorization_server
    if metadata is None or not probed.supports_oauth:
        raise _bad_request("That server did not offer a way to connect without an API key.")
    # See the note in ``probe``: the audience is the server's own canonical
    # URI, which is both what the MCP rule specifies and the exact value the
    # executor recomputes before attaching the token to a call.
    resource = canonical_resource_uri(probed.server_url)
    pending_scope = _pending_scope(connection)
    scope = pending_scope or select_scopes(
        challenge_scope=probed.challenge_scope,
        resource_scopes=(
            probed.protected_resource.scopes_supported
            if probed.protected_resource is not None
            else ()
        ),
        server_scopes=metadata.scopes_supported,
        want_offline_access=metadata.supports_refresh(),
    )
    return DiscoveredTarget(
        metadata=metadata,
        resource=resource,
        scope=scope,
        challenge_scope=probed.challenge_scope,
    )


def _server_url_for(payload: OAuthStartIn, connection: Connection | None) -> str:
    candidate = payload.config.get("server_url")
    if isinstance(candidate, str) and candidate:
        return candidate
    if connection is not None:
        stored = connection.config_json.get("server_url")
        if isinstance(stored, str):
            return stored
    return ""


def _pending_scope(connection: Connection | None) -> str:
    """A step-up scope set the executor recorded after an ``insufficient_scope``."""
    if connection is None:
        return ""
    pending = connection.config_json.get("oauth_pending_scope")
    return pending if isinstance(pending, str) else ""


async def _client_for(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    settings: Settings,
    *,
    target: DiscoveredTarget,
) -> tuple[UUID, ClientCredentials, RegistrationSource]:
    """This workspace's client at this issuer, registering one if we may.

    Credentials are keyed by ``(workspace, issuer, redirect URI)`` and reused
    for every later connection to the same server — which is what turns the
    second and every subsequent connection into two clicks and no fields.
    """
    uri = redirect_module.redirect_uri(settings)
    store = OAuthClientStore(db, crypto)
    existing = await store.get(ctx.workspace_id, issuer=target.metadata.issuer, redirect_uri=uri)
    if existing is not None:
        row, credentials = existing
        return row.id, credentials, _source_of(row.source)

    if target.is_static:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This workspace has not registered Jhin with this provider yet. Add the "
                "client id in Settings → OAuth, then connect."
            ),
        )
    if not target.metadata.supports_dcr():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This server does not register apps automatically. An admin needs to add a "
                "client id for it once, in Settings → OAuth."
            ),
        )
    try:
        credentials = await register_client(
            http_client,
            target.metadata,
            redirect_uri=uri,
            client_name=settings.oauth_client_name,
            client_uri=settings.app_url,
            scopes=target.scope,
        )
    except TransientOAuthError as exc:
        raise _upstream_unavailable(
            "The provider's authorization service could not be reached. Try again shortly."
        ) from exc
    except (OAuthError, EndpointPolicyError) as exc:
        raise _bad_request(
            "This server refused to register Jhin as an app. An admin can add a client id "
            "for it in Settings → OAuth instead."
        ) from exc
    row = await store.save(
        workspace_id=ctx.workspace_id,
        issuer=target.metadata.issuer,
        redirect_uri=uri,
        credentials=credentials,
        scopes=target.scope,
        source="dcr",
        created_by_user_id=ctx.user.id,
    )
    return row.id, credentials, "dcr"


def _source_of(raw: str) -> RegistrationSource:
    """Narrow a stored ``source`` string, defaulting to the conservative value.

    ``manual`` is the safe default for an unrecognised value: it is the one
    provenance Jhin never re-registers automatically, so an unreadable row
    cannot become a reason to silently mint new credentials.
    """
    if raw == "dcr":
        return "dcr"
    if raw == "static":
        return "static"
    return "manual"


# --- The callback ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """Where the browser goes next, and nothing the request could influence."""

    connection: Connection | None
    error: redirect_module.OAuthReturnError | None


async def complete_authorization(
    db: AsyncSession,
    crypto: SecretCrypto,
    http_client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    state: str,
    code: str | None,
    iss: str | None,
    provider_error: str | None,
    request_id: UUID,
    ip_hash: str,
) -> CallbackResult:
    """Judge one provider callback and, if it survives, create the connection.

    Raises :class:`fastapi.HTTPException` with the single invalid-attempt body
    for every rejection. ``provider_error`` is used only as a boolean — the
    provider's ``error``, ``error_description``, and ``error_uri`` are
    attacker-influenced text and are never read, stored, logged, or rendered.
    """
    pending = PendingAuthorizationStore(db, crypto)
    try:
        row = await pending.claim(
            handle=state, expected_user_id=user_id, expected_flow="authorization_code"
        )
    except PendingAuthorizationInvalid as exc:
        raise invalid_attempt() from exc

    try:
        _verify_callback_context(row, settings=settings, iss=iss)
    except PendingAuthorizationInvalid as exc:
        await _abandon(db, pending, row)
        raise invalid_attempt() from exc

    if provider_error is not None or not code:
        # The user said no at the provider, or the provider refused. The row is
        # spent either way; the browser goes back to Apps with a flag, never
        # with the provider's own words.
        await _abandon(db, pending, row)
        return CallbackResult(connection=None, error="denied")

    clients = OAuthClientStore(db, crypto)
    if row.client_registration_id is None:
        await _abandon(db, pending, row)
        raise invalid_attempt()
    try:
        registration, credentials = await clients.get_by_id(
            row.workspace_id, row.client_registration_id
        )
    except LookupError as exc:
        await _abandon(db, pending, row)
        raise invalid_attempt() from exc

    verifier = await pending.reveal_verifier(row)
    metadata = _metadata_from_row(row)
    try:
        tokens = await exchange_code(
            http_client,
            metadata,
            credentials=credentials,
            code=code,
            redirect_uri=row.redirect_uri,
            code_verifier=verifier,
            resource=row.resource,
        )
    except (OAuthError, EndpointPolicyError, httpx.HTTPError):
        # Deliberately no provider text: the exchange failed, the pending row
        # is spent, and the user is told to try again from a page we control.
        logger.warning("oauth.code_exchange_failed", connector_type=row.connector_type)
        await _abandon(db, pending, row)
        return CallbackResult(connection=None, error="failed")

    await clients.touch(registration)
    try:
        connection = await _persist_connection(
            db,
            crypto,
            http_client,
            row=row,
            tokens=tokens,
            registration_id=registration.id,
            registration_source=registration.source,
            user_id=user_id,
            request_id=request_id,
            ip_hash=ip_hash,
        )
    except Exception:
        # The tokens are real but the connection could not be written — a name
        # taken while the user was at the provider, or a config the connector
        # now rejects. Send them back to a page we control rather than an
        # error body, and hand the tokens back to the provider on the way out
        # so nothing usable is left behind unattached to a connection.
        logger.warning("oauth.connection_not_created", connector_type=row.connector_type)
        with contextlib.suppress(Exception):
            await db.rollback()
        await _abandon(db, pending, row)
        return CallbackResult(connection=None, error="failed")
    await pending.finish(row)
    await db.commit()
    return CallbackResult(connection=connection, error=None)


def _verify_callback_context(
    row: OAuthAuthorization, *, settings: Settings, iss: str | None
) -> None:
    """The two checks that need more than the row itself: redirect URI and issuer.

    The redirect URI is compared byte-for-byte against the value recomputed
    from settings *now*, so an operator who changed the base URL mid-flow gets
    a refusal instead of a token bound to a URI no provider has registered.

    ``iss`` is compared with plain string equality and no normalization at all
    — no case folding, no default-port elision, no trailing-slash or
    percent-encoding cleanup. RFC 9207 §2.4 says byte comparison, and every
    "helpful" normalization is a way for two different servers to look like
    one.
    """
    try:
        expected_uri = redirect_module.redirect_uri(settings)
    except redirect_module.OAuthRedirectMisconfigured as exc:
        # The instance can no longer compute its own redirect URI, so nothing
        # arriving here can be honoured until an operator fixes that.
        raise PendingAuthorizationInvalid() from exc
    if row.redirect_uri != expected_uri:
        raise PendingAuthorizationInvalid()
    if row.iss_parameter_supported:
        if iss is None or iss != row.issuer:
            raise PendingAuthorizationInvalid()
    elif iss is not None and row.issuer and iss != row.issuer:
        raise PendingAuthorizationInvalid()


def _metadata_from_row(row: OAuthAuthorization) -> AuthorizationServerMetadata:
    """Rebuild the token-endpoint metadata, re-validating the stored URLs.

    Validation happens again here rather than being trusted from the row, so
    tightening the outbound allow-list takes effect on flows already in
    progress.
    """
    try:
        token_endpoint = validate_oauth_url(row.token_endpoint, kind="token endpoint")
        revocation_endpoint = (
            validate_oauth_url(row.revocation_endpoint, kind="revocation endpoint")
            if row.revocation_endpoint
            else None
        )
    except EndpointPolicyError as exc:
        raise PendingAuthorizationInvalid() from exc
    return AuthorizationServerMetadata(
        issuer=row.issuer,
        authorization_endpoint=token_endpoint,
        token_endpoint=token_endpoint,
        revocation_endpoint=revocation_endpoint,
    )


async def _abandon(
    db: AsyncSession, pending: PendingAuthorizationStore, row: OAuthAuthorization
) -> None:
    """Delete a spent pending row and commit, whatever went wrong."""
    with contextlib.suppress(Exception):
        await pending.finish(row)
        await db.commit()


async def _persist_connection(
    db: AsyncSession,
    crypto: SecretCrypto,
    http_client: httpx.AsyncClient,
    *,
    row: OAuthAuthorization,
    tokens: TokenResponse,
    registration_id: UUID,
    registration_source: str,
    user_id: UUID,
    request_id: UUID,
    ip_hash: str,
) -> Connection:
    """Create or re-authorize the connection and store its tokens, atomically."""
    config = dict(row.draft_json.get("config") or {})
    config[CONFIG_ISSUER_KEY] = row.issuer
    config[CONFIG_RESOURCE_KEY] = row.resource
    config[CONFIG_SCOPE_KEY] = tokens.scope or row.scope
    config[CONFIG_TOKEN_ENDPOINT_KEY] = row.token_endpoint
    if row.revocation_endpoint:
        config[CONFIG_REVOCATION_ENDPOINT_KEY] = row.revocation_endpoint
    config.pop("oauth_pending_scope", None)

    name = row.draft_json.get("name")
    connection = await connections.create_connection_from_oauth(
        db,
        workspace_id=row.workspace_id,
        connection_id=row.connection_id,
        connector_type=row.connector_type,
        name=name if isinstance(name, str) and name else row.connector_type,
        config=config,
        created_by_user_id=user_id,
    )
    tokens_service = ConnectionTokenService(db, crypto, http_client)
    await tokens_service.store_tokens(
        connection,
        tokens,
        registration_id=registration_id,
        resource=row.resource,
        issuer=row.issuer,
        authorized_by_user_id=user_id,
    )
    audit.record(
        db,
        action="connection.oauth_authorized",
        target_type="connection",
        target_id=connection.id,
        workspace_id=row.workspace_id,
        actor_id=user_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "connector_type": row.connector_type,
            "issuer": row.issuer,
            "resource": row.resource,
            "scopes": tokens.scope or row.scope,
            "registration_source": registration_source,
        },
    )
    return connection


# --- Device flow -------------------------------------------------------

_DEVICE_START_GENERIC = "The provider refused to start a device sign-in for this app."
# What the person can do about a refusal, keyed by the provider's own error
# code. A device sign-in fails at start for one of a few nameable reasons, and
# the fix for each is on the provider's side; saying which one saves a round
# of guessing. GitHub Apps ship with the device flow switched off, so the
# first of these is the one people actually hit.
_DEVICE_START_REFUSALS: dict[str, str] = {
    "device_flow_disabled": (
        "{provider} has device sign-in turned off for this app. In the app's settings on "
        '{provider}, turn on "Enable Device Flow", save, and try again.'
    ),
    "unauthorized_client": (
        "{provider} does not allow this app to use the device sign-in. Check the app's "
        "settings on {provider}, or connect with a redirect instead."
    ),
    "invalid_client": (
        "{provider} no longer recognises this app's client id. Check the client id under "
        "Settings → OAuth."
    ),
    "invalid_scope": (
        "{provider} refused one of the permissions Jhin asked for. Check the app's "
        "permissions on {provider}."
    ),
}
_DEVICE_PROVIDER_NAMES: dict[str, str] = {"github": "GitHub"}


def _device_start_refusal(connector_type: str, error_code: str) -> str:
    provider = _DEVICE_PROVIDER_NAMES.get(connector_type, "The provider")
    template = _DEVICE_START_REFUSALS.get(error_code)
    if template is None:
        return _DEVICE_START_GENERIC
    return template.format(provider=provider)


async def start_device_flow(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    settings: Settings,
    payload: OAuthDeviceStartIn,
) -> OAuthDeviceStartOut:
    """Begin a device-code authorization: no redirect URI, no client secret.

    This is the answer for an instance the internet cannot reach — a laptop,
    a private network, anything without TLS — which is precisely why it never
    sends a client secret, at start, at poll, or at refresh.
    """
    provider = _static_provider_for(payload.connector_type)
    if provider is None or not provider.device_authorization_endpoint:
        raise _bad_request("This app does not offer the device-code sign-in.")

    store = OAuthClientStore(db, crypto)
    existing = await store.get(
        ctx.workspace_id,
        issuer=provider.issuer,
        redirect_uri=redirect_module.redirect_uri(settings),
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This workspace has not registered Jhin with this provider yet. Add the "
                "client id in Settings → OAuth, then connect."
            ),
        )
    registration, credentials = existing
    scope = " ".join(provider.default_scopes)
    try:
        grant = await start_device_authorization(
            http_client,
            device_authorization_endpoint=provider.device_authorization_endpoint,
            client_id=credentials.client_id,
            scope=scope,
        )
    except TransientOAuthError as exc:
        raise _upstream_unavailable(
            "The provider could not be reached. Try again shortly."
        ) from exc
    except ClientForgottenError as exc:
        logger.warning(
            "oauth.device_start_refused",
            connector_type=payload.connector_type,
            error_code="invalid_client",
        )
        raise _bad_request(_device_start_refusal(payload.connector_type, "invalid_client")) from exc
    except TokenError as exc:
        logger.warning(
            "oauth.device_start_refused",
            connector_type=payload.connector_type,
            error_code=exc.error_code,
        )
        raise _bad_request(_device_start_refusal(payload.connector_type, exc.error_code)) from exc
    except (OAuthError, EndpointPolicyError) as exc:
        raise _bad_request(_DEVICE_START_GENERIC) from exc

    ttl = int((grant.expires_at - datetime.now(UTC)).total_seconds())
    pending = PendingAuthorizationStore(db, crypto)
    row, handle = await pending.create(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        flow="device_code",
        connector_type=payload.connector_type,
        ttl_seconds=min(max(ttl, 1), DEVICE_FLOW_MAX_TTL_SECONDS),
        client_registration_id=registration.id,
        issuer=provider.issuer,
        token_endpoint=provider.token_endpoint,
        revocation_endpoint=provider.revocation_endpoint or None,
        scope=scope,
        verifier=grant.device_code,
        draft={"name": payload.name, "config": dict(payload.config)},
        poll_interval_seconds=grant.interval_seconds,
    )
    await db.commit()
    return OAuthDeviceStartOut(
        handle=handle,
        user_code=grant.user_code,
        verification_uri=grant.verification_uri,
        verification_uri_complete=grant.verification_uri_complete,
        expires_at=row.expires_at,
        interval_seconds=row.poll_interval_seconds,
    )


@dataclass(frozen=True, slots=True)
class DevicePollResult:
    status: str
    interval_seconds: int | None
    connection: Connection | None


async def poll_device_flow(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    http_client: httpx.AsyncClient,
    handle: str,
    *,
    request_id: UUID,
    ip_hash: str,
) -> DevicePollResult:
    """Ask the provider once whether the device grant has been approved.

    The pending row is *peeked* while waiting and only claimed at the moment
    a token actually arrives, so polling never burns the grant it is waiting
    on, and the claim is still exactly once.
    """
    pending = PendingAuthorizationStore(db, crypto)
    try:
        row = await pending.peek(
            handle=handle,
            expected_user_id=ctx.user.id,
            expected_workspace_id=ctx.workspace_id,
            expected_flow="device_code",
        )
    except PendingAuthorizationInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_INVALID_ATTEMPT_DETAIL
        ) from exc

    clients = OAuthClientStore(db, crypto)
    if row.client_registration_id is None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=_INVALID_ATTEMPT_DETAIL)
    try:
        registration, credentials = await clients.get_by_id(
            row.workspace_id, row.client_registration_id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=_INVALID_ATTEMPT_DETAIL
        ) from exc

    device_code = await pending.reveal_device_code(row)
    try:
        result = await poll_device_token(
            http_client,
            token_endpoint=row.token_endpoint,
            client_id=credentials.client_id,
            device_code=device_code,
            # No client secret, ever, on this path. GitHub does not want one,
            # and an instance using device flow is one that cannot safely
            # hold one anyway.
            client_secret=None,
        )
    except DeviceAuthorizationDenied:
        await _abandon(db, pending, row)
        return DevicePollResult(status="denied", interval_seconds=None, connection=None)
    except DeviceCodeExpired:
        await _abandon(db, pending, row)
        return DevicePollResult(status="expired", interval_seconds=None, connection=None)
    except TransientOAuthError:
        return DevicePollResult(
            status="pending", interval_seconds=row.poll_interval_seconds, connection=None
        )
    except (OAuthError, EndpointPolicyError, httpx.HTTPError) as exc:
        await _abandon(db, pending, row)
        raise _bad_request(
            "The provider refused this device sign-in. Start again from Apps."
        ) from exc

    if isinstance(result, DeviceTokenPending):
        interval = next_poll_interval(row.poll_interval_seconds, result)
        if interval != row.poll_interval_seconds:
            row.poll_interval_seconds = interval
            await db.commit()
        return DevicePollResult(
            status="slow_down" if result.reason == "slow_down" else "pending",
            interval_seconds=interval,
            connection=None,
        )

    claimed = await pending.claim(
        handle=handle,
        expected_user_id=ctx.user.id,
        expected_workspace_id=ctx.workspace_id,
        expected_flow="device_code",
    )
    connection = await _persist_connection(
        db,
        crypto,
        http_client,
        row=claimed,
        tokens=result,
        registration_id=registration.id,
        registration_source=registration.source,
        user_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await pending.finish(claimed)
    await db.commit()
    return DevicePollResult(status="connected", interval_seconds=None, connection=connection)


# --- Client registrations ----------------------------------------------


async def list_clients(
    db: AsyncSession, crypto: SecretCrypto, ctx: WorkspaceContext
) -> list[OAuthClientOut]:
    store = OAuthClientStore(db, crypto)
    rows = await store.list(ctx.workspace_id)
    rows_with_counts = (
        await db.execute(
            select(Connection.oauth_client_registration_id, func.count(Connection.id))
            .where(
                Connection.workspace_id == ctx.workspace_id,
                Connection.oauth_client_registration_id.is_not(None),
            )
            .group_by(Connection.oauth_client_registration_id)
        )
    ).all()
    counts: dict[UUID, int] = {
        registration_id: int(count)
        for registration_id, count in rows_with_counts
        if registration_id is not None
    }
    return [
        OAuthClientOut(
            id=row.id,
            issuer=row.issuer,
            redirect_uri=row.redirect_uri,
            client_id=row.client_id,
            client_secret_configured=row.client_secret_id is not None,
            token_endpoint_auth_method=row.token_endpoint_auth_method,
            source=_source_of(row.source),
            scopes=row.scopes,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            connection_count=int(counts.get(row.id, 0)),
        )
        for row in rows
    ]


async def create_client(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    settings: Settings,
    payload: OAuthClientCreate,
    *,
    request_id: UUID,
    ip_hash: str,
) -> OAuthClientOut:
    """Store a client id (and secret) an admin registered by hand.

    The issuer is validated as a URL against the same outbound policy every
    other provider URL goes through: it is what future discovery and token
    calls will be keyed by, and a workspace admin is not a reason to skip it.
    """
    try:
        issuer = validate_oauth_url(payload.issuer, kind="issuer")
    except EndpointPolicyError as exc:
        raise _bad_request(
            "That issuer is not an address this Jhin install is allowed to reach."
        ) from exc
    if payload.token_endpoint_auth_method != "none" and not payload.client_secret:
        raise _bad_request("This sign-in method needs a client secret as well as a client id.")

    store = OAuthClientStore(db, crypto)
    row = await store.save(
        workspace_id=ctx.workspace_id,
        issuer=issuer,
        redirect_uri=redirect_module.redirect_uri(settings),
        credentials=ClientCredentials(
            client_id=payload.client_id,
            client_secret=payload.client_secret or None,
            token_endpoint_auth_method=payload.token_endpoint_auth_method,
        ),
        scopes=payload.scopes,
        source="manual",
        created_by_user_id=ctx.user.id,
    )
    audit.record(
        db,
        action="oauth.client_registered",
        target_type="oauth_client_registration",
        target_id=row.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"issuer": issuer, "source": "manual"},
    )
    await db.commit()
    return OAuthClientOut(
        id=row.id,
        issuer=row.issuer,
        redirect_uri=row.redirect_uri,
        client_id=row.client_id,
        client_secret_configured=row.client_secret_id is not None,
        token_endpoint_auth_method=row.token_endpoint_auth_method,
        source="manual",
        scopes=row.scopes,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        connection_count=0,
    )


async def delete_client(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    registration_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    store = OAuthClientStore(db, crypto)
    try:
        row, _credentials = await store.get_by_id(ctx.workspace_id, registration_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OAuth client not found"
        ) from exc
    issuer = row.issuer
    await store.forget(ctx.workspace_id, registration_id)
    audit.record(
        db,
        action="oauth.client_removed",
        target_type="oauth_client_registration",
        target_id=registration_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"issuer": issuer},
    )
    await db.commit()


# --- GitHub App manifest -----------------------------------------------


def _github_oauth_module() -> Any:
    """The GitHub manifest helpers, or ``None`` when they are not installed."""
    try:
        from jhin_connectors.github import oauth as github_oauth
    except ImportError:  # pragma: no cover - GitHub OAuth helpers not installed
        return None
    return github_oauth


async def start_github_app_manifest(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    settings: Settings,
    payload: GitHubAppManifestIn,
) -> GitHubAppManifestOut:
    """Build the form that makes GitHub create this instance's own app.

    The alternative is asking an admin to fill in eight fields on GitHub's app
    form and then copy four values back, which is exactly the friction this
    whole feature exists to remove. The state is a pending row like any other
    — single-use, bound to this admin's session — with the one-hour TTL
    GitHub's conversion code lives for.
    """
    github_oauth = _github_oauth_module()
    if github_oauth is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This Jhin install cannot create GitHub Apps.",
        )
    app_url = settings.app_url.strip().rstrip("/")
    callback_url = redirect_module.redirect_uri(settings)
    manifest = github_oauth.build_app_manifest(
        app_name=payload.app_name,
        homepage_url=app_url,
        redirect_url=redirect_module.github_app_redirect_uri(settings),
        callback_url=callback_url,
        setup_url=f"{app_url}/settings/oauth",
        webhook_url=None,
    )
    pending = PendingAuthorizationStore(db, crypto)
    row, handle = await pending.create(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        flow="github_app_manifest",
        connector_type="github",
        ttl_seconds=GITHUB_MANIFEST_TTL_SECONDS,
        issuer=GITHUB_ISSUER,
        redirect_uri=callback_url,
        draft={"organization": payload.organization or ""},
    )
    await db.commit()
    return GitHubAppManifestOut(
        post_url=github_oauth.manifest_post_target(organization=payload.organization),
        manifest=manifest,
        state=handle,
        expires_at=row.expires_at,
    )


async def complete_github_app_manifest(
    db: AsyncSession,
    crypto: SecretCrypto,
    http_client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    state: str,
    code: str | None,
    request_id: UUID,
    ip_hash: str,
) -> bool:
    """Turn GitHub's conversion code into this workspace's stored app credentials.

    Same refusal discipline as the OAuth callback: the state is claimed once,
    checked against the initiating user's session, and every failure produces
    the identical body. The private key and webhook secret are registered with
    the redactor by the conversion helper before they reach this function.
    """
    pending = PendingAuthorizationStore(db, crypto)
    try:
        row = await pending.claim(
            handle=state, expected_user_id=user_id, expected_flow="github_app_manifest"
        )
    except PendingAuthorizationInvalid as exc:
        raise invalid_attempt() from exc
    if not code:
        await _abandon(db, pending, row)
        return False

    github_oauth = _github_oauth_module()
    if github_oauth is None:
        await _abandon(db, pending, row)
        return False
    try:
        app = await github_oauth.convert_app_manifest(http_client, code)
    except (OAuthError, EndpointPolicyError, httpx.HTTPError):
        logger.warning("oauth.github_app_conversion_failed")
        await _abandon(db, pending, row)
        return False

    store = OAuthClientStore(db, crypto)
    await store.save(
        workspace_id=row.workspace_id,
        issuer=GITHUB_ISSUER,
        redirect_uri=redirect_module.redirect_uri(settings),
        credentials=ClientCredentials(
            client_id=app.client_id,
            client_secret=app.client_secret,
            token_endpoint_auth_method="client_secret_post",
        ),
        scopes="",
        source="manual",
        created_by_user_id=user_id,
    )
    audit.record(
        db,
        action="oauth.github_app_created",
        target_type="oauth_client_registration",
        workspace_id=row.workspace_id,
        actor_id=user_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"issuer": GITHUB_ISSUER, "app_slug": app.slug},
    )
    await pending.finish(row)
    await db.commit()
    return True


# --- Re-authorization --------------------------------------------------


def provider_key_for(connection: Connection) -> str | None:
    """The static provider this connection was authorized against, if any.

    Re-authorization has to take the same path the first authorization did:
    a native connector goes back to its provider entry, an MCP server goes
    back through discovery. Reading it off the connector type keeps the
    Reconnect button from needing the caller to remember which.
    """
    provider = _static_provider_for(connection.connector_type)
    return str(provider.key) if provider is not None else None
