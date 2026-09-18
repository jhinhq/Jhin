"""Configuration for the dedicated tool worker."""

from __future__ import annotations

from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings
from jhin_tool_worker.drain import DRAIN_BUDGET_SECONDS
from jhin_tools import CrashBarrierName


class ToolWorkerSettings(ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
    # How long a SIGTERM'd worker waits for its in-flight tool calls before it
    # lets Temporal cancel them. The first of the three budgets that share
    # Docker's stop grace; ``jhin_tool_worker.drain`` owns the arithmetic and
    # says what is left over for the steps that have no budget of their own.
    tool_worker_drain_timeout_seconds: float = DRAIN_BUDGET_SECONDS
    # How often the worker sweeps for ``sandbox_job`` rows whose runner is
    # demonstrably gone. Five minutes rather than an hour because the sweep
    # is one indexed query that usually returns nothing, and because the row
    # it exists to close is a job an agent is no longer being told about.
    tool_worker_sandbox_sweep_seconds: float = 300.0
    test_crash_barrier_dir: Path | None = Field(
        default=None,
        validation_alias="JHIN_TEST_CRASH_BARRIER_DIR",
    )
    test_crash_barrier_name: CrashBarrierName | None = Field(
        default=None,
        validation_alias="JHIN_TEST_CRASH_BARRIER_NAME",
    )
    test_crash_barrier_match: UUID | None = Field(
        default=None,
        validation_alias="JHIN_TEST_CRASH_BARRIER_MATCH",
    )

    @field_validator(
        "test_crash_barrier_dir",
        "test_crash_barrier_name",
        "test_crash_barrier_match",
        mode="before",
    )
    @classmethod
    def empty_barrier_value_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def reject_production_barrier(self) -> Self:
        configured = any(
            (
                self.test_crash_barrier_dir,
                self.test_crash_barrier_name,
                self.test_crash_barrier_match,
            )
        )
        if self.app_env.lower() in {"prod", "production"} and configured:
            raise ValueError("test crash barriers are forbidden in production")
        return self


__all__ = ["ToolWorkerSettings"]
