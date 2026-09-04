"""CLI tool executors (plan 11.6, 14.5): every tool is one sandbox job.

Execution path: the gateway has already authorized the call (capability +
scope: connection, command pattern, image, network, repository, path, branch,
plus the CLI connector's repository allow-list validator). Here the job is
submitted to the sandbox runner over the internal API, polled to completion,
and recorded:

- one ``sandbox_job`` row per job, linked to run/task/tool_call, written in
  the same transaction as the tool_call row;
- append-only audit events ``sandbox.job.started`` / ``completed`` /
  ``failed``, plus ``sandbox.repo_config_tampered`` when a push finds the
  repository's own git config rewritten;
- stdout/stderr redacted (runner-side against the job's secret env, worker-
  side against the process redactor) and size-capped before persistence.

Workspace persistence (documented in docs/architecture/sandboxing.md): all
jobs of one agent run share the named volume ``run-<run_id>`` mounted at
``/workspace``, so a checkout survives across tool calls; the volume is
destroyed when the run finalizes. Repository checkouts land in
``/workspace/repo`` and command-style jobs start there when it exists.

Git credentials (plan 13.6, 14.5). Two rules, and everything else follows:

1. **Only Jhin-authored scripts run in a job that holds ``GIT_TOKEN``.**
   ``cli.repository.checkout`` and ``cli.repository.push`` are the only tools
   that resolve a credential; ``cli.command.execute`` never receives one, so
   no model-authored shell string can reach the secret.
2. **The credential is bound to the cloned remote.** It is delivered as
   ``git -c credential.helper= -c credential."<git base>".helper=<inline>``
   on Jhin's own command line, so git's URL matcher — not a script of ours —
   decides whether the helper runs, and a push to any other host falls through
   to ``GIT_ASKPASS=/bin/false`` and ``GIT_TERMINAL_PROMPT=0``, both hard
   errors. The helper never lands in a file the agent can rewrite.

The file tools refuse git's own state three times over: the schema rejects a
``.git`` segment (see ``cli/schemas.py``); every file job re-resolves the path
with ``realpath`` inside the sandbox, so a symlink cannot smuggle a write into
``.git``; and the same guard refuses a file with more than one name, because a
hard link gives ``.git/config`` a second name that ``realpath`` has nothing to
resolve and the schema never sees.

**The push trusts nothing inside the container** (plan 14.5). Everything a
sandbox job could have rewritten is either bypassed or compared against a
record only Jhin holds:

- the push goes to the URL Jhin computes, ``git push <url> <refspec>``, never
  to the *name* ``origin`` — so rewriting the remote redirects nothing;
- ``git config --local`` is audited by key name, then by value (``--get-all``
  proves ``remote.origin.url`` carries exactly the one URL Jhin cloned — a
  name-only audit passes a key that has been given a second value), then
  byte-for-byte against the sha256 the checkout recorded;
- the base branch a push may not land on comes from the ``base_ref`` the
  checkout wrote into Jhin's own audit trail, not from ``refs/remotes/origin/
  HEAD`` inside the repository the agent has been working in.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from jhin_connectors.cli.runner_client import SandboxRunnerError, run_sandbox_job
from jhin_connectors.cli.schemas import (
    CommandExecuteInput,
    CommandExecuteOutput,
    FileEditInput,
    FileEditOutput,
    FileEntry,
    FileListInput,
    FileListOutput,
    FileMatch,
    FileReadInput,
    FileReadOutput,
    FileSearchInput,
    FileSearchOutput,
    FileWriteInput,
    FileWriteOutput,
    RepositoryCheckoutInput,
    RepositoryCheckoutOutput,
    RepositoryPushInput,
    RepositoryPushOutput,
    TestRunInput,
    TestRunOutput,
)
from jhin_connectors.cli.validators import is_plain_repository
from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_connectors.github.auth import resolve_access_token
from jhin_connectors.github.client import DEFAULT_BASE_URL, validate_github_base_url
from jhin_db.models import AuditEvent, Connection, SandboxJob
from jhin_domain import ActorType, ConnectionStatus, SandboxJobStatus, new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_secrets.redaction import redact_text
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.errors import ToolExecutionError

# Persisted/observed output tails; the runner caps raw capture much higher.
_MAX_TAIL_CHARS = 8_000
# One file page returned to the model. Kept under jhin_tools.sanitize's
# MAX_STRING_CHARS (8_192) so a page is never silently clipped by the gateway.
_MAX_FILE_CHARS = 6_000
# Everything the sandbox is allowed to emit for one page, so the runner's own
# tail-keeping cap never eats the beginning of the page.
_READ_PAGE_BYTES = 20_000
# Budget for list/search results so the whole tool output stays under
# jhin_tools.sanitize's MAX_DOCUMENT_BYTES (32_768).
_MAX_RESULT_BYTES = 20_000
_MAX_MATCH_CHARS = 300
# Bytes of listing/match data one job may encode into its trailer. Base64
# costs a third on top, and the runner caps a stream at 65_536 keeping the
# *tail* — so a word wider than this could be cut at its front, which decodes
# to nothing. Bounded here so the cap is Jhin's rather than the runner's.
_MAX_ENCODED_BYTES = 24_000

_DEFAULT_COMMAND_TIMEOUT = 300
_DEFAULT_FILE_TIMEOUT = 60

_WORKSPACE_PATH = "/workspace"
_REPO_PATH = "/workspace/repo"

# Machine-readable trailer. It goes *after* the payload because the sandbox
# runner keeps the tail of oversized output, so a trailer always survives
# while a header would not — which means the parser, not the position, has to
# be what makes it trustworthy. Four rules do that, and all four are needed:
#
# 1. the sentinel carries a nonce Jhin draws per job. Nothing in the container
#    can predict it, so no byte a repository (or a model) chose can write one;
# 2. it must appear exactly once. A stream carrying two is ambiguous, so it is
#    discarded rather than resolved by "the last one wins" — a rule that hands
#    the decision to whoever printed last;
# 3. nothing derived from repository content is printed inside the region.
#    Values that describe content — the checkout's top-level listing, the file
#    tools' listings and matches — travel as base64, so a filename cannot
#    contribute a newline, a key, a field separator, or a sentinel;
# 4. exactly one thing emits it: :attr:`_Trailer.echo`. A second emitter is
#    how a sentinel and its parser drift apart, and the drift is silent — the
#    trailer simply stops being found, and every value read from it comes back
#    empty. ``cli.file.edit`` shipped that way: its Python program wrote the
#    old bare marker while the tool parsed the nonce form, so the read_token
#    it documents was always ''. The program now writes only key=value lines
#    and the shell prints the sentinel ahead of them like every other tool.
_META_KEY = "JHIN_META"
_META_NONCE_BYTES = 16


@dataclass(frozen=True)
class _Trailer:
    """One job's trailer: the shell line that emits it, and its parser."""

    nonce: str

    @property
    def sentinel(self) -> str:
        return f"\n{_META_KEY}:{self.nonce}\n"

    @property
    def echo(self) -> str:
        """The emitting shell line — the *only* thing in Jhin that writes a
        sentinel. Written out rather than interpolated so the backslash
        escapes are printf's, not Python's."""
        return f"printf '\\n{_META_KEY}:{self.nonce}\\n'\n"

    def split(self, stdout: str) -> tuple[str, list[tuple[str, str]]]:
        """(payload, trailer entries). A stream with no sentinel — or with
        more than one — carries no trailer at all: every caller that needs a
        value from it then fails closed rather than reading a forged one."""
        if stdout.count(self.sentinel) != 1:
            return stdout, []
        index = stdout.index(self.sentinel)
        entries: list[tuple[str, str]] = []
        for line in stdout[index + len(self.sentinel) :].splitlines():
            key, separator, value = line.partition("=")
            if separator:
                entries.append((key.strip(), value.strip()))
        return stdout[:index], entries


