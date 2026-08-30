"""The OAuth HTTP surface (``docs/architecture/oauth.md``).

Two routers, deliberately separate.

``oauth_public_router`` carries the two provider callbacks and the
redirect-URI lookup. It is not workspace-prefixed, because a provider
redirecting a browser knows nothing about workspaces — which workspace an
authorization belongs to is read out of the pending row, never out of the
request. The callbacks declare no CSRF dependency: they are provider-driven
top-level ``GET`` navigations, and their forgery defense is the opaque state
handle plus the requirement that the request carry the *initiating user's own
session cookie*, which a cross-site top-level ``GET`` does send under
``SameSite=Lax`` and which no attacker can supply.

``oauth_router`` is the workspace-scoped, admin-only half. Every route on it
requires a workspace admin, and every route that mints, holds, or hands out
material that becomes a credential is sealed against API keys at every scope
in ``access/route_scopes.py``: reachable from a live browser session and
nothing else. The two routes whose request bodies carry credential material
read them through the same bounded, sensitive-body path the connections
router already uses, so a client secret never travels through generic request
handling.

No route here accepts a ``redirect_uri``, a ``client_id``, or a return URL
from a request, and none may be added. That absence is what keeps Jhin a leaf
OAuth client rather than a proxy somebody can aim somewhere else.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from jhin_api.connections.router import (
    json_request_body,
    parse_sensitive_json,
    serialize_connection,
)
from jhin_api.deps import (
    AdminCtx,
    CurrentAuth,
    DbSession,
    OAuthHttpClientDep,
    OptionalAuth,
    SecretCryptoDep,
    get_settings_dep,
)
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.oauth import redirect as redirect_module
from jhin_api.oauth import service
from jhin_api.oauth.schemas import (
    GitHubAppManifestIn,
    GitHubAppManifestOut,
    OAuthClientCreate,
    OAuthClientOut,
    OAuthDevicePollIn,
    OAuthDevicePollOut,
    OAuthDeviceStartIn,
    OAuthDeviceStartOut,
    OAuthProbeIn,
    OAuthProbeOut,
    OAuthRedirectOut,
    OAuthStartIn,
    OAuthStartOut,
)
from jhin_api.security.csrf import csrf_protect
from jhin_api.settings import Settings
from jhin_observability import get_logger
from jhin_workflows.oauth_refresh import OAuthRefreshInput, ensure_oauth_refresh

logger = get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

#: The callbacks accept these query parameters and echo none of them. The
#: bounds exist so somebody who reaches this URL cannot make us allocate
#: megabytes before we refuse.
MAX_CALLBACK_PARAM_LENGTH = 2_048
MAX_STATE_LENGTH = 256

oauth_public_router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])

oauth_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/oauth",
    tags=["oauth"],
    dependencies=[Depends(csrf_protect)],
)


def _no_store(location: str) -> Response:
    """A 303 that no cache may keep. A callback response is single-use."""
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location, "Cache-Control": "no-store"},
    )


async def _ensure_refresher(request: Request, workspace_id: UUID, settings: Settings) -> None:
    """Make sure this workspace has a running token refresher. Never raises.

    Started here, after the first successful authorization, rather than at
    boot: an install with no OAuth connections should run no refresher, and
    the workflow id makes starting it idempotent, so nothing has to remember
    whether it is already running. Temporal being briefly unreachable must
    never turn a connection the user just made into an error — the refresher
    is upkeep, and the on-use path covers the gap until the next authorization
    starts it.
    """
    try:
        client = await request.app.state.temporal_provider.get()
        await ensure_oauth_refresh(
            client,
            OAuthRefreshInput(
                workspace_id=str(workspace_id),
                interval_seconds=settings.oauth_refresh_interval_seconds,
            ),
        )
    except Exception:
        logger.warning("oauth.refresher_not_started")


# --- Global routes -----------------------------------------------------


@oauth_public_router.get("/redirect-uri")
async def get_redirect_uri(_auth: CurrentAuth, settings: SettingsDep) -> OAuthRedirectOut:
    """The callback URL an operator pastes into a provider's app settings.

    Shown verbatim, with a copy button, wherever somebody is asked to bring
    their own OAuth app, and again in Settings → OAuth — because "what
    exactly do I paste?" is the one question that stalls a self-hosted OAuth
    setup.
    """
    return OAuthRedirectOut(
        redirect_uri=redirect_module.redirect_uri(settings),
        github_app_redirect_uri=redirect_module.github_app_redirect_uri(settings),
        is_https=redirect_module.is_https_redirect(settings),
        is_loopback=redirect_module.is_loopback_redirect(settings),
        configured_via=redirect_module.configured_via(settings),
    )


@oauth_public_router.get("/callback", status_code=status.HTTP_303_SEE_OTHER)
async def oauth_callback(
    request: Request,
    auth: OptionalAuth,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
    state: Annotated[str, Query(min_length=1, max_length=MAX_STATE_LENGTH)],
    code: Annotated[str | None, Query(max_length=MAX_CALLBACK_PARAM_LENGTH)] = None,
    iss: Annotated[str | None, Query(max_length=MAX_CALLBACK_PARAM_LENGTH)] = None,
    error: Annotated[str | None, Query(max_length=MAX_CALLBACK_PARAM_LENGTH)] = None,
) -> Response:
    """Where a provider sends the browser back. The security-critical route.

    Five checks stand between this request and a stored token, and all five
    must pass: the handle is well formed, a row exists for its hash, the
    single-use claim wins, the live session belongs to the user who started
    the flow, and ``iss`` byte-matches the issuer recorded before we
    redirected. Every failure returns the same 400 with the same body, so a
    probe learns nothing from which one it tripped.

    ``error_description`` and ``error_uri`` are accepted by the URL and read
    by nothing. They are text whoever reached this URL chose, and rendering
    them would let them write our error page. ``error`` itself is used as a
    boolean and never shown.

    The ``Location`` is built by :func:`redirect_module.app_return_url` from
    settings plus a connection id proven to be thirty-two hex characters.
    Nothing from this request can reach it, which is what closes the
    open-redirect surface by construction rather than by filtering.
    """
    if auth is None:
        # The session died while the person was at the provider — the most
        # likely real-world failure, and the one that used to escape the
        # "every failure looks the same" promise by answering a raw 401 JSON
        # body. Nothing is claimed and no token is exchanged without a
        # session; this only decides what the browser is shown.
        return _no_store(redirect_module.app_return_url(settings, public_id=None, error="failed"))
    result = await service.complete_authorization(
        db,
        crypto,
        http_client,
        settings,
        user_id=auth.user.id,
        state=state,
        code=code,
        iss=iss,
        provider_error=error,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    if result.connection is not None:
        await _ensure_refresher(request, result.connection.workspace_id, settings)
    return _no_store(
        redirect_module.app_return_url(
            settings,
            public_id=result.connection.public_id if result.connection is not None else None,
            error=result.error,
        )
    )


@oauth_public_router.get("/github-app/callback", status_code=status.HTTP_303_SEE_OTHER)
async def github_app_callback(
    request: Request,
    auth: CurrentAuth,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
    state: Annotated[str, Query(min_length=1, max_length=MAX_STATE_LENGTH)],
    code: Annotated[str | None, Query(max_length=MAX_CALLBACK_PARAM_LENGTH)] = None,
) -> Response:
    """Where GitHub sends the browser after creating this instance's own app.

    Same five checks as the OAuth callback, same single refusal. The
    conversion code is worth a full set of app credentials for exactly one
    hour, which is why the pending row for this flow is the only one with a
    TTL longer than ten minutes and why it is still single-use.
    """
    created = await service.complete_github_app_manifest(
        db,
        crypto,
        http_client,
        settings,
        user_id=auth.user.id,
        state=state,
        code=code,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _no_store(redirect_module.github_app_return_url(settings, created=created))


# --- Workspace routes --------------------------------------------------


@oauth_router.post("/probe")
async def probe_connector(
    payload: OAuthProbeIn,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
) -> OAuthProbeOut:
    """Ask a connector how it signs in, before showing anybody a form.

    This is what demotes API-key entry: the panel never offers a key field
    until the server itself has said it has nothing better on offer. The
    catalog's ``auth_hint`` is not consulted — it labels the Apps library, and
    as a protocol signal it is wrong more often than right.
    """
    return await service.probe(db, crypto, ctx, http_client, settings, payload)


@oauth_router.post(
    "/start",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=json_request_body(OAuthStartIn),
)
async def start_authorization(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
) -> OAuthStartOut:
    """Mint a pending authorization and return the URL the browser leaves for.

    The returned URL carries the client id, the PKCE challenge, the state
    handle, the scopes, and the resource — and no secret of any kind. The
    verifier stays encrypted on this side until the callback needs it.

    Read through the bounded sensitive-body path: the configuration in this
    payload is meant to be non-secret, but it is admin-supplied and adjacent
    to credentials, and the cheap thing is to keep it off the generic path.
    """
    payload = await parse_sensitive_json(
        request,
        OAuthStartIn,
        invalid_detail="Authorization payload is invalid",
        too_large_detail="Authorization payload is too large",
    )
    return await service.start_authorization(
        db,
        crypto,
        ctx,
        http_client,
        settings,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@oauth_router.post("/device/start", status_code=status.HTTP_201_CREATED)
async def start_device_flow(
    payload: OAuthDeviceStartIn,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
) -> OAuthDeviceStartOut:
    """Begin a device-code sign-in: a short code, no redirect, no secret.

    The answer for an install the internet cannot reach — a laptop, a private
    network, anything without a public HTTPS origin — where a redirect-based
    flow cannot work at all and pasting a personal access token used to be the
    only remaining option.
    """
    return await service.start_device_flow(db, crypto, ctx, http_client, settings, payload)


@oauth_router.post("/device/poll")
async def poll_device_flow(
    payload: OAuthDevicePollIn,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
) -> OAuthDevicePollOut:
    """Ask whether the person has approved the device code yet.

    The handle is Jhin's own; the provider's device code never leaves this
    server. A ``slow_down`` answer raises the poll interval permanently, as
    RFC 8628 requires — a client that returns to its old cadence gets
    throttled and then rejected.
    """
    result = await service.poll_device_flow(
        db,
        crypto,
        ctx,
        http_client,
        payload.handle,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    if result.connection is not None:
        await _ensure_refresher(request, ctx.workspace_id, settings)
    connection = (
        await serialize_connection(db, result.connection) if result.connection is not None else None
    )
    return OAuthDevicePollOut(
        status=result.status,
        interval_seconds=result.interval_seconds,
        connection=connection,
    )


@oauth_router.get("/clients")
async def list_oauth_clients(
    ctx: AdminCtx, db: DbSession, crypto: SecretCryptoDep
) -> list[OAuthClientOut]:
    """This workspace's OAuth app registrations. Client ids, never secrets."""
    return await service.list_clients(db, crypto, ctx)


