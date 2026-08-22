from typing import Literal

from jhin_observability import ObservabilitySettings


class Settings(ObservabilitySettings):
    nats_url: str = "nats://localhost:4222"
    consumer_durable_name: Literal["event-worker"] = "event-worker"
    ingress_durable_name: Literal["event-worker-ingress"] = "event-worker-ingress"
    # Trigger matching (plan 10.5): the matcher reads triggers from Postgres
    # and starts TriggeredTaskWorkflow through Temporal.
    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    trigger_cache_ttl_seconds: float = 5.0