def _new_trailer() -> _Trailer:
    return _Trailer(nonce=secrets.token_hex(_META_NONCE_BYTES))


# The credential answer, inline on Jhin's own git command line. It carries no
# secret: ``$GIT_TOKEN`` is expanded by the helper's shell from the job-scoped
# secret env, which exists only for the lifetime of one container.
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; }; f'
)

# Repo-local config keys a Jhin checkout legitimately produces. Anything else
# in ``git config --local`` before a push is tampering, not configuration.
_ALLOWED_REPO_CONFIG = (
    r"^(user\.(name|email)"
    r"|core\.(repositoryformatversion|filemode|bare|logallrefupdates|symlinks"
    r"|ignorecase|precomposeunicode)"
    r"|remote\.origin\.(url|fetch)"
    r"|branch\..*\.(remote|merge))$"
)

# The audit trail entry the checkout writes and the push reads back: Jhin's
# own account of what was cloned, in a table no sandbox job can reach. Keyed
# on the run so the lookup needs no JSON predicate.
_CHECKOUT_RECORD_ACTION = "sandbox.checkout.recorded"
_CHECKOUT_RECORD_TARGET = "agent_run"
# Shape of a ref name Jhin will interpolate into a script, applied to the
# recorded base even though Jhin wrote the record: the value originated as a
# line of container stdout, so it is re-checked rather than trusted twice.
_REF_NAME = re.compile(r"^[\w./-]{1,200}$")
# The other two recorded values, checked the same way and for the same reason.
# A commit id is sha1 or sha256 depending on the repository's object format.
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_CONFIG_SHA = re.compile(r"^[0-9a-f]{64}$")

# Refuses a write that resolves outside the working tree or into git's own
# state — symlinks, and files carrying a second name, included. Defence in
# depth with the schema validator.
#
# The link count is the half ``realpath`` cannot do. ``ln .git/config cfg``
# creates no symlink and no new path segment: the schema sees ``cfg``, the
# resolver sees ``<root>/cfg``, and both are correct — the file simply has two
# names and one of them is git's. A regular file Jhin's tools may touch has
# exactly one.
_GUARD_PROLOGUE = r"""jhin_root=$(pwd -P)
jhin_guard() {
  jhin_full=$(realpath -m -- "$1") || { printf 'JHIN_ERR=path_unresolvable\n' >&2; exit 66; }
  case "$jhin_full" in
    "$jhin_root"|"$jhin_root"/*) : ;;
    *) printf 'JHIN_ERR=path_escapes_workspace\n' >&2; exit 66 ;;
  esac
  case "$jhin_full/" in
    */.git/*) printf 'JHIN_ERR=git_internals_refused\n' >&2; exit 66 ;;
  esac
  if [ -f "$jhin_full" ] && [ "$(stat -c %h -- "$jhin_full")" != "1" ]; then
    printf 'JHIN_ERR=hard_linked_file\n' >&2; exit 66
  fi
}
"""

# The exit codes Jhin's own scripts use when they refuse. Nothing else in a
# job exits with these: git and the tools it runs use their own, so the range
# is what separates "Jhin decided not to" from "something failed".
_REFUSAL_EXIT_CODES = frozenset({65, 66, 67, 68, 69})

# Sandbox exit codes carrying a Jhin-authored refusal, mapped by the JHIN_ERR
# line on stderr. Every one of these happens *before* anything leaves the
# sandbox, so the tool failure is proven side-effect free.
_REFUSAL_HINTS: dict[str, str] = {
    "path_unresolvable": "That path could not be resolved inside the workspace.",
    "path_escapes_workspace": "Paths must stay inside the checkout.",
    "git_internals_refused": (
        "Files under .git are not writable or readable through the file tools. "
        "Use cli.repository.push to land a branch."
    ),
    "hard_linked_file": (
        "That file has more than one name on disk, so changing it would change "
        "a file you did not name. The file tools only touch files with a single "
        "name; remove the extra link, or work through cli.file.edit on the real "
        "path."
    ),
    "not_a_file": "That path is not a regular file.",
    "file_not_found": "That file does not exist yet.",
    "file_not_text": "That file is not UTF-8 text; the edit tool only edits text.",
    "file_changed": (
        "The file changed since you read it. Read it again and retry with the "
        "read_token from that read."
    ),
    "file_exists_pass_read_token": (
        "That file already exists. Read it first and pass the read_token it "
        "returns; an empty read_token creates a new file only."
    ),
    "file_missing_for_read_token": (
        "That file does not exist, so there is no read_token to match. Pass an "
        "empty read_token to create it."
    ),
    "edit_count_mismatch": (
        "old_string did not occur expected_count times. Read the file and retry "
        "with the count the failure reports."
    ),
    "no_checkout": "Run cli.repository.checkout first.",
    "checkout_unrecordable": (
        "The checkout ran, but Jhin could not read back its own account of what "
        "it produced, so nothing was recorded and a push would have had nothing "
        "to compare against. Check the repository out again."
    ),
    "no_checkout_record": (
        "Jhin has no record of checking this repository out during this run, so "
        "there is no trusted account of what the sandbox holds and nothing is "
        "pushed. Run cli.repository.checkout for this repository first."
    ),
    "branch_not_checked_out": (
        "That branch is not the one checked out in the sandbox. Push the branch "
        "cli.repository.checkout created."
    ),
    "push_to_base_refused": (
        "Pushing onto the base branch is refused. Push the agent branch and open a pull request."
    ),
    "repo_config_tampered": (
        "The repository's local git config carries entries Jhin did not write, "
        "so the push was refused. Check the repository out again."
    ),
    "remote_rewritten": (
        "The origin remote no longer names exactly the repository Jhin cloned, "
        "so the push was refused. Check the repository out again."
    ),
}


class CliToolError(Exception):
    """Tool-level failure with a message safe for models and persistence."""


def _refusal(code: str, *, detail: str = "") -> ToolExecutionError:
    """A named, proven-side-effect-free refusal the model can act on."""
    message = f"{code}{f': {detail}' if detail else ''}"
    return ToolExecutionError(
        message,
        code=code,
        side_effect_possible=False,
        hint=_REFUSAL_HINTS.get(code, ""),
    )


async def _load_cli_connection(ctx: ToolExecutionContext, connection_id: str) -> Connection:
    """The CLI connection carries no credential, so it is loaded without
    decryption — but with the same workspace isolation and status checks as
    :func:`resolve_connection` (plan 48.4)."""
    try:
        target = UUID(connection_id)
    except ValueError:
        raise ConnectionResolutionError("connection_id is not a valid UUID") from None
    connection = await ctx.session.scalar(
        select(Connection).where(
            Connection.id == target,
            Connection.workspace_id == ctx.workspace_id,
            Connection.connector_type == "cli",
        )
    )
    if connection is None:
        raise ConnectionResolutionError(f"no cli connection {target} in this workspace")
    if connection.status == ConnectionStatus.DISABLED.value:
        raise ConnectionResolutionError(f"connection '{connection.name}' is disabled")
    return connection


def _connection_defaults(connection: Connection) -> tuple[str, str, str]:
    """(default_image, default_network, git_connection_id) from config."""
    config = connection.config_json
    image = str(config.get("default_image") or "")
    network = str(config.get("default_network") or "none")
    git_connection_id = str(config.get("git_connection_id") or "")
    if network not in ("none", "internet"):
        network = "none"
    return image, network, git_connection_id


def allowed_repositories(connection: Connection) -> tuple[str, ...]:
    """The connection's repository allow-list. Absent or empty means this
    connection may not do repository work at all (deny by default)."""
    raw = connection.config_json.get("allowed_repositories")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str) and item)


