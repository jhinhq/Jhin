"""Wire schemas of the internal runner API (plan 14.2).

Design decision (documented in docs/architecture/sandboxing.md): the caller
resolves credentials and sends short-lived plaintext values in ``secret_env``
over the internal runner network, instead of the runner resolving secret
refs itself. That keeps the master key out of this service entirely — the
component that holds the Docker socket holds no key material at rest.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NetworkPolicy = Literal["none", "internet"]

_JOB_ID_RE = re.compile(r"^[a-f0-9-]{8,64}$")
_WORKSPACE_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SandboxJobRequest(BaseModel):
    """One job = one fresh ephemeral container (plan 14.2)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    # Empty string = use the runner's default image.
    image: str = Field(default="", max_length=300)
    # Exec-form argv; the runner never invokes a host shell.
    command: list[str] = Field(min_length=1, max_length=64)
    # Optional persistent workspace volume key. Jobs sharing the same key
    # (e.g. all CLI calls of one agent run) see the same /workspace.
    workspace_key: str = ""
    working_dir: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    # Injected like env but registered for redaction: these values are
    # scrubbed from all captured output before it leaves the runner.
    secret_env: dict[str, str] = Field(default_factory=dict)
    network_policy: NetworkPolicy = "none"
    cpu_limit: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    pids_limit: int | None = Field(default=None, gt=0)
    timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("job_id")
    @classmethod
    def _job_id_shape(cls, value: str) -> str:
        if not _JOB_ID_RE.match(value):
            raise ValueError("job_id must be a lowercase hex/uuid-like token")
        return value

    @field_validator("workspace_key")
    @classmethod
    def _workspace_key_shape(cls, value: str) -> str:
        if value and not _WORKSPACE_KEY_RE.match(value):
            raise ValueError("workspace_key must be a short [a-zA-Z0-9_.-] token")
        return value

    @field_validator("env", "secret_env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
        return value

    @field_validator("working_dir")
    @classmethod
    def _absolute_dir(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("working_dir must be an absolute path")
        return value


class SandboxJobStatusResponse(BaseModel):
    """Job snapshot. ``stdout``/``stderr`` are redacted and size-capped."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: Literal["running", "completed", "failed", "timeout", "cancelled"]
    image: str
    network_policy: NetworkPolicy
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Infrastructure error detail (image missing, docker failure) — safe text.
    error: str | None = None


class SandboxLogsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
