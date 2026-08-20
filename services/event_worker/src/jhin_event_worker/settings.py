from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "dev"
    nats_url: str = "nats://localhost:4222"
    log_level: str = "INFO"
    consumer_durable_name: str = "event-worker"
    ingress_durable_name: str = "event-worker-ingress"
    # Trigger matching (plan 10.5): the matcher reads triggers from Postgres
    # and starts TriggeredTaskWorkflow through Temporal.
    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    trigger_cache_ttl_seconds: float = 5.0