async def _git_credentials(ctx: ToolExecutionContext, git_connection_id: str) -> tuple[str, str]:
    """(git_base_url, short-lived token) from the referenced GitHub
    connection — the plan-13.6 sandbox credential path. The connection is
    admin-set on the CLI connection; no tool input can choose it."""
    if not git_connection_id:
        raise CliToolError(
            "no GitHub connection configured: set git_connection_id on the CLI connection"
        )
    resolved = await resolve_connection(ctx, git_connection_id, connector_type="github")
    api_base = validate_github_base_url(str(resolved.config.get("base_url") or DEFAULT_BASE_URL))
    token = await resolve_access_token(
        resolved.connection.auth_type, resolved.credentials, api_base
    )
    # Real GitHub serves git on github.com; test/self-hosted layouts serve
    # git smart-HTTP under /git on the same server as the REST API.
    git_base = "https://github.com" if api_base == DEFAULT_BASE_URL else f"{api_base}/git"
    return git_base, token


def _git_env() -> dict[str, str]:
    """The environment every credentialed git job runs in. Each entry closes
    a way the credential could be answered by something other than Jhin's own
    inline helper."""
    return {
        "HOME": _WORKSPACE_PATH,
        # No askpass program: a prompt is a hard error, never an echo.
        "GIT_ASKPASS": "/bin/false",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        # Blocks a hostile /workspace/.gitconfig from contributing a helper.
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def _credential_args(git_base: str) -> str:
    """``-c`` arguments binding the credential to one remote, for Jhin's own
    git command line only. The empty ``credential.helper=`` resets the
    inherited helper list so nothing planted elsewhere can answer first."""
    helper = f"credential.{git_base}.helper={_CREDENTIAL_HELPER}"
    return f"-c credential.helper= -c {shlex.quote(helper)} -c core.hooksPath=/nonexistent"


def _remote_host(git_base: str) -> str:
    without_scheme = git_base.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def _workspace_key(ctx: ToolExecutionContext) -> str:
    return f"run-{ctx.run_id}"


def _tail(value: str) -> str:
    """Worker-side redaction pass, NUL strip, and size cap before anything
    persists.

    The NUL strip is not cosmetic: these tails land in Postgres ``text``
    columns, which reject U+0000 outright, so a repository file or a command
    that emits one would fail the whole tool call at commit rather than
    returning. The byte is repository-chosen, so it must not decide that.
    Only the persisted tail is touched; a file page keeps its exact bytes,
    because the sha the sandbox computed is what makes a write safe.
    """
    redacted = redact_text(value).replace("\x00", "?")
    if len(redacted) > _MAX_TAIL_CHARS:
        return redacted[-_MAX_TAIL_CHARS:]
    return redacted


def _raw_stdout(result: Mapping[str, Any]) -> str:
    """Full runner-captured stdout, redacted again worker-side. Used where a
    tool needs more than the persisted tail (file pages, listings)."""
    return redact_text(str(result.get("stdout", "")))


def _meta_one(entries: list[tuple[str, str]], key: str) -> str:
    for name, value in entries:
        if name == key:
            return value
    return ""


def _meta_int(entries: list[tuple[str, str]], key: str, *, default: int) -> int:
    """A counted value from the trailer, or ``default``.

    The nonce sentinel is what stops a repository writing one of these; this is
    the shape check that keeps a value which is not a number from leaving the
    executor as a ``ValueError``. Every counted trailer value goes through it,
    so "trusted, and checked anyway" is one rule rather than three spellings of
    one."""
    try:
        return int(_meta_one(entries, key))
    except ValueError:
        return default


def _read_token(entries: list[tuple[str, str]]) -> str:
    """The sha256 a file job reports for the file it just handled, checked for
    its shape before it becomes a token ``cli.file.write`` will accept.

    The nonce sentinel is what makes it unforgeable; this is the second look
    the recorded checkout values already get, for the same reason — the value
    arrived as a line of container stdout, so nothing about it is assumed."""
    token = _meta_one(entries, "sha")
    return token if _CONFIG_SHA.match(token) else ""


def _decoded(entries: list[tuple[str, str]], key: str) -> bytes:
    """One trailer word, base64-decoded. Empty when the word is absent or does
    not decode — which is what a truncated or missing trailer looks like, and
    is read as "nothing was listed" rather than guessed at."""
    try:
        return base64.b64decode(_meta_one(entries, key), validate=True)
    except (binascii.Error, ValueError):
        return b""


def _displayable(value: str) -> str:
    """Repository-chosen text on its way to a model: shown, never trusted.

    Every character Python does not consider printable becomes ``?``. That is
    a wider net than "below U+0020" on purpose: ``str.splitlines`` also breaks
    on U+000B, U+000C, U+001C-U+001E, U+0085 and U+2028/U+2029, so a name
    carrying one of those used to arrive here as a value that *looks* like one
    line to this function and like two to everything downstream.
    """
    return "".join(character if character.isprintable() else "?" for character in value)


def _top_level(entries: list[tuple[str, str]]) -> list[str]:
    """The checkout's one-level listing, decoded from the trailer's single
    base64 word: NUL-separated ``<type>:<name>`` records.

    Names are repository content, so they are shown but never trusted: a
    character that is not printable is displayed as ``?`` (the file tools'
    schema refuses such a path anyway), and an entry that does not decode is
    dropped rather than guessed at."""
    listing: list[str] = []
    for record in _decoded(entries, "top").decode("utf-8", "replace").split("\0"):
        kind, separator, name = record.partition(":")
        if not separator or not name:
            continue
        display = _displayable(name)
        listing.append(f"{display}/" if kind == "d" else display)
    return listing


def _refusal_code(row: SandboxJob) -> str:
    """The JHIN_ERR line a Jhin-authored script writes when it refuses.

    Read only when the exit code is one Jhin's own scripts reserve for a
    refusal. ``git`` writes to the same stream and repository content reaches
    it — a file name appears verbatim in plenty of git errors — and every
    refusal these codes name is claimed to be *proven side-effect free*. A
    push that failed after touching the remote exits with git's own code, so
    it can never be reported as one of these by a line somebody else printed.
    """
    if row.exit_code not in _REFUSAL_EXIT_CODES:
        return ""
    for line in reversed((row.stderr_tail or "").splitlines()):
        if line.startswith("JHIN_ERR="):
            return line.removeprefix("JHIN_ERR=").strip()
    return ""


def _raise_for_failure(row: SandboxJob, *, what: str) -> None:
    """Turn a non-zero Jhin-authored job into either a named refusal or an
    ordinary tool error."""
    if row.status == SandboxJobStatus.COMPLETED.value and row.exit_code == 0:
        return
    code = _refusal_code(row)
    if code in _REFUSAL_HINTS:
        raise _refusal(code)
    raise CliToolError(
        f"{what} failed ({row.status}, exit {row.exit_code}): {(row.stderr_tail or '')[:300]}"
    )


async def _run_job(
    ctx: ToolExecutionContext,
    *,
    command_display: str,
    argv: list[str],
    image: str,
    network: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    secret_env: dict[str, str] | None = None,
    audit_metadata: Mapping[str, Any] | None = None,
    completion_metadata: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
) -> tuple[SandboxJob, dict[str, Any]]:
    """Submit one sandbox job, poll it to a terminal state, and persist the
    ``sandbox_job`` row + audit trail (plan 14, 23)."""
    row = SandboxJob(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        tool_call_id=ctx.tool_call_id,
        status=SandboxJobStatus.RUNNING.value,
        image=image or "(runner default)",
        command=redact_text(command_display)[:2_000],
        network_policy=network,
        timeout_seconds=timeout_seconds,
        started_at=datetime.now(UTC),
    )
    ctx.session.add(row)
    shared = dict(audit_metadata or {})

    def audit(action: str, metadata: dict[str, Any]) -> None:
        ctx.session.add(
            AuditEvent(
                workspace_id=ctx.workspace_id,
                actor_type=ActorType.AGENT.value,
                actor_id=ctx.agent_id,
                action=action,
                target_type="sandbox_job",
                target_id=row.id,
                metadata_json={
                    "run_id": str(ctx.run_id),
                    "tool_call_id": str(ctx.tool_call_id) if ctx.tool_call_id else None,
                    "image": row.image,
                    "network_policy": network,
                    **shared,
                    **metadata,
                },
            )
        )

    audit("sandbox.job.started", {"timeout_seconds": timeout_seconds})
    await ctx.session.flush()

    payload: dict[str, Any] = {
        "job_id": str(row.id),
        "image": image,
        "command": argv,
        "workspace_key": _workspace_key(ctx),
        "working_dir": _WORKSPACE_PATH,
        "env": env or {},
        "secret_env": secret_env or {},
        "network_policy": network,
        "timeout_seconds": timeout_seconds,
    }
    try:
        result = await run_sandbox_job(payload, job_timeout_seconds=timeout_seconds)
    except SandboxRunnerError as exc:
        row.status = SandboxJobStatus.FAILED.value
        row.completed_at = datetime.now(UTC)
        row.error_code = "runner_error"
        row.stderr_tail = _tail(str(exc))
        audit("sandbox.job.failed", {"error": str(exc)[:300]})
        raise CliToolError(f"sandbox job failed: {exc}") from exc

    status = str(result.get("status", SandboxJobStatus.FAILED.value))
    row.status = status
    row.exit_code = cast("int | None", result.get("exit_code"))
    row.duration_ms = cast("int | None", result.get("duration_ms"))
    row.completed_at = datetime.now(UTC)
    row.stdout_tail = _tail(str(result.get("stdout", "")))
    row.stderr_tail = _tail(str(result.get("stderr", "")))
    if status != SandboxJobStatus.COMPLETED.value:
        row.error_code = status
        audit(
            "sandbox.job.failed",
            {"status": status, "error": str(result.get("error") or "")[:300]},
        )
    else:
        completion = dict(completion_metadata(result)) if completion_metadata else {}
        audit(
            "sandbox.job.completed",
            {"exit_code": row.exit_code, "duration_ms": row.duration_ms, **completion},
        )
    return row, result


def _security_audit(ctx: ToolExecutionContext, row: SandboxJob, metadata: dict[str, Any]) -> None:
    """A refusal that is a security event in its own right, not a tool error."""
    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="sandbox.repo_config_tampered",
            target_type="sandbox_job",
            target_id=row.id,
            metadata_json={
                "run_id": str(ctx.run_id),
                "tool_call_id": str(ctx.tool_call_id) if ctx.tool_call_id else None,
                **metadata,
            },
        )
    )


