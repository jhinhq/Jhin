"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from opentelemetry.trace import Span, SpanKind
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jhin_api import __version__
from jhin_api.access.api_keys import record_usage
from jhin_api.access.router import api_keys_router, invitations_router, public_invitations_router
from jhin_api.agents.router import router as agents_router
from jhin_api.approvals.router import router as approvals_router
from jhin_api.audit.router import router as audit_router
from jhin_api.auth.router import router as auth_router
from jhin_api.connections.router import catalog_router as connectors_catalog_router
from jhin_api.connections.router import router as connections_router
from jhin_api.conversations.router import conversations_router, workspace_feed_router
from jhin_api.coordination.router import router as coordination_router
from jhin_api.directory.router import router as directory_router
from jhin_api.health.router import router as health_router
from jhin_api.media.router import router as media_router
from jhin_api.memory.router import router as memory_router
from jhin_api.models.router import profiles_router, providers_router, spend_router
from jhin_api.openapi import (
    CONTACT_INFO,
    DESCRIPTION,
    LICENSE_INFO,
    SUMMARY,
    build_openapi,
    tag_metadata,
)
from jhin_api.openapi import router as openapi_router
from jhin_api.org.router import router as org_router
from jhin_api.policy.router import router as policy_router
from jhin_api.secrets.router import router as secrets_router
from jhin_api.security.headers import SecurityHeadersMiddleware
from jhin_api.security.limits import RequestSizeLimitMiddleware
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.validation import safe_validation_error_handler
from jhin_api.settings import Settings, get_settings
from jhin_api.skills.router import agent_skills_router, skill_sources_router, skills_router
from jhin_api.tasks.router import agent_actions_router, runs_router, tasks_router
from jhin_api.teams.router import router as teams_router
from jhin_api.temporal import TemporalClientProvider
from jhin_api.triggers.router import router as triggers_router
from jhin_api.webhooks.router import router as webhooks_router
from jhin_api.workspaces.router import router as workspaces_router
from jhin_db import create_engine, create_session_factory
from jhin_domain import new_uuid7
from jhin_observability import (
    SafeErrorCode,
    bind_context,
    extract_trace_context,
    get_logger,
    initialize_observability,
    normalize_span_attributes,
    record_span_error,
    safe_error,
    safe_span,
    service_version,
)
from jhin_secrets import SecretCrypto, load_master_key
from jhin_secrets.crypto import MasterKeyError
from jhin_secrets.redaction import redact_event_dict

logger = get_logger(__name__)


def normalize_http_method(method: str) -> str:
    normalized = method.upper()
    return (
        normalized
        if normalized
        in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        }
        else "other"
    )


def normalize_http_route(scope: Scope) -> str:
    route = scope.get("route")
    if not isinstance(route, APIRoute):
        return "other"
    template = route.path
    if not isinstance(template, str) or not 1 <= len(template) <= 200:
        return "other"
    return "/api/:path*" if template.startswith("/api/") else "other"


def set_http_span_result(
    span: Span,
    *,
    method: str,
    route: str,
    status_code: int,
) -> None:
    status = status_code if 100 <= status_code <= 599 else 500
    normalized_method = normalize_http_method(method)
    status_class = f"{status // 100}xx"
    attributes = normalize_span_attributes(
        {
            "http.request.method": normalized_method,
            "http.route": route,
            "http.response.status_code": status,
            "http.response.status_class": status_class,
        }
    )
    for key, value in attributes.items():
        span.set_attribute(key, value)


