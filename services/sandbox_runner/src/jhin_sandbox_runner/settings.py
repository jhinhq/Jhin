"""Runner configuration (plan 14.3 caps, section 39 env names).

Everything here is infrastructure configuration; the runner never sees the
master key, database credentials, or long-lived user secrets — job env
values arrive per request over the internal ``runner`` network and die with
the job.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Shared bearer token required on every job endpoint (defense in depth on
    # top of Docker network isolation). Empty token = every request denied.
    sandbox_runner_token: str = ""
    sandbox_runner_port: int = 8085

    # Image used when a job request does not name one.
    sandbox_default_image: str = "jhin-sandbox:latest"
    # Docker network attached for network_policy=internet. Must be a dedicated
    # bridge network that carries NO control-plane services (plan 14.4).
    sandbox_network: str = "jhin_sandbox"

    # Hard caps (plan 14.3). Per-job requests may go lower, never higher.
    sandbox_max_cpus: float = 2.0
    sandbox_max_memory_mb: int = 4096
    sandbox_max_pids: int = 256
    sandbox_max_timeout_seconds: int = 1800

    # Per-stream cap on captured stdout/stderr (plan 21.8).
    sandbox_max_output_bytes: int = 65536
    # Startup reaping: workspace volumes older than this are removed.
    sandbox_workspace_max_age_hours: int = 24

    log_level: str = "INFO"
