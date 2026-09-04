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

import contextlib
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
#: megabytes before we refuse. They are applied *inside* the handlers rather
#: than on the signature: a ``Query(max_length=...)`` makes FastAPI answer a
#: JSON 422 before the handler runs, and a JSON body in a browser is the dead
#: end these routes exist not to be.
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


def _is_navigation(request: Request) -> bool:
    """Whether this is a person's browser arriving, or a machine's guess.

    A prefetch that spends a single-use state, leaving the real navigation to
    find its row gone, is the classic cause of a callback that refuses a
    handle nobody misused. A request that *announces itself* as one is
    answered before anything is looked up.

    Absence proves nothing and is never held against a request: a browser
    sending none of these headers is treated as a navigation, so no real
    callback is ever lost to a header we did not receive.
    """
    headers = request.headers
    if "prefetch" in headers.get("sec-purpose", "").lower():
        return False
    if headers.get("purpose", "").strip().lower() == "prefetch":
        return False
    if headers.get("x-moz", "").strip().lower() == "prefetch":
        return False
    mode = headers.get("sec-fetch-mode", "").strip().lower()
    if mode and mode != "navigate":
        return False
    dest = headers.get("sec-fetch-dest", "").strip().lower()
    return not (dest and dest != "document")


def _bounded(value: str | None, limit: int) -> str | None:
    """A query value we are willing to look at, or nothing. Never a 422."""
    return value if value is not None and len(value) <= limit else None


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
        github_app_available=redirect_module.github_app_available(settings),
        github_app_permissions=service.github_app_permissions(),
        preferred_sign_in="device_code" if settings.oauth_prefer_device_code else "redirect",
    )


