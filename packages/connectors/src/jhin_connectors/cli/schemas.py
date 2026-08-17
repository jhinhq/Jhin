"""Input/output models of the five CLI tools (plan 11.6).

Inputs are strict (``extra="forbid"``) and every scope-relevant field is a
plain string so grant scope patterns (fnmatch, plan 12) apply directly:
``command`` is matched as one shell string, ``image``/``network``/
``repository``/``path`` as-is. Outputs carry sanitized, size-capped tails —
the gateway sanitizes again before persistence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Optional per-call network override; empty string = connection default.
NetworkChoice = Literal["", "none", "internet"]

_MAX_COMMAND_CHARS = 4_000
_MAX_CONTENT_CHARS = 48_000
# Job wall-clock caps: the Temporal step activity allows 10 minutes end to
# end, so one job may consume at most 8 of them (model latency + polling
# overhead need the rest).
MAX_JOB_TIMEOUT_SECONDS = 480


def _validate_workspace_path(value: str) -> str:
    """File paths are always relative to the job workspace; never absolute,
    never escaping upward."""
    if not value or value.startswith("/"):
        raise ValueError("path must be relative to the workspace")
    parts = value.split("/")
    if any(part in ("", "..") for part in parts):
        raise ValueError("path must not contain '..' or empty segments")
    return value


class _JobOptions(BaseModel):
    """Fields shared by every job-submitting tool input."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    # Empty = the connection's default image (falling back to the runner's).
    image: str = Field(default="", max_length=300)
    timeout_seconds: int | None = Field(default=None, gt=0, le=MAX_JOB_TIMEOUT_SECONDS)


class CommandExecuteInput(_JobOptions):
    command: str = Field(min_length=1, max_length=_MAX_COMMAND_CHARS)
    network: NetworkChoice = ""


class RepositoryCheckoutInput(_JobOptions):
    repository: str = Field(min_length=3, max_length=200, pattern=r"^[\w.-]+/[\w.-]+$")
    # Branch to create for the agent's work; empty = agent/<task-id>-<repo>.
    branch: str = Field(default="", max_length=200, pattern=r"^[\w./-]*$")
    # Existing ref to clone from; empty = the remote default branch.
    ref: str = Field(default="", max_length=200, pattern=r"^[\w./-]*$")
    # Empty = the CLI connection's configured git_connection_id.
    git_connection_id: str = ""


class TestRunInput(_JobOptions):
    __test__ = False  # not a pytest class, despite the name

    command: str = Field(default="bash ./run_tests.sh", max_length=_MAX_COMMAND_CHARS)
    network: NetworkChoice = ""


class FileReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    path: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_workspace_path(value)


class FileWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=_MAX_CONTENT_CHARS)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_workspace_path(value)


class SandboxJobOutput(BaseModel):
    """Common result shape for command-style tools."""

    sandbox_job_id: str
    status: str
    exit_code: int | None
    duration_ms: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class CommandExecuteOutput(SandboxJobOutput):
    command: str


class TestRunOutput(SandboxJobOutput):
    __test__ = False  # not a pytest class, despite the name

    command: str
    passed: bool


class RepositoryCheckoutOutput(SandboxJobOutput):
    repository: str
    branch: str
    head_sha: str
    path: str


class FileReadOutput(BaseModel):
    sandbox_job_id: str
    path: str
    content: str
    truncated: bool


class FileWriteOutput(BaseModel):
    sandbox_job_id: str
    path: str
    bytes_written: int
