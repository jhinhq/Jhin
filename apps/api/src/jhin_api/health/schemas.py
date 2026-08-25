"""Response contracts for health endpoints."""

from typing import Literal

from pydantic import BaseModel

DependencyState = Literal["ok", "error"]


class LivenessReport(BaseModel):
    status: Literal["ok"]
    app: str
    #: The app's release version (VERSION / CHANGELOG), e.g. ``0.1.0``.
    version: str
    #: The API contract version this install serves — the ``v1`` in
    #: ``/api/v1``. Additive and stable: an integrator reads it to decide
    #: which contract it is talking to (docs/architecture/api-versioning.md).
    api_version: str = "v1"


class DependencyStatus(BaseModel):
    name: str
    status: DependencyState
    latency_ms: float
    detail: str | None = None


class ReadinessReport(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    dependencies: list[DependencyStatus]
