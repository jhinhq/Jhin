"""Route handlers for health endpoints. No business logic here — handlers
delegate to the health service (plan section 47)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from jhin_api import __version__
from jhin_api.health import service
from jhin_api.health.schemas import LivenessReport, ReadinessReport
from jhin_api.settings import Settings
from jhin_api.temporal import TemporalClientProvider

router = APIRouter(prefix="/api/v1", tags=["health"])


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def _temporal_provider(request: Request) -> TemporalClientProvider:
    provider: TemporalClientProvider = request.app.state.temporal_provider
    return provider


@router.get("/health")
async def health(settings: Annotated[Settings, Depends(_settings)]) -> LivenessReport:
    return LivenessReport(status="ok", app=settings.app_name, version=__version__)


@router.get("/health/ready", responses={503: {"model": ReadinessReport}})
async def health_ready(
    settings: Annotated[Settings, Depends(_settings)],
    engine: Annotated[AsyncEngine, Depends(_engine)],
    temporal_provider: Annotated[TemporalClientProvider, Depends(_temporal_provider)],
    response: Response,
) -> ReadinessReport:
    report = await service.readiness(settings, engine, temporal_provider)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
