"""API configuration loaded from environment variables (plan section 39)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "Jhin"
    app_env: str = "dev"
    app_url: str = "http://localhost:3000"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"


@lru_cache
def get_settings() -> Settings:
    return Settings()