@oauth_public_router.get("/callback", status_code=status.HTTP_303_SEE_OTHER)
async def oauth_callback(
    request: Request,
    auth: OptionalAuth,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    iss: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    """Where a provider sends the browser back. The security-critical route.

    Five checks stand between this request and a stored token, and all five
    must pass: the handle is well formed, a row exists for its hash, the
    single-use claim wins, the live session belongs to the user who started
    the flow, and ``iss`` byte-matches the issuer recorded before we
    redirected.

    **No refusal has a body.** This handler has one ``return`` and it always
    builds a 303 from :func:`redirect_module.app_return_url`. That is why the
    query bounds are enforced in the body rather than on the signature: a
    ``Query(max_length=...)`` makes FastAPI answer a JSON 422 before the
    handler runs, which is exactly the dead end this route exists not to be.

    **Two tiers.** Everything decided before the single-use claim succeeds
    gets one flag, ``expired``, byte-identically, because any session at all
    can reach it. A caller with no session gets ``signed_out`` before the
    database is touched at all. Everything decided after the claim may name a
    cause: reaching that tier needs the raw 256-bit handle *and* the owner's
    browser.

    ``error_description`` and ``error_uri`` are accepted by the URL and read
    by nothing. They are text whoever reached this URL chose, and rendering
    them would let them write our error page. ``error`` itself is used as a
    boolean and never shown.

    The ``Location`` is built by :func:`redirect_module.app_return_url` from
    settings plus a connection id proven to be thirty-two hex characters and a
    connector type proven to match its pattern — both read out of a database
    column. Nothing from this request can reach it, which is what closes the
    open-redirect surface by construction rather than by filtering.
    """
    if not _is_navigation(request):
        # A prefetch or background fetch. Answered before anything is looked
        # up, so the single-use row survives for the real navigation. A plain
        # ``/apps`` because a prerendered document is what the person sees if
        # the prerender activates, and a 204 renders as a blank page.
        logger.debug("oauth.callback_prefetch_ignored", flow="authorization_code")
        return _no_store(redirect_module.app_return_url(settings, public_id=None))

    result = service.CallbackResult(connection=None, error="expired")
    handle = _bounded(state, MAX_STATE_LENGTH)
    bounded_code = _bounded(code, MAX_CALLBACK_PARAM_LENGTH)
    bounded_iss = _bounded(iss, MAX_CALLBACK_PARAM_LENGTH)
    bounded_error = _bounded(error, MAX_CALLBACK_PARAM_LENGTH)
    if auth is None:
        # The session died while the person was at the provider — the most
        # likely real-world failure, and the one that used to escape the
        # "every failure looks the same" promise by answering a raw 401 JSON
        # body. Nothing is claimed and no token is exchanged without a
        # session; this only decides what the browser is shown, and it is
        # decided before the database is touched at all.
        service.log_callback_refusal("no_session", flow="authorization_code")
        result = service.CallbackResult(connection=None, error="signed_out")
    elif handle is None:
        service.log_callback_refusal("state_malformed", flow="authorization_code")
    elif (
        (code is not None and bounded_code is None)
        or (iss is not None and bounded_iss is None)
        or (error is not None and bounded_error is None)
    ):
        # A refusal in its own right, never a silent drop: treating an
        # over-long ``code`` as absent would file "the provider sent something
        # absurd" under "the person declined", and skipping an over-long
        # ``iss`` would skip a check.
        service.log_callback_refusal("param_too_long", flow="authorization_code")
    else:
        try:
            result = await service.complete_authorization(
                db,
                crypto,
                http_client,
                settings,
                user_id=auth.user.id,
                state=handle,
                code=bounded_code,
                iss=bounded_iss,
                provider_error=bounded_error,
                request_id=req_id(request),
                ip_hash=ip_hash(request),
            )
        except Exception:
            # The last line of the promise: no exception from this subsystem
            # reaches a browser as a body. The reason is logged from the
            # closed vocabulary; the exception itself is not, because a
            # driver's message is not ours to render.
            with contextlib.suppress(Exception):
                await db.rollback()
            service.log_callback_refusal("internal_error", flow="authorization_code")
            result = service.CallbackResult(connection=None, error="expired")
        else:
            # A replay is a pure read plus a redirect: no Temporal call, no
            # network, nothing.
            if result.connection is not None and not result.replayed:
                await _ensure_refresher(request, result.connection.workspace_id, settings)
    return _no_store(
        redirect_module.app_return_url(
            settings,
            public_id=result.public_id,
            error=result.error,
            connector_type=result.connector_type,
        )
    )


@oauth_public_router.get("/github-app/callback", status_code=status.HTTP_303_SEE_OTHER)
async def github_app_callback(
    request: Request,
    auth: OptionalAuth,
    db: DbSession,
    crypto: SecretCryptoDep,
    settings: SettingsDep,
    http_client: OAuthHttpClientDep,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
) -> Response:
    """Where GitHub sends the browser after creating this instance's own app.

    Same five checks as the OAuth callback, and the same two tiers. The
    conversion code is worth a full set of app credentials for exactly one
    hour, which is why the pending row for this flow is the only one with a
    TTL longer than the authorization-code state and why it is still
    single-use.

    Every refusal decided *before* the claim lands on the shared recovery
    page with bytes identical to the OAuth callback's, so neither callback
    can be used as a differential oracle for the other. ``?github_app=`` is
    reached only past a successful claim, or from a receipt.

    A session that died while the person was on GitHub's form is handled
    exactly as the OAuth callback handles it: nothing is claimed, nothing is
    converted, and the browser is sent back to Apps with a flag rather than
    a raw 401 body. The pending row survives for a retry within the hour.
    """
    if not _is_navigation(request):
        logger.debug("oauth.callback_prefetch_ignored", flow="github_app_manifest")
        return _no_store(redirect_module.app_return_url(settings, public_id=None))

    result = service.CallbackResult(connection=None, error="expired")
    handle = _bounded(state, MAX_STATE_LENGTH)
    bounded_code = _bounded(code, MAX_CALLBACK_PARAM_LENGTH)
    if auth is None:
        service.log_callback_refusal("no_session", flow="github_app_manifest")
        result = service.CallbackResult(connection=None, error="signed_out")
    elif handle is None:
        service.log_callback_refusal("state_malformed", flow="github_app_manifest")
    elif code is not None and bounded_code is None:
        service.log_callback_refusal("param_too_long", flow="github_app_manifest")
    else:
        try:
            result = await service.complete_github_app_manifest(
                db,
                crypto,
                http_client,
                settings,
                user_id=auth.user.id,
                state=handle,
                code=bounded_code,
                request_id=req_id(request),
                ip_hash=ip_hash(request),
            )
        except Exception:
            with contextlib.suppress(Exception):
                await db.rollback()
            service.log_callback_refusal("internal_error", flow="github_app_manifest")
            result = service.CallbackResult(connection=None, error="expired")
    if result.manifest_created is not None:
        return _no_store(
            redirect_module.github_app_return_url(settings, created=result.manifest_created)
        )
    return _no_store(
        redirect_module.app_return_url(
            settings,
            public_id=None,
            error=result.error,
            connector_type=result.connector_type,
        )
    )


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

    The alternative to the browser sign-in for a native provider that offers
    RFC 8628 — chosen from a link on the consent step, or offered first when
    the registration has no client secret or ``OAUTH_PREFER_DEVICE_CODE`` is
    set. A provider that refuses to start it is answered with a sentence that
    names the fix, and names the browser sign-in when that is one.
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
