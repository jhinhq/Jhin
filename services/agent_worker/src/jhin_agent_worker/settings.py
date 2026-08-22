from collections.abc import Mapping
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings
from jhin_tools import CrashBarrierName


class BarrierSettingsMixin(BaseModel):
    test_crash_barrier_dir: Path | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_DIR"
    )
    test_crash_barrier_name: CrashBarrierName | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_NAME"
    )
    test_crash_barrier_match: UUID | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_MATCH"
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

    @model_validator(mode="before")
    @classmethod
    def reject_noncanonical_production_barrier(cls, values: object) -> object:
        if not isinstance(values, Mapping):
            return values
        environment = values.get("app_env", values.get("APP_ENV"))
        barriers = (
            values.get("test_crash_barrier_dir", values.get("JHIN_TEST_CRASH_BARRIER_DIR")),
            values.get("test_crash_barrier_name", values.get("JHIN_TEST_CRASH_BARRIER_NAME")),
            values.get("test_crash_barrier_match", values.get("JHIN_TEST_CRASH_BARRIER_MATCH")),
        )
        configured = any(value is not None and value != "" for value in barriers)
        if (
            type(environment) is str
            and environment.lower() in {"prod", "production"}
            and configured
        ):
            raise ValueError("test crash barriers are forbidden in production")
        return values

    @model_validator(mode="after")
    def reject_production_barrier(self) -> Self:
        configured = any(
            (
                self.test_crash_barrier_dir,
                self.test_crash_barrier_name,
                self.test_crash_barrier_match,
            )
        )
        environment = getattr(self, "app_env", "")
        if (
            type(environment) is str
            and environment.lower() in {"prod", "production"}
            and configured
        ):
            raise ValueError("test crash barriers are forbidden in production")
        return self


class Settings(BarrierSettingsMixin, ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
