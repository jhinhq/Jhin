from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    log_level: str = "INFO"