def _record_checkout(ctx: ToolExecutionContext, metadata: Mapping[str, Any]) -> None:
    """Write Jhin's own account of a completed checkout.

    ``cli.repository.push`` reads it back to answer two questions it must not
    ask the container: which ref this branch was cut from, and what the
    repository's git config looked like when Jhin was last the one writing it.
    The row lives in the append-only audit table, keyed on the run, so no
    sandbox job can reach it and the operator sees the same facts the push
    checked.
    """
    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action=_CHECKOUT_RECORD_ACTION,
            target_type=_CHECKOUT_RECORD_TARGET,
            target_id=ctx.run_id,
            metadata_json={
                "run_id": str(ctx.run_id),
                "tool_call_id": str(ctx.tool_call_id) if ctx.tool_call_id else None,
                **metadata,
            },
        )
    )


async def _checkout_record(ctx: ToolExecutionContext, repository: str) -> Mapping[str, Any]:
    """The most recent checkout this run recorded, which must be the one on
    disk. A push for any other repository — or with no record at all — is
    refused before a container starts."""
    event = await ctx.session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.workspace_id == ctx.workspace_id,
            AuditEvent.action == _CHECKOUT_RECORD_ACTION,
            AuditEvent.target_type == _CHECKOUT_RECORD_TARGET,
            AuditEvent.target_id == ctx.run_id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if event is None:
        raise _refusal("no_checkout_record")
    metadata = event.metadata_json or {}
    if str(metadata.get("repository") or "") != repository:
        raise _refusal(
            "no_checkout_record",
            detail=f"this run last checked out {metadata.get('repository') or 'nothing'}",
        )
    return metadata


def _job_output_fields(row: SandboxJob, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_job_id": str(row.id),
        "status": row.status,
        "exit_code": row.exit_code,
        "duration_ms": row.duration_ms,
        "stdout": row.stdout_tail,
        "stderr": row.stderr_tail,
        "stdout_truncated": bool(result.get("stdout_truncated", False)),
        "stderr_truncated": bool(result.get("stderr_truncated", False)),
    }


def _in_repo(script: str) -> str:
    """Command-style jobs start in the checkout when one exists."""
    return f"if [ -d {_REPO_PATH} ]; then cd {_REPO_PATH}; fi\n{script}"


def _guarded(script: str) -> str:
    """A file job: start in the checkout, then refuse anything that resolves
    outside it or into ``.git``."""
    return _in_repo(f"set -e\n{_GUARD_PROLOGUE}{script}")


# --- cli.command.execute ---


