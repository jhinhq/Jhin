from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jhin_tools import CrashBarrierName


class BarrierSettingsMixin(BaseModel):
    app_env: str = Field(default="dev", validation_alias="APP_ENV")
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


class Settings(BarrierSettingsMixin, BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
    log_level: str = "INFO"
