from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    nats_url: str = "nats://localhost:4222"
    log_level: str = "INFO"
    consumer_durable_name: str = "event-worker"