async def _command_execute(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CommandExecuteInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, default_network, _ = _connection_defaults(connection)
    image = data.image or default_image
    network = data.network or default_network
    timeout = data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT

    # No credential, ever. A grant scope is one fnmatch over a shell string,
    # so it cannot constrain what a command does with a secret in its
    # environment; the only containment is not putting one there.
    row, result = await _run_job(
        ctx,
        command_display=data.command,
        argv=["bash", "-c", _in_repo(data.command)],
        image=image,
        network=network,
        timeout_seconds=timeout,
        env={"HOME": _WORKSPACE_PATH},
    )
    return CommandExecuteOutput(command=data.command, **_job_output_fields(row, result))


# --- cli.repository.checkout ---


def _slug(repository: str) -> str:
    name = repository.split("/", 1)[-1].lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")
    return cleaned[:40] or "repo"


def _clone_url(git_base: str, repository: str) -> str:
    """The remote Jhin will clone from and push to.

    The repository is joined onto a URL, so anything that is not an ordinary
    ``owner/name`` walks out of the path prefix the credential was scoped
    around, and the URL the audit trail records is no longer where the objects
    went. A literal ``..`` is only the obvious spelling — a server that
    percent-decodes reads ``..%2fevil`` the same way — so the check is
    :func:`is_plain_repository`, which says what a name *is*. The schema
    refuses the same shapes (``cli/schemas.py``); this is that rule restated
    where the URL is actually built, so no future caller can join a value that
    never passed through it.
    """
    if not is_plain_repository(repository):
        raise CliToolError(f"repository '{repository}' is not an owner/name pair")
    return f"{git_base}/{repository}.git"


async def _repository_checkout(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryCheckoutInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, git_connection_id = _connection_defaults(connection)
    git_base, token = await _git_credentials(ctx, git_connection_id)
    branch = data.branch or f"agent/{str(ctx.task_id)[:8]}-{_slug(data.repository)}"
    clone_url = _clone_url(git_base, data.repository)

    ref_arg = f"--branch {shlex.quote(data.ref)} " if data.ref else ""
    credential = _credential_args(git_base)
    trailer = _new_trailer()
    script = (
        "set -e\n"
        f"cd {_WORKSPACE_PATH}\n"
        f"rm -rf {_REPO_PATH}\n"
        f"git {credential} clone {ref_arg}{shlex.quote(clone_url)} {_REPO_PATH}\n"
        f"cd {_REPO_PATH}\n"
        'git config user.name "Jhin Agent"\n'
        'git config user.email "agent@jhin.local"\n'
        "jhin_base=$(git rev-parse --abbrev-ref HEAD)\n"
        f"git checkout -b {shlex.quote(branch)}\n"
        # The listing is the one value here that repository content decides, so
        # it is collected first and emitted as a single base64 word: NUL-
        # separated inside the encoding, where `git` allows a newline in a file
        # name and `find`'s %f would otherwise print it raw. Nothing that
        # reaches the trailer region can carry a line break, let alone a
        # second sentinel.
        r"jhin_top=$(find . -maxdepth 1 -mindepth 1 -name .git -prune -o -printf '%y:%f\0'"
        " | LC_ALL=C sort -z | head -z -n 100 | base64 -w0)\n"
        + trailer.echo
        + "printf 'head=%s\\n' \"$(git rev-parse HEAD)\"\n"
        "printf 'base=%s\\n' \"$jhin_base\"\n"
        # The config as Jhin leaves it. cli.repository.push compares the file
        # against this sha, so any later rewrite — by any route, including one
        # nobody has thought of — stops the push instead of travelling with it.
        "printf 'config=%s\\n' \"$(sha256sum -- .git/config | cut -c1-64)\"\n"
        "printf 'top=%s\\n' \"$jhin_top\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"git clone {clone_url} && git checkout -b {branch}",
        argv=["bash", "-c", script],
        image=data.image or default_image,
        network="internet",  # clone always needs the sandbox bridge
        timeout_seconds=data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT,
        env=_git_env(),
        secret_env={"GIT_TOKEN": token},
        audit_metadata={
            "git_connection_id": git_connection_id,
            "remote_host": _remote_host(git_base),
            "repository": data.repository,
            "branch": branch,
        },
    )
    _raise_for_failure(row, what="checkout")

    _, entries = trailer.split(_raw_stdout(result))
    head_sha = _meta_one(entries, "head")
    base_ref = _meta_one(entries, "base")
    config_sha = _meta_one(entries, "config")
    # Every recorded value is checked for its shape before it becomes the
    # record a push trusts. An unreadable trailer records *nothing*: a missing
    # config sha used to mean "skip the comparison", which is the one outcome
    # an attacker would choose, so it now means "there was no checkout".
    if not (
        _OBJECT_ID.match(head_sha) and _REF_NAME.match(base_ref) and _CONFIG_SHA.match(config_sha)
    ):
        raise _refusal("checkout_unrecordable")
    _record_checkout(
        ctx,
        {
            "repository": data.repository,
            "branch": branch,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "config_sha": config_sha,
            "remote_url": clone_url,
            "remote_host": _remote_host(clone_url),
            "git_connection_id": git_connection_id,
        },
    )
    return RepositoryCheckoutOutput(
        repository=data.repository,
        branch=branch,
        head_sha=head_sha,
        base_ref=base_ref,
        top_level=_top_level(entries),
        path=_REPO_PATH,
        **_job_output_fields(row, result),
    )


# --- cli.repository.push ---


async def _repository_push(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryPushInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, git_connection_id = _connection_defaults(connection)
    # Jhin's own account of the checkout, read before a credential is minted:
    # a push with nothing to compare against never reaches the runner.
    record = await _checkout_record(ctx, data.repository)
    recorded_base = str(record.get("base_ref") or "")
    recorded_config = str(record.get("config_sha") or "")
    # Both are re-checked even though Jhin wrote them: they originated as
    # lines of container stdout. A record missing either one is not a weaker
    # record, it is no record — skipping a comparison because the value Jhin
    # holds is empty is exactly the outcome an attacker on the checkout's
    # output would be aiming for.
    if not _REF_NAME.match(recorded_base) or not _CONFIG_SHA.match(recorded_config):
        raise _refusal(
            "no_checkout_record",
            detail="the recorded checkout is incomplete, so there is nothing to compare against",
        )

    git_base, token = await _git_credentials(ctx, git_connection_id)
    clone_url = _clone_url(git_base, data.repository)
    branch = shlex.quote(data.branch)
    refspec = shlex.quote(f"refs/heads/{data.branch}:refs/heads/{data.branch}")
    quoted_url = shlex.quote(clone_url)
    credential = _credential_args(git_base)

    # Nothing here is model-authored, and nothing here is *container*-authored
    # either: the destination URL, the refspec, the base branch, the expected
    # config and every pre-flight check come from Jhin. The model supplied a
    # repository, a branch name (matched against ^[\w./-]+$ by the schema) and
    # a commit message that travels in the environment, never in argv.
    trailer = _new_trailer()
    script = (
        "set -e\n"
        f"cd {_REPO_PATH} 2>/dev/null || "
        "{ printf 'JHIN_ERR=no_checkout\\n' >&2; exit 65; }\n"
        "jhin_head=$(git rev-parse --abbrev-ref HEAD)\n"
        f'if [ "$jhin_head" != {branch} ]; then '
        "printf 'JHIN_ERR=branch_not_checked_out\\n' >&2; exit 66; fi\n"
        f"case {branch} in main|master|HEAD) "
        "printf 'JHIN_ERR=push_to_base_refused\\n' >&2; exit 67 ;; esac\n"
        # The base is the ref the checkout was cut from, as Jhin recorded it —
        # not refs/remotes/origin/HEAD, which is the remote's default branch
        # and is in any case a ref inside the repository the agent works in.
        f"if [ {branch} = {shlex.quote(recorded_base)} ]; then "
        "printf 'JHIN_ERR=push_to_base_refused\\n' >&2; exit 67; fi\n"
        "jhin_bad=$(git config --local --list --name-only | "
        f"grep -v -E {shlex.quote(_ALLOWED_REPO_CONFIG)} || true)\n"
        "if [ -n \"$jhin_bad\" ]; then printf 'JHIN_ERR=repo_config_tampered\\n' >&2; "
        "printf 'JHIN_KEYS=%s\\n' \"$(echo \"$jhin_bad\" | tr '\\n' ',')\" >&2; exit 68; fi\n"
        # Values, not just names: remote.origin.url is an allowed *key*, and a
        # key that has been given a second value passes a name-only audit while
        # ``git push origin`` delivers to every one of them.
        "jhin_url_count=$(git config --local --get-all remote.origin.url | wc -l | tr -d ' ')\n"
        "jhin_origin=$(git config --local --get-all remote.origin.url | head -n 1)\n"
        f'if [ "$jhin_url_count" != "1" ] || [ "$jhin_origin" != {quoted_url} ]; then '
        "printf 'JHIN_ERR=remote_rewritten\\n' >&2; "
        "printf 'JHIN_URLS=%s\\n' "
        "\"$(git config --local --get-all remote.origin.url | tr '\\n' ',')\" >&2; exit 69; fi\n"
        # And the config byte for byte as the checkout left it, which covers
        # every key and value nobody has thought to enumerate.
        "jhin_config=$(sha256sum -- .git/config | cut -c1-64)\n"
        f'if [ "$jhin_config" != {shlex.quote(recorded_config)} ]; then '
        "printf 'JHIN_ERR=repo_config_tampered\\n' >&2; "
        "printf 'JHIN_KEYS=%s\\n' '.git/config changed since the checkout' >&2; "
        "exit 68; fi\n"
        "jhin_previous=$(git rev-parse HEAD)\n"
        "git add -A\n"
        "if ! git diff --cached --quiet; then\n"
        "  git -c core.hooksPath=/nonexistent commit --no-verify "
        '-m "$JHIN_COMMIT_MESSAGE"\n'
        "fi\n"
        # The URL, never the name. ``origin`` is a pointer the container owns;
        # this URL is Jhin's, so rewriting the remote redirects nothing.
        f"git {credential} push {quoted_url} {refspec}\n"
        + trailer.echo
        + "printf 'previous=%s\\n' \"$jhin_previous\"\n"
        "printf 'pushed=%s\\n' \"$(git rev-parse HEAD)\"\n"
    )
    audit_metadata = {
        "git_connection_id": git_connection_id,
        # The URL git was actually given, not a re-derivation of where a push
        # "should" go: these are the objects' real destination.
        "remote_url": clone_url,
        "remote_host": _remote_host(clone_url),
        "repository": data.repository,
        "branch": data.branch,
        "base_ref": recorded_base,
    }

    def completion(result: dict[str, Any]) -> Mapping[str, Any]:
        _, entries = trailer.split(_raw_stdout(result))
        return {
            "previous_sha": _meta_one(entries, "previous"),
            "pushed_sha": _meta_one(entries, "pushed"),
        }

    row, result = await _run_job(
        ctx,
        command_display=f"git push {clone_url} {data.branch}",
        argv=["bash", "-c", script],
        image=default_image,
        network="internet",  # push always needs the sandbox bridge
        timeout_seconds=data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT,
        env={**_git_env(), "JHIN_COMMIT_MESSAGE": data.commit_message},
        secret_env={"GIT_TOKEN": token},
        audit_metadata=audit_metadata,
        completion_metadata=completion,
    )

    if row.status != SandboxJobStatus.COMPLETED.value or row.exit_code != 0:
        code = _refusal_code(row)
        if code == "repo_config_tampered":
            _security_audit(ctx, row, {**audit_metadata, "keys": _tampered_keys(row)})
        if code == "remote_rewritten":
            # Where it would have gone. The push refused, so this names an
            # attempt rather than a destination — which is the point.
            _security_audit(
                ctx,
                row,
                {**audit_metadata, "keys": "remote.origin.url", "observed_urls": _urls(row)},
            )
        if code in _REFUSAL_HINTS:
            raise _refusal(code)
        # The push itself failed: a 502 after the ref was updated looks exactly
        # like one before it, so this is not provably side-effect free.
        raise ToolExecutionError(
            f"push failed ({row.status}, exit {row.exit_code})",
            code="push_failed",
            side_effect_possible=True,
            hint="The push did not complete. Check the branch on the remote before retrying.",
        )

    _, entries = trailer.split(_raw_stdout(result))
    return RepositoryPushOutput(
        repository=data.repository,
        branch=data.branch,
        # The URL the objects went to, so the model's own transcript agrees
        # with the audit event rather than naming a remote alias.
        remote=clone_url,
        previous_sha=_meta_one(entries, "previous"),
        pushed_sha=_meta_one(entries, "pushed"),
        **_job_output_fields(row, result),
    )


def _stderr_marker(row: SandboxJob, prefix: str) -> str:
    for line in reversed((row.stderr_tail or "").splitlines()):
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()[:500]
    return ""


def _tampered_keys(row: SandboxJob) -> str:
    return _stderr_marker(row, "JHIN_KEYS=")


def _urls(row: SandboxJob) -> str:
    return _stderr_marker(row, "JHIN_URLS=")


# --- cli.test.run ---


async def _test_run(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(TestRunInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    row, result = await _run_job(
        ctx,
        command_display=data.command,
        argv=["bash", "-c", _in_repo(data.command)],
        image=data.image or default_image,
        # The command is arbitrary, so the network is Jhin's decision, not the
        # model's. Operators who need networked tests grant cli.command.execute
        # with a narrow scope.
        network="none",
        timeout_seconds=data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT,
        env={"HOME": _WORKSPACE_PATH},
    )
    return TestRunOutput(
        command=data.command,
        passed=row.exit_code == 0 and row.status == SandboxJobStatus.COMPLETED.value,
        **_job_output_fields(row, result),
    )


# --- cli.file.read ---


async def _file_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileReadInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    quoted = shlex.quote(data.path)
    last = data.offset + data.limit - 1
    trailer = _new_trailer()
    script = _guarded(
        f"jhin_guard {quoted}\n"
        f"test -f {quoted} || {{ printf 'JHIN_ERR=not_a_file\\n' >&2; exit 65; }}\n"
        f"sed -n '{data.offset},{last}p' -- {quoted} | head -c {_READ_PAGE_BYTES}\n"
        + trailer.echo
        + f"printf 'total=%s\\n' \"$(awk 'END{{print NR+0}}' {quoted})\"\n"
        f"printf 'sha=%s\\n' \"$(sha256sum -- {quoted} | cut -c1-64)\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"read {data.path} lines {data.offset}-{last}",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",  # file reads never need egress
        timeout_seconds=data.timeout_seconds or _DEFAULT_FILE_TIMEOUT,
    )
    _raise_for_failure(row, what="file read")

    body, entries = trailer.split(_raw_stdout(result))
    total_lines = _meta_int(entries, "total", default=0)
    truncated = bool(result.get("stdout_truncated", False))
    if len(body.encode()) >= _READ_PAGE_BYTES or len(body) > _MAX_FILE_CHARS:
        truncated = True
        body = body[:_MAX_FILE_CHARS]
        cut = body.rfind("\n")
        body = body[: cut + 1] if cut >= 0 else body
    returned = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
    last_line = data.offset + returned - 1 if returned else data.offset - 1
    has_more = last_line < total_lines
    # The token comes back only when this page IS the whole file: it starts at
    # line one, nothing was cut, and it reaches the end. cli.file.write replaces
    # the entire file, so a token earned by a partial read would let an agent
    # write back the page it saw and silently drop the rest -- the exact data
    # loss the token exists to prevent. A partial reader gets no token and must
    # either read the whole file or use cli.file.edit.
    whole_file = data.offset <= 1 and not truncated and not has_more
    return FileReadOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        content=body,
        truncated=truncated,
        first_line=data.offset,
        last_line=last_line,
        total_lines=total_lines,
        has_more=has_more,
        read_token=_read_token(entries) if whole_file else "",
    )


# --- cli.file.write ---


async def _file_write(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileWriteInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    quoted = shlex.quote(data.path)
    trailer = _new_trailer()
    script = _guarded(
        f"jhin_guard {quoted}\n"
        f"if [ -e {quoted} ]; then\n"
        f"  [ -f {quoted} ] || {{ printf 'JHIN_ERR=not_a_file\\n' >&2; exit 65; }}\n"
        '  if [ -z "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_exists_pass_read_token\\n' >&2; exit 65; fi\n"
        f"  jhin_actual=$(sha256sum -- {quoted} | cut -c1-64)\n"
        '  if [ "$jhin_actual" != "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_changed\\n' >&2; exit 65; fi\n"
        "else\n"
        '  if [ -n "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_missing_for_read_token\\n' >&2; exit 65; fi\n"
        "fi\n"
        f'mkdir -p -- "$(dirname -- {quoted})"\n'
        f"jhin_guard {quoted}\n"
        f"printf '%s' \"$JHIN_FILE_CONTENT\" > {quoted}\n"
        + trailer.echo
        + f"printf 'bytes=%s\\n' \"$(wc -c < {quoted})\"\n"
        f"printf 'sha=%s\\n' \"$(sha256sum -- {quoted} | cut -c1-64)\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"write {data.path} ({len(data.content)} chars)",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",  # file writes never need egress
        timeout_seconds=data.timeout_seconds or _DEFAULT_FILE_TIMEOUT,
        env={"JHIN_FILE_CONTENT": data.content, "JHIN_READ_TOKEN": data.read_token},
    )
    _raise_for_failure(row, what="file write")

    _, entries = trailer.split(_raw_stdout(result))
    bytes_written = _meta_int(entries, "bytes", default=len(data.content.encode()))
    return FileWriteOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        bytes_written=bytes_written,
        read_token=_read_token(entries),
    )


# --- cli.file.edit ---


_EDIT_PROGRAM = r"""import hashlib, os, stat, sys
path = os.environ["JHIN_EDIT_PATH"]
old = os.environ["JHIN_EDIT_OLD"]
new = os.environ["JHIN_EDIT_NEW"]
expected = int(os.environ["JHIN_EDIT_EXPECTED"])
try:
    handle = open(path, "r+b")
except FileNotFoundError:
    sys.stderr.write("JHIN_ERR=file_not_found\n")
    raise SystemExit(65)
except IsADirectoryError:
    sys.stderr.write("JHIN_ERR=not_a_file\n")
    raise SystemExit(65)
with handle:
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        sys.stderr.write("JHIN_ERR=not_a_file\n")
        raise SystemExit(65)
    # A second name for this inode is a second file being rewritten, and the
    # other name may be one the schema would have refused. Asked of the open
    # descriptor, so no link can appear between the check and the write.
    if info.st_nlink != 1:
        sys.stderr.write("JHIN_ERR=hard_linked_file\n")
        raise SystemExit(65)
    try:
        data = handle.read().decode("utf-8")
    except UnicodeDecodeError:
        sys.stderr.write("JHIN_ERR=file_not_text\n")
        raise SystemExit(65)
    count = data.count(old)
    if count != expected:
        sys.stderr.write("JHIN_ERR=edit_count_mismatch\n")
        sys.stderr.write("JHIN_ACTUAL=%d\n" % count)
        raise SystemExit(65)
    written = data.replace(old, new).encode("utf-8")
    handle.seek(0)
    handle.truncate(0)
    handle.write(written)
# Trailer *entries* only. The sentinel is the shell's to print, from the one
# place that knows this job's nonce; a program that printed its own would be a
# second emitter, and the two would drift (they did).
sys.stdout.write("replacements=%d\n" % count)
sys.stdout.write("sha=%s\n" % hashlib.sha256(written).hexdigest())
"""


async def _file_edit(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileEditInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    # Both strings and the path travel in the environment: never in argv, so
    # nothing about them can be read from ``ps`` or reinterpreted by a shell.
    trailer = _new_trailer()
    # The program's stdout is captured, not printed: the sentinel has to come
    # first and only the shell prints one, so the entries wait in a variable
    # until it has. ``set -e`` still carries the program's refusal exit code
    # out through the assignment, and its stderr — the JHIN_ERR lines — is
    # untouched.
    script = _guarded(
        'jhin_guard "$JHIN_EDIT_PATH"\n'
        "jhin_entries=$(python3 - <<'JHIN_EDIT_PY'\n"
        f"{_EDIT_PROGRAM}JHIN_EDIT_PY\n)\n" + trailer.echo + "printf '%s\\n' \"$jhin_entries\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"edit {data.path} (expect {data.expected_count})",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",
        timeout_seconds=data.timeout_seconds or _DEFAULT_FILE_TIMEOUT,
        env={
            "JHIN_EDIT_PATH": data.path,
            "JHIN_EDIT_OLD": data.old_string,
            "JHIN_EDIT_NEW": data.new_string,
            "JHIN_EDIT_EXPECTED": str(data.expected_count),
        },
    )
    if row.status != SandboxJobStatus.COMPLETED.value or row.exit_code != 0:
        code = _refusal_code(row)
        if code == "edit_count_mismatch":
            raise ToolExecutionError(
                "edit_count_mismatch",
                code="edit_count_mismatch",
                side_effect_possible=False,
                hint=(
                    "old_string occurred "
                    f"{_actual_count(row)} time(s), not {data.expected_count}. "
                    "Read the file and retry with a unique old_string."
                ),
            )
        _raise_for_failure(row, what="file edit")

    _, entries = trailer.split(_raw_stdout(result))
    replacements = _meta_int(entries, "replacements", default=data.expected_count)
    return FileEditOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        replacements=replacements,
        read_token=_read_token(entries),
    )


def _actual_count(row: SandboxJob) -> str:
    for line in reversed((row.stderr_tail or "").splitlines()):
        if line.startswith("JHIN_ACTUAL="):
            return line.removeprefix("JHIN_ACTUAL=").strip()[:12]
    return "a different number of"


# --- cli.file.list ---


async def _file_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileListInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    base = shlex.quote(data.path or ".")
    name_filter = f"-name {shlex.quote(data.glob)} " if data.glob else ""
    trailer = _new_trailer()
    # Every field here except the path is Jhin's; the path is repository
    # content, and a file name may contain both a tab and a newline. So the
    # rows never travel as lines of the payload: each is NUL-terminated (the
    # one byte a path cannot hold), the whole listing reaches the trailer as a
    # single base64 word, and the path — which may still contain a tab — is
    # read from the *left* of two right-hand separators rather than by
    # splitting. Printing them raw is how a file called
    # ``match<newline>shadow:1:x`` used to end one row and start another.
    script = _guarded(
        f"jhin_guard {base}\n"
        f"jhin_rows=$(find {base} -maxdepth {data.max_depth} -mindepth 1 "
        r"\( -name .git -o -name '.jhin*' \) -prune -o "
        f"{name_filter}"
        r"-printf '%p\t%y\t%s\0' 2>/dev/null"
        f" | LC_ALL=C sort -z | head -z -n {data.max_entries + 1}"
        f" | head -c {_MAX_ENCODED_BYTES} | base64 -w0)\n"
        + trailer.echo
        + "printf 'rows=%s\\n' \"$jhin_rows\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"list {data.path or '.'}{f' ({data.glob})' if data.glob else ''}",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",
        timeout_seconds=data.timeout_seconds or _DEFAULT_FILE_TIMEOUT,
    )
    _raise_for_failure(row, what="file list")

    _, meta = trailer.split(_raw_stdout(result))
    raw = _decoded(meta, "rows")
    # The last element is either the empty string after the final terminator
    # or a record the byte cap cut in half; neither is a row.
    records = raw.decode("utf-8", "replace").split("\0")[:-1]
    truncated = len(records) > data.max_entries or len(raw) >= _MAX_ENCODED_BYTES
    entries: list[FileEntry] = []
    budget = _MAX_RESULT_BYTES
    for record in records[: data.max_entries]:
        head, separator, size = record.rpartition("\t")
        path, kind_separator, kind = head.rpartition("\t")
        if not separator or not kind_separator or not path:
            continue
        path = _displayable(path[2:] if path.startswith("./") else path)
        budget -= len(path) + 24
        if budget <= 0:
            truncated = True
            break
        entries.append(
            FileEntry(
                path=path,
                kind={"d": "directory", "f": "file", "l": "symlink"}.get(kind, kind),
                size_bytes=int(size) if size.isdigit() else 0,
            )
        )
    return FileListOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        entries=entries,
        truncated=truncated,
    )


# --- cli.file.search ---


async def _file_search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileSearchInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    base = shlex.quote(data.path or ".")
    include = f"--include={shlex.quote(data.glob)} " if data.glob else ""
    mode = "-E " if data.regex else "-F "
    trailer = _new_trailer()
    # ``-Z`` is the whole difference: grep terminates the file name with a NUL
    # instead of the ``:`` the parser used to split on, so a file called
    # ``shadow:1:JHIN planted`` is a name and not a match at ``shadow`` line 1.
    # The name may still contain a newline, so the stream is read as bytes
    # rather than lines and reaches the trailer as one base64 word. Framing:
    # ``<name>\0<line>:<text>\n`` — the NUL cannot occur in a name, and the
    # text is one line because grep prints one line per match.
    script = _guarded(
        f"jhin_guard {base}\n"
        "jhin_hits=$(grep -rnIZ --exclude-dir=.git --exclude-dir='.jhin*' "
        f"{include}{mode}-e {shlex.quote(data.pattern)} -- {base} 2>/dev/null"
        f" | head -c {_MAX_ENCODED_BYTES} | base64 -w0)\n"
        + trailer.echo
        + "printf 'hits=%s\\n' \"$jhin_hits\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"search {data.path or '.'} for a pattern",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",
        timeout_seconds=data.timeout_seconds or _DEFAULT_FILE_TIMEOUT,
    )
    _raise_for_failure(row, what="file search")

    _, meta = trailer.split(_raw_stdout(result))
    raw = _decoded(meta, "hits")
    matches: list[FileMatch] = []
    truncated = len(raw) >= _MAX_ENCODED_BYTES
    budget = _MAX_RESULT_BYTES
    position = 0
    while position < len(raw):
        end_of_name = raw.find(b"\0", position)
        end_of_line = raw.find(b"\n", end_of_name + 1) if end_of_name >= 0 else -1
        if end_of_name < 0 or end_of_line < 0:
            # A record the byte cap cut in half: reported as more to come, not
            # guessed at.
            truncated = True
            break
        name = raw[position:end_of_name].decode("utf-8", "replace")
        number, separator, text = (
            raw[end_of_name + 1 : end_of_line].decode("utf-8", "replace").partition(":")
        )
        position = end_of_line + 1
        if len(matches) >= data.max_matches:
            truncated = True
            break
        if not separator or not number.isdigit():
            continue
        path = _displayable(name[2:] if name.startswith("./") else name)
        text = _displayable(text[:_MAX_MATCH_CHARS])
        budget -= len(path) + len(text) + 32
        if budget <= 0:
            truncated = True
            break
        matches.append(FileMatch(path=path, line=int(number), text=text))
    return FileSearchOutput(
        sandbox_job_id=str(row.id),
        pattern=data.pattern,
        matches=matches,
        truncated=truncated,
    )


CLI_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="cli.command.execute",
            description=(
                "Run a shell command inside an ephemeral sandbox container. The "
                "workspace (and any repository checkout at /workspace/repo) "
                "persists across calls within one run. This tool never holds a "
                "git credential: commit and push a branch with "
                "cli.repository.push instead."
            ),
            risk=RiskLevel.WRITE,
            input_model=CommandExecuteInput,
            output_model=CommandExecuteOutput,
            required_capability="cli.command.execute",
            supports_approval=True,
            scope_keys=("connection_id", "command", "image", "network"),
        ),
        _command_execute,
    ),
    (
        ToolDefinition(
            name="cli.repository.checkout",
            description=(
                "Clone a repository into the sandbox workspace using a short-lived "
                "credential and create a working branch (default: agent/<task>-<repo>). "
                "Returns the branch, the base ref it was cut from, and the top-level "
                "entries so you can start navigating. Explore with cli.file.list and "
                "cli.file.search, change files with cli.file.edit, then land the branch "
                "with cli.repository.push and open the pull request from it; do not "
                "create the branch through the GitHub API, that would give the pull "
                "request no changes."
            ),
            risk=RiskLevel.WRITE,
            input_model=RepositoryCheckoutInput,
            output_model=RepositoryCheckoutOutput,
            required_capability="cli.repository.checkout",
            supports_approval=True,
            scope_keys=("connection_id", "repository", "image"),
            required_grant_scope_keys=("connection_id", "repository"),
        ),
        _repository_checkout,
    ),
    (
        ToolDefinition(
            name="cli.repository.push",
            description=(
                "Commit everything in the sandbox checkout and push the working "
                "branch to its origin. Jhin owns the remote and the refspec: the "
                "branch must be the one checked out, it may not be the base branch, "
                "and the push is never forced. Open the pull request afterwards with "
                "github.pull_request.create."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=RepositoryPushInput,
            output_model=RepositoryPushOutput,
            required_capability="cli.repository.push",
            supports_approval=True,
            scope_keys=("connection_id", "repository", "branch"),
            # ``branch`` is required, not merely available. Which branches an
            # agent may land on is the whole difference between "opens a pull
            # request" and "writes to the trunk", and a grant that names only a
            # repository would leave that to the in-sandbox refusals alone.
            required_grant_scope_keys=("connection_id", "repository", "branch"),
        ),
        _repository_push,
    ),
    (
        ToolDefinition(
            name="cli.test.run",
            description=(
                "Run a test command in the sandbox workspace and report pass/fail "
                "with output. The job is fully isolated: no network, and no git "
                "credential. The command is an ordinary shell command running in "
                "the checkout, so it can change the files there — use "
                "cli.file.edit for changes you intend, and this for running them."
            ),
            # An arbitrary shell in a writable checkout is not a read. It runs
            # between the model's last visible action and a human's push
            # approval, and a grant scope is one fnmatch over a shell string:
            # "python3 -m pytest*" matches "python3 -m pytest -x; <anything>".
            # WRITE still auto-runs under Autonomous and Balanced, so tests keep
            # running unattended where the operator asked for that; Restricted
            # now sees it, which is what Restricted promises. Containment is
            # structural, not risk-level: cli.repository.push trusts nothing
            # this command could have touched.
            risk=RiskLevel.WRITE,
            input_model=TestRunInput,
            output_model=TestRunOutput,
            required_capability="cli.test.run",
            supports_approval=True,
            scope_keys=("connection_id", "command", "image"),
        ),
        _test_run,
    ),
    (
        ToolDefinition(
            name="cli.file.list",
            description=(
                "List files and directories in the sandbox checkout. Start here on a "
                "repository you have not seen: path='' lists the top of the tree, and "
                "glob filters one path segment (e.g. '*.py')."
            ),
            risk=RiskLevel.READ,
            input_model=FileListInput,
            output_model=FileListOutput,
            required_capability="cli.file.list",
            scope_keys=("connection_id", "path"),
        ),
        _file_list,
    ),
    (
        ToolDefinition(
            name="cli.file.search",
            description=(
                "Find where a string appears in the sandbox checkout: returns "
                "path, line number and the matching line. The pattern is a fixed "
                "string unless regex=true. Use it to locate a symbol before reading."
            ),
            risk=RiskLevel.READ,
            input_model=FileSearchInput,
            output_model=FileSearchOutput,
            required_capability="cli.file.search",
            scope_keys=("connection_id", "path"),
        ),
        _file_search,
    ),
    (
        ToolDefinition(
            name="cli.file.read",
            description=(
                "Read part of one file from the sandbox workspace (path relative to the "
                "checkout). Returns a line window plus total_lines, has_more and a "
                "read_token; page through a large file with offset and limit. Keep the "
                "read_token: cli.file.write requires it."
            ),
            risk=RiskLevel.READ,
            input_model=FileReadInput,
            output_model=FileReadOutput,
            required_capability="cli.file.read",
            scope_keys=("connection_id", "path"),
        ),
        _file_read,
    ),
    (
        ToolDefinition(
            name="cli.file.edit",
            description=(
                "Replace an exact string in one file of the sandbox checkout. "
                "old_string must occur exactly expected_count times or nothing is "
                "written and the real count is reported. This is the safe way to change "
                "part of a file you have only read part of."
            ),
            risk=RiskLevel.WRITE,
            input_model=FileEditInput,
            output_model=FileEditOutput,
            required_capability="cli.file.edit",
            supports_approval=True,
            scope_keys=("connection_id", "path"),
        ),
        _file_edit,
    ),
    (
        ToolDefinition(
            name="cli.file.write",
            description=(
                "Write one whole file in the sandbox workspace (path relative to the "
                "checkout). read_token is the token from a cli.file.read of that file, "
                "or empty for a file that does not exist yet — so a partial read can "
                "never overwrite the rest of a file. To change part of a file, prefer "
                "cli.file.edit. The change exists only in the sandbox until "
                "cli.repository.push lands it."
            ),
            risk=RiskLevel.WRITE,
            input_model=FileWriteInput,
            output_model=FileWriteOutput,
            required_capability="cli.file.write",
            supports_approval=True,
            scope_keys=("connection_id", "path"),
        ),
        _file_write,
    ),
)