@oauth_router.post(
    "/clients",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=json_request_body(OAuthClientCreate),
)
async def create_oauth_client(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
) -> OAuthClientOut:
    """Store a client id (and secret) an admin registered by hand.

    Needed once per workspace per server that has no dynamic registration.
    Every later connection to that same server skips this entirely — which is
    the difference between a one-time setup and a tax on every connection.
    """
    payload = await parse_sensitive_json(
        request,
        OAuthClientCreate,
        invalid_detail="OAuth client payload is invalid",
        too_large_detail="OAuth client payload is too large",
    )
    return await service.create_client(
        db,
        crypto,
        ctx,
        settings,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@oauth_router.delete("/clients/{registration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_oauth_client(
    registration_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> Response:
    """Forget a registration and every secret it owns."""
    await service.delete_client(
        db,
        crypto,
        ctx,
        registration_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@oauth_router.post("/github-app/manifest", status_code=status.HTTP_201_CREATED)
async def create_github_app_manifest(
    payload: GitHubAppManifestIn,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
) -> GitHubAppManifestOut:
    """The form the browser POSTs to GitHub to create this instance's own app.

    One click instead of a page of copy-paste: GitHub creates the app, sends
    the browser to Jhin's callback with a conversion code, and Jhin exchanges
    that for the app's id, client id, client secret, webhook secret, and
    private key. The whole handshake has to finish inside an hour, so this is
    the only pending authorization whose TTL is longer than ten minutes.
    """
    return await service.start_github_app_manifest(db, crypto, ctx, settings, payload)


__all__ = ["oauth_public_router", "oauth_router"]
