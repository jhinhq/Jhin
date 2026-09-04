"""Input/output models of the CLI tools (plan 11.6).

Inputs are strict (``extra="forbid"``) and every scope-relevant field is a
plain string so grant scope patterns (fnmatch, plan 12) apply directly:
``command`` is matched as one shell string, ``image``/``network``/
``repository``/``path``/``branch`` as-is. Outputs carry sanitized, size-capped
tails — the gateway sanitizes again before persistence.

Two fields the model used to supply are gone on purpose (docs/architecture/
sandboxing.md): ``RepositoryCheckoutInput.git_connection_id`` (the credential
is admin-set on the connection, never chosen per call) and
``TestRunInput.network`` (the command is arbitrary and runs in the checkout,
so the egress decision is Jhin's rather than the model's: ``cli.test.run``
always runs with ``network: "none"``. The tool is WRITE risk — unattended
under Autonomous and Balanced, approved under Restricted — and under none of
them does it choose its own egress).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Optional per-call network override; empty string = connection default.
NetworkChoice = Literal["", "none", "internet"]

_MAX_COMMAND_CHARS = 4_000
_MAX_CONTENT_CHARS = 48_000
_MAX_PATTERN_CHARS = 500
_MAX_COMMIT_MESSAGE_CHARS = 2_000
# Job wall-clock caps: the Temporal step activity allows 10 minutes end to
# end, so one job may consume at most 8 of them (model latency + polling
# overhead need the rest).
MAX_JOB_TIMEOUT_SECONDS = 480

# A working-tree path may never reach git's own state. ``.git`` in any
# position is refused (a nested submodule's ``.git`` file included), and the
# three dotfiles below are refused as the first segment because a job whose
# checkout has not run yet works in ``/workspace`` itself, where ``.gitconfig``
# would be git's per-user config. ``.github/``, ``.gitignore`` and
# ``.gitattributes`` stay writable: they are ordinary repository content, and
# the config-based attacks they would otherwise enable are blocked by
# GIT_CONFIG_NOSYSTEM + GIT_CONFIG_GLOBAL=/dev/null and by the push-time
# config audit.
_REFUSED_SEGMENTS = frozenset({".git"})
_REFUSED_FIRST_SEGMENTS = frozenset({".git", ".gitconfig", ".gitmodules"})
_JHIN_PREFIX = ".jhin"

# ``owner/name``, where neither half may be made of dots alone. To a plain
# ``[\w.-]+/[\w.-]+`` a repository of ``../evil`` is two perfectly ordinary
# segments; to everything that later joins the value onto a path — the clone
# URL Jhin builds, and the ``/repos/<repository>`` API paths the GitHub tools
# build — it is a directory traversal out of the prefix the credential's scope
# was written around. The middle character class is the whole rule: every
# segment must carry at least one character that is not a dot.
REPOSITORY_PATTERN = r"^[\w.-]*[\w-][\w.-]*/[\w.-]*[\w-][\w.-]*$"


def _validate_workspace_path(value: str) -> str:
    """File paths are always relative to the job workspace; never absolute,
    never escaping upward, and never git's own state (see §2.3 of the build
    contract). This is the schema half of the ``.git`` ban — the executors
    add an in-sandbox ``realpath`` guard that also catches symlinks."""
    if not value or value.startswith("/"):
        raise ValueError("path must be relative to the workspace")
    if "\\" in value:
        raise ValueError("path must use '/' separators")
    if any(character < " " or character == "\x7f" for character in value):
        raise ValueError("path must not contain control characters")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must not contain '.', '..' or empty segments")
    for part in parts:
        folded = part.casefold()
        if folded in _REFUSED_SEGMENTS or folded.startswith(_JHIN_PREFIX):
            raise ValueError("path must not touch git internals or Jhin's own workspace files")
    if parts[0].casefold() in _REFUSED_FIRST_SEGMENTS:
        raise ValueError("path must not touch git internals or Jhin's own workspace files")
    return value


def _validate_optional_workspace_path(value: str) -> str:
    """Same rules, but an empty value means "the whole working tree"."""
    if not value:
        return value
    return _validate_workspace_path(value)


class _JobOptions(BaseModel):
    """Fields shared by every job-submitting tool input."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    # Empty = the connection's default image (falling back to the runner's).
    image: str = Field(default="", max_length=300)
    timeout_seconds: int | None = Field(default=None, gt=0, le=MAX_JOB_TIMEOUT_SECONDS)


