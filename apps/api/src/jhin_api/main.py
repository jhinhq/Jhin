"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jhin_api import __version__
from jhin_api.health.router import router as health_router
from jhin_api.settings import Settings, get_settings
from jhin_db import create_engine
from jhin_observability import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("api", settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Engine construction is lazy: no connection is made until first use,
        # and tables are never auto-created (migrations own the schema).
        app.state.engine = create_engine(settings.database_url)
        logger.info("api.started", app_name=settings.app_name, env=settings.app_env)
        yield
        await app.state.engine.dispose()
        logger.info("api.stopped")

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    return app


app = create_app()
