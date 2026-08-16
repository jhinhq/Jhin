"""FastAPI application factory."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from jhin_api import __version__
from jhin_api.agents.router import router as agents_router
from jhin_api.audit.router import router as audit_router
from jhin_api.auth.router import router as auth_router
from jhin_api.health.router import router as health_router
from jhin_api.models.router import profiles_router, providers_router
from jhin_api.org.router import router as org_router
from jhin_api.secrets.router import router as secrets_router
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.settings import Settings, get_settings
from jhin_api.teams.router import router as teams_router
from jhin_api.workspaces.router import router as workspaces_router
from jhin_db import create_engine, create_session_factory
from jhin_domain import new_uuid7
from jhin_observability import configure_logging, get_logger
from jhin_secrets import SecretCrypto, load_master_key
from jhin_secrets.crypto import MasterKeyError
from jhin_secrets.redaction import redact_event_dict

logger = get_logger(__name__)


def _load_secret_crypto() -> SecretCrypto | None:
    try:
        return SecretCrypto(load_master_key())
    except MasterKeyError as exc:
        logger.warning("secrets.master_key_unavailable", error=str(exc))
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Known secret values are scrubbed from every log record (plan 13.5).
    configure_logging("api", settings.log_level, extra_processors=[redact_event_dict])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Engine construction is lazy: no connection is made until first use,
        # and tables are never auto-created (migrations own the schema).
        app.state.engine = create_engine(settings.database_url)
        app.state.session_factory = create_session_factory(app.state.engine)
        logger.info("api.started", app_name=settings.app_name, env=settings.app_env)
        yield
        await app.state.engine.dispose()
        logger.info("api.stopped")

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.secret_crypto = _load_secret_crypto()
    app.state.login_limiter = LoginRateLimiter(
        settings.login_max_attempts, settings.login_window_seconds
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = new_uuid7()
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(workspaces_router)
    app.include_router(teams_router)
    app.include_router(agents_router)
    app.include_router(org_router)
    app.include_router(audit_router)
    app.include_router(secrets_router)
    app.include_router(providers_router)
    app.include_router(profiles_router)
    return app


app = create_app()