class _FileToolInput(BaseModel):
    """File tools never choose an image or a network: they are isolated jobs
    on the connection's default image."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    timeout_seconds: int | None = Field(default=None, gt=0, le=MAX_JOB_TIMEOUT_SECONDS)


class CommandExecuteInput(_JobOptions):
    command: str = Field(min_length=1, max_length=_MAX_COMMAND_CHARS)
    network: NetworkChoice = ""


class RepositoryCheckoutInput(_JobOptions):
    repository: str = Field(min_length=3, max_length=200, pattern=REPOSITORY_PATTERN)
    # Branch to create for the agent's work; empty = agent/<task-id>-<repo>.
    branch: str = Field(default="", max_length=200, pattern=r"^[\w./-]*$")
    # Existing ref to clone from; empty = the remote default branch.
    ref: str = Field(default="", max_length=200, pattern=r"^[\w./-]*$")


class RepositoryPushInput(_FileToolInput):
    """Commit the working tree and push one branch. The model supplies no
    remote, no refspec and no shell — Jhin writes the whole script."""

    repository: str = Field(min_length=3, max_length=200, pattern=REPOSITORY_PATTERN)
    # A leading '-' is refused as well as a shell metacharacter: the name
    # is Jhin's to put in a refspec, and nothing there may look like a flag.
    branch: str = Field(min_length=1, max_length=200, pattern=r"^[\w][\w./-]*$")
    commit_message: str = Field(min_length=1, max_length=_MAX_COMMIT_MESSAGE_CHARS)


class TestRunInput(_JobOptions):
    __test__ = False  # not a pytest class, despite the name

    command: str = Field(default="bash ./run_tests.sh", max_length=_MAX_COMMAND_CHARS)


class FileReadInput(_FileToolInput):
    path: str = Field(min_length=1, max_length=500)
    # 1-based inclusive line window.
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=400, ge=1, le=2_000)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_workspace_path(value)


class FileWriteInput(_FileToolInput):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=_MAX_CONTENT_CHARS)
    # The sha256 cli.file.read reported for the whole file. Empty is accepted
    # only when the file does not exist yet. A paged read returns no token at
    # all, so "read a truncated page, write back what you read" cannot even be
    # attempted: there is nothing to pass here. Editing part of a large file is
    # what cli.file.edit is for.
    read_token: str = Field(pattern=r"^$|^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_workspace_path(value)


class FileEditInput(_FileToolInput):
    path: str = Field(min_length=1, max_length=500)
    old_string: str = Field(min_length=1, max_length=_MAX_CONTENT_CHARS)
    new_string: str = Field(max_length=_MAX_CONTENT_CHARS)
    expected_count: int = Field(default=1, ge=1, le=1_000)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_workspace_path(value)


class FileListInput(_FileToolInput):
    # Empty = the whole working tree.
    path: str = Field(default="", max_length=500)
    glob: str = Field(default="", max_length=200)
    max_entries: int = Field(default=200, ge=1, le=500)
    max_depth: int = Field(default=2, ge=1, le=10)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_optional_workspace_path(value)

    @field_validator("glob")
    @classmethod
    def _glob_shape(cls, value: str) -> str:
        if any(character in value for character in ("/", "\\", "\n", "\x00")):
            raise ValueError("glob matches one path segment and cannot contain '/'")
        return value


class FileSearchInput(_FileToolInput):
    pattern: str = Field(min_length=1, max_length=_MAX_PATTERN_CHARS)
    # Empty = the whole working tree.
    path: str = Field(default="", max_length=500)
    glob: str = Field(default="", max_length=200)
    regex: bool = False
    max_matches: int = Field(default=100, ge=1, le=200)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        return _validate_optional_workspace_path(value)

    @field_validator("glob")
    @classmethod
    def _glob_shape(cls, value: str) -> str:
        if any(character in value for character in ("/", "\\", "\n", "\x00")):
            raise ValueError("glob matches one path segment and cannot contain '/'")
        return value

    @field_validator("pattern")
    @classmethod
    def _pattern_shape(cls, value: str) -> str:
        if "\n" in value or "\x00" in value:
            raise ValueError("pattern must be a single line")
        return value


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
    # The ref the working branch was cut from — cli.repository.push refuses to
    # push back onto it.
    base_ref: str = ""
    # One directory level, so the agent can start navigating immediately.
    top_level: list[str] = []


class RepositoryPushOutput(SandboxJobOutput):
    repository: str
    branch: str
    remote: str
    previous_sha: str
    pushed_sha: str


class FileReadOutput(BaseModel):
    sandbox_job_id: str
    path: str
    content: str
    truncated: bool
    first_line: int = 1
    last_line: int = 0
    total_lines: int = 0
    has_more: bool = False
    # sha256 of the whole file, computed in the sandbox. Returned ONLY when this
    # page was the whole file (started at line one, not truncated, nothing more
    # to come); a partial page returns "" so it can never be spent on a
    # cli.file.write that would drop the lines this read never saw.
    read_token: str = ""


class FileWriteOutput(BaseModel):
    sandbox_job_id: str
    path: str
    bytes_written: int
    read_token: str = ""


class FileEditOutput(BaseModel):
    sandbox_job_id: str
    path: str
    replacements: int
    read_token: str = ""


class FileEntry(BaseModel):
    path: str
    kind: str
    size_bytes: int


class FileListOutput(BaseModel):
    sandbox_job_id: str
    path: str
    entries: list[FileEntry]
    truncated: bool


class FileMatch(BaseModel):
    path: str
    line: int
    text: str


class FileSearchOutput(BaseModel):
    sandbox_job_id: str
    pattern: str
    matches: list[FileMatch]
    truncated: bool
