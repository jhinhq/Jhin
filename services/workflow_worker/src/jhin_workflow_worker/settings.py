from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings


class Settings(ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
