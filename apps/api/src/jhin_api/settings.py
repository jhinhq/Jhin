"""API configuration loaded from environment variables (plan section 39)."""

from functools import lru_cache

from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings


class Settings(ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "Jhin"
    app_url: str = "http://localhost:3000"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

    # --- Auth (plan 20.1) ---
    session_cookie_name: str = "jhin_session"
    csrf_cookie_name: str = "jhin_csrf"
    csrf_header_name: str = "x-csrf-token"
    session_ttl_hours: int = 24 * 7
    # Set COOKIE_SECURE=true behind HTTPS. Defaults to false so the
    # quick-start stack works over plain http://localhost.
    cookie_secure: bool = False
    login_max_attempts: int = 10
    login_window_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
