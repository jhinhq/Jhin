"""Runner configuration (plan 14.3 caps, section 39 env names).

Everything here is infrastructure configuration; the runner never sees the
master key, database credentials, or long-lived user secrets — job env
values arrive per request over the internal ``runner`` network and die with
the job.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jhin_sandbox_runner.docker_socket import (
    ROOTLESS_TRANSPORT_URL,
    DockerSocketMode,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "dev"

    # Shared bearer token required on every job endpoint (defense in depth on
    # top of Docker network isolation). Empty token = every request denied.
    sandbox_runner_token: str = ""
    sandbox_runner_port: int = 8085

    # Required Docker authority. There is deliberately no mode default: an
    # omitted deployment decision must fail configuration validation.
    sandbox_docker_mode: DockerSocketMode
    sandbox_docker_socket: Path | None = None
    sandbox_docker_transport_url: str | None = None
    sandbox_docker_gid: int | None = Field(default=None, gt=0)

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

    @model_validator(mode="after")
    def validate_docker_mode(self) -> Settings:
        if self.sandbox_docker_mode == "rootless":
            if self.sandbox_docker_socket is not None or self.sandbox_docker_gid is not None:
                raise ValueError("rootless mode accepts no socket path or socket GID")
            if self.sandbox_docker_transport_url != ROOTLESS_TRANSPORT_URL:
                raise ValueError("rootless mode requires the exact private transport URL")
            return self

        if self.sandbox_docker_socket is None:
            raise ValueError("rootful mode requires a Docker socket path")
        if not self.sandbox_docker_socket.is_absolute():
            raise ValueError("rootful Docker socket path must be absolute")
        if self.sandbox_docker_gid is None:
            raise ValueError("rootful mode requires a positive socket GID")
        if self.sandbox_docker_transport_url is not None:
            raise ValueError("rootful mode accepts no transport URL")
        return self