class HttpObservabilityMiddleware:
    """Own the complete HTTP request telemetry and request-ID lifecycle."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_uuid7()
        scope.setdefault("state", {})["request_id"] = request_id
        parent = extract_trace_context(Headers(scope=scope))
        tracer = scope["app"].state.observability.tracer
        response_started = False
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start" and not response_started:
                candidate_status = message.get("status", 500)
                status_code = candidate_status if isinstance(candidate_status, int) else 500
                response_started = True
                outgoing = dict(message)
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"X-Request-ID", str(request_id).encode("ascii")))
                outgoing["headers"] = headers
                await send(outgoing)
                return
            await send(message)

        with (
            bind_context(request_id=request_id),
            safe_span(
                "http.server.request",
                tracer=tracer,
                kind=SpanKind.SERVER,
                context=parent,
            ) as span,
        ):
            try:
                try:
                    await self.app(scope, receive, send_with_request_id)
                except Exception as exc:
                    record_span_error(
                        span,
                        safe_error(exc, code=SafeErrorCode.INTERNAL_ERROR),
                    )
                    logger.error(
                        "api.request_failed",
                        error_code=SafeErrorCode.INTERNAL_ERROR.value,
                    )
                    if response_started:
                        raise
                    response = JSONResponse(
                        status_code=500,
                        content={"detail": "Internal server error"},
                    )
                    await response(scope, receive, send_with_request_id)
            finally:
                normalized_method = normalize_http_method(scope["method"])
                normalized_route = normalize_http_route(scope)
                normalized_status = status_code if 100 <= status_code <= 599 else 500
                status_class = f"{normalized_status // 100}xx"
                set_http_span_result(
                    span,
                    method=normalized_method,
                    route=normalized_route,
                    status_code=normalized_status,
                )
                logger.info(
                    "api.request_finished",
                    http_method=normalized_method,
                    http_route=normalized_route,
                    http_status_class=status_class,
                )


class ApiKeyUsageMiddleware:
    """Persist one usage row per API-key request, whatever the outcome.

    The auth dependency stashes the resolved key on ``request.state`` the
    moment it verifies, *before* the role and scope checks run, so denied calls
    (403 for a missing scope) are logged just as faithfully as successful ones
    — which is exactly what makes the log useful when investigating a key.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500

        async def observe(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                candidate = message.get("status", 500)
                status_code = candidate if isinstance(candidate, int) else 500
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            usage = scope.get("state", {}).get("api_key_usage")
            if usage is not None:
                await self._persist(scope, usage, status_code)

    async def _persist(self, scope: Scope, usage: Any, status_code: int) -> None:
        app = scope["app"]
        settings: Settings = app.state.settings
        route = scope.get("route")
        template = getattr(route, "path", None)
        try:
            async with app.state.session_factory() as session:
                await record_usage(
                    session,
                    api_key_id=usage.api_key_id,
                    workspace_id=usage.workspace_id,
                    acting_user_id=usage.acting_user_id,
                    method=str(scope.get("method", "")),
                    # Route template, never the raw URL: query strings can
                    # carry filter values that do not belong in a log.
                    path=template if isinstance(template, str) else str(scope.get("path", "")),
                    status_code=status_code,
                    ip_hash=usage.ip_hash,
                    retention_days=settings.api_key_usage_retention_days,
                )
        except Exception:
            # Telemetry must never turn a served request into a failed one.
            logger.warning(
                "api_key.usage_not_recorded",
                error_code=SafeErrorCode.INTERNAL_ERROR.value,
            )


def _load_secret_crypto() -> SecretCrypto | None:
    try:
        return SecretCrypto(load_master_key())
    except MasterKeyError:
        logger.warning(
            "secrets.master_key_unavailable",
            error_code=SafeErrorCode.INTERNAL_ERROR.value,
        )
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = initialize_observability(
            settings.observability_config(
                service_name="api",
                service_version=service_version("jhin-api"),
                extra_log_processors=(redact_event_dict,),
            )
        )
        engine = None
        active_error: BaseException | None = None
        active_traceback: TracebackType | None = None
        try:
            app.state.observability = runtime
            temporal_provider = TemporalClientProvider(settings, runtime)
            app.state.temporal_provider = temporal_provider
            app.state.secret_crypto = _load_secret_crypto()
            # Engine construction is lazy: no connection is made until first use,
            # and tables are never auto-created (migrations own the schema).
            engine = create_engine(settings.database_url, trace_sql=True, tracer=runtime.tracer)
            app.state.engine = engine
            app.state.session_factory = create_session_factory(engine)
            app.state.nats_client = None
            app.state.nats_connect_lock = asyncio.Lock()
            logger.info("api.started")
            yield
        except BaseException as error:
            active_error = error
            active_traceback = error.__traceback__

        cleanup_cancellation: asyncio.CancelledError | None = None
        cleanup_error: BaseException | None = None
        cleanup_traceback: TracebackType | None = None

        def remember(error: BaseException) -> None:
            nonlocal cleanup_cancellation, cleanup_error, cleanup_traceback
            if isinstance(error, asyncio.CancelledError):
                if cleanup_cancellation is None:
                    cleanup_cancellation = error
            elif cleanup_error is None:
                cleanup_error = error
                cleanup_traceback = error.__traceback__

        try:
            nats = getattr(app.state, "nats_client", None)
            if nats is not None and not nats.is_closed:
                await nats.close()
        except BaseException as error:
            remember(error)
        if engine is not None:
            try:
                await engine.dispose()
            except BaseException as error:
                remember(error)
        try:
            logger.info("api.stopped")
        except BaseException as error:
            remember(error)
        try:
            runtime.shutdown(timeout_millis=5_000)
        except BaseException as error:
            remember(error)

        if active_error is not None:
            raise active_error.with_traceback(active_traceback)
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_traceback)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary=SUMMARY,
        description=DESCRIPTION,
        contact=CONTACT_INFO,
        license_info=LICENSE_INFO,
        openapi_tags=tag_metadata(),
        # Relative, so the document is correct behind whatever host, port, and
        # reverse proxy this install happens to sit behind — and identical
        # across environments, which is what makes the committed snapshot in
        # docs/api/openapi.v1.json a meaningful diff target.
        servers=[{"url": "/", "description": "This Jhin install"}],
        lifespan=lifespan,
        # Interactive docs map the entire API surface for an unauthenticated
        # visitor. Useful in development, needless exposure in production —
        # signed-in users read the same document at /api/v1/openapi.json,
        # which the web app renders at /api-docs.
        docs_url="/docs" if settings.expose_api_docs else None,
        redoc_url="/redoc" if settings.expose_api_docs else None,
        openapi_url="/openapi.json" if settings.expose_api_docs else None,
    )
    # Adds auth schemes and the per-operation scope each route already
    # declares in access/route_scopes.py, so the reference cannot drift from
    # what the API enforces.
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    app.state.settings = settings
    app.state.login_limiter = LoginRateLimiter(
        account_max_attempts=settings.login_max_attempts,
        ip_max_attempts=settings.login_ip_max_attempts,
        half_life_seconds=float(settings.login_window_seconds),
        base_block_seconds=float(settings.login_base_block_seconds),
        account_max_block_seconds=float(settings.login_account_max_block_seconds),
        ip_max_block_seconds=float(settings.login_ip_max_block_seconds),
    )
    app.state.api_key_limiter = LoginRateLimiter(
        account_max_attempts=settings.api_key_max_attempts,
        ip_max_attempts=settings.api_key_ip_max_attempts,
        half_life_seconds=float(settings.login_window_seconds),
        base_block_seconds=float(settings.login_base_block_seconds),
        account_max_block_seconds=float(settings.login_account_max_block_seconds),
        ip_max_block_seconds=float(settings.login_ip_max_block_seconds),
    )
    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)

    # Order matters: middleware added last runs first, so security headers wrap
    # every response including CORS preflights and the body-size rejection.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(HttpObservabilityMiddleware)
    # Outside the observability layer so it sees the status a client actually
    # received, including the 500 that layer substitutes for an unhandled error.
    app.add_middleware(ApiKeyUsageMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.emit_hsts)

    app.include_router(health_router)
    app.include_router(openapi_router)
    app.include_router(auth_router)
    app.include_router(workspaces_router)
    app.include_router(invitations_router)
    app.include_router(public_invitations_router)
    app.include_router(api_keys_router)
    app.include_router(teams_router)
    app.include_router(agents_router)
    app.include_router(org_router)
    app.include_router(audit_router)
    app.include_router(secrets_router)
    app.include_router(providers_router)
    app.include_router(profiles_router)
    app.include_router(spend_router)
    app.include_router(tasks_router)
    app.include_router(runs_router)
    app.include_router(agent_actions_router)
    app.include_router(conversations_router)
    app.include_router(workspace_feed_router)
    app.include_router(directory_router)
    app.include_router(coordination_router)
    app.include_router(memory_router)
    app.include_router(skill_sources_router)
    app.include_router(skills_router)
    app.include_router(agent_skills_router)
    app.include_router(policy_router)
    app.include_router(approvals_router)
    app.include_router(connectors_catalog_router)
    app.include_router(connections_router)
    app.include_router(triggers_router)
    app.include_router(webhooks_router)
    app.include_router(media_router)
    return app


app = create_app()
