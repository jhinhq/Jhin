"""Response contracts for health endpoints."""

from typing import Literal

from pydantic import BaseModel

DependencyState = Literal["ok", "error"]


class LivenessReport(BaseModel):
    status: Literal["ok"]
    app: str
    version: str


class DependencyStatus(BaseModel):
    name: str
    status: DependencyState
    latency_ms: float
    detail: str | None = None


class ReadinessReport(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    dependencies: list[DependencyStatus]
