"""CLI tool executors (plan 11.6, 14.5): every tool is one sandbox job.

Execution path: the gateway has already authorized the call (capability +
scope: connection, command pattern, image, network, repository, path, branch,
plus the CLI connector's repository allow-list validator). Here the job is
submitted to the sandbox runner over the internal API, polled to completion,
and recorded:

- one ``sandbox_job`` row per job, linked to run/task/tool_call, committed on
  its own connection before the job is submitted — so it survives the
  gateway's rollback, and so a re-dispatch of the same call can find it;
- append-only audit events ``sandbox.job.started`` / ``completed`` /
  ``failed``, plus ``sandbox.repo_config_tampered`` when a push finds the
  repository's own git config rewritten;
- stdout/stderr redacted (runner-side against the job's secret env, worker-
  side against the process redactor) and size-capped before persistence.

Workspace persistence (``cli/workspace.py``, docs/architecture/sandboxing.md):
every job of one agent shares a named volume derived from that agent's
identity, mounted at ``/workspace``, and it survives the run that made it — so
a checkout, a dependency install and a build cache are still there on the
agent's next turn. A second concurrent run of the same agent gets a private
``run-<run_id>`` volume instead of sharing the tree. Repository checkouts land
in ``/workspace/repo`` and command-style jobs start there when it exists.

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

import asyncio
import base64
import binascii
import hashlib
import hmac
import re
import secrets
import shlex
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_connectors.cli.managed_files import FILE_PUBLISH_TOOL, file_publish, stage_chat_inputs
from jhin_connectors.cli.output import sanitized_output_tail
from jhin_connectors.cli.runner_client import (
    SandboxInvocationUnknownError,
    SandboxRunnerError,
    run_sandbox_job,
    runner_config,
    sandbox_job_progress,
)
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
from jhin_connectors.cli.validators import forbidden_repositories_on_disk, is_plain_repository
from jhin_connectors.cli.workspace import (
    AUDIT_CHECKOUT_RECORDED,
    WORKSPACE_REFUSAL_HINTS,
    WorkspaceBinding,
    bind_workspace,
    record_size,
)
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
# How much of a failure's own words a refusal carries. Enough to name the
# thing that went wrong, short enough that a driver's stack trace cannot
# become the tool's error message.
_MAX_REASON_CHARS = 300

_WORKSPACE_PATH = "/workspace"
_REPO_PATH = "/workspace/repo"

# Machine-readable trailer. It goes *after* the payload because the sandbox
# runner keeps the tail of oversized output, so a trailer always survives
# while a header would not — which means the parser, not the position, has to
# be what makes it trustworthy. Four rules do that, and all four are needed:
#
# 1. the sentinel carries a nonce Jhin draws per *invocation*. Nothing in the
#    container can predict it, so no byte a repository (or a model) chose can
#    write one;
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
    """One invocation's trailer: the shell line that emits it, and its parser."""

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


def _new_trailer(ctx: ToolExecutionContext | None = None) -> _Trailer:
    """This call's trailer — the same one for every dispatch of it.

    **A re-dispatch has to be able to read the answer it is given.** The runner
    answers a second dispatch of one invocation with the *first* dispatch's
    job, output included, which is what stops a container running twice. A
    nonce drawn per dispatch made that answer unreadable: the replayed stdout
    carries dispatch one's sentinel while dispatch two's parser looks for its
    own, :meth:`_Trailer.split` finds none, and every value falls back to a
    default — an empty ``read_token``, an empty ``pushed_sha``, a checkout that
    refuses itself as unrecordable. Reproduced end to end, and it made the
    idempotency the runner had just provided useless to the caller.

    So the nonce is a property of the invocation rather than of the attempt:
    HMAC-SHA256 over the tool call id, keyed on the shared runner token, which
    is stable across a worker restart, identical on every worker replica (a
    re-dispatch need not land on the process that made the first one), and
    never enters a container. The unforgeability argument is unchanged and is
    what the key is for: a container sees the nonce of the job it *is* — it is
    printed by its own script — and can predict no other job's, because every
    other job is a different tool call and the key is not on that disk. What it
    could always do is print a second sentinel of its own, and that still voids
    the trailer rather than replacing it.

    A call with no invocation identity gets a drawn nonce: nothing keys
    idempotency on it, so no dispatch of it is ever answered with another's
    output. Without a configured runner token there is no key — and no
    dispatch either, since :func:`run_sandbox_job` refuses to submit without
    one — so that path is tests, and it draws too.
    """
    token = runner_config()[1]
    invocation = str(ctx.tool_call_id) if ctx is not None and ctx.tool_call_id else ""
    if not invocation or not token:
        return _Trailer(nonce=secrets.token_hex(_META_NONCE_BYTES))
    derived = hmac.new(
        token.encode(), f"{_META_KEY}:{invocation}".encode(), hashlib.sha256
    ).hexdigest()
    return _Trailer(nonce=derived[: _META_NONCE_BYTES * 2])


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

# On every git invocation Jhin makes inside a job, credentialed or not.
# ``_credential_args`` already carries it; a durable workspace is why the
# uncredentialed calls need it too. A hook planted in one run used to die with
# the volume; now it would survive into the next, so the checkout removes
# ``.git/hooks`` outright *and* every Jhin git command refuses to look there.
_HOOKLESS = "-c core.hooksPath=/nonexistent"

# The audit trail entry the checkout writes and the push reads back: Jhin's
# own account of what was cloned, in a table no sandbox job can reach.
#
# Keyed on the *workspace binding* rather than the run, because that is what it
# describes. A durable workspace outlives the run that checked something out
# into it, so a record keyed on the run would leave tomorrow's chat turn
# looking at a ready checkout while the push refuses with ``no_checkout_record``
# for a record that belonged to yesterday. Keying the record to the disk keeps
# every existing check intact -- the lookup is still scoped by workspace, the
# repository must still match, and the config sha still has to be byte-for-byte
# what the checkout left behind -- while widening the record's reach from one
# run to one agent's disk, which is precisely the reach of the disk itself.
# A contended run has its own private binding and therefore its own record.
# Public because the workspace allow-list validator reads the same rows; the
# name itself lives with the rest of a disk's audit vocabulary in
# ``cli/workspace.py``, which is what reads the history back.
CHECKOUT_RECORD_ACTION = AUDIT_CHECKOUT_RECORDED
# Shape of a ref name Jhin will interpolate into a script, applied to the
# recorded base even though Jhin wrote the record: the value originated as a
# line of container stdout, so it is re-checked rather than trusted twice.
_REF_NAME = re.compile(r"^[\w./-]{1,200}$")
# The other two recorded values, checked the same way and for the same reason.
# A commit id is sha1 or sha256 depending on the repository's object format.
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_CONFIG_SHA = re.compile(r"^[0-9a-f]{64}$")

#: Where a checkout's working branch can have started, and the whole set of it.
#: ``base`` is a branch that did not exist before this run; the other two are a
#: run continuing work that does — published on the remote, or held only on
#: this disk. See :func:`_branch_selection`.
_BRANCH_STARTING_POINTS = frozenset({"base", "remote_branch", "workspace_branch"})

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
#: Exit codes Jhin's own scripts reserve for a refusal. 70 is the push's, and
#: it is the only one a script writes *after* touching the network: it is
#: emitted only where the remote has been asked what it holds and answered
#: that this branch is not at the sha this workspace has, which is proof and
#: not an assumption. git itself exits 1, 128 or 129 and never these.
_REFUSAL_EXIT_CODES = frozenset({65, 66, 67, 68, 69, 70})

# Sandbox exit codes carrying a Jhin-authored refusal, mapped by the JHIN_ERR
# line on stderr. Every one of these happens *before* anything leaves the
# sandbox, so the tool failure is proven side-effect free.
_REFUSAL_HINTS: dict[str, str] = {
    "binary_file": (
        "cli.file.read reads UTF-8 text/code only. Use Python document tools in a terminal "
        "for binary files: python-docx for DOCX, openpyxl for XLSX, python-pptx for PPTX, "
        "or pypdf for PDF. Save revisions to the original output path and call cli.file.publish."
    ),
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
    "write_staging_failed": (
        "The write could not be staged next to the file, so nothing was "
        "written. The workspace may be out of space."
    ),
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
    "redispatch_unprovable": (
        "This call was started once before and the sandbox runner has restarted "
        "since, so nothing here can say what that attempt did. It was not run "
        "again. Read the file or the checkout to see what it now holds."
    ),
    "redispatch_uncheckable": (
        "Nothing was run. Whether an earlier attempt at this same call already ran "
        "could not be checked, and running it again could repeat what that attempt "
        "did, so it was refused. Read the file or the checkout to see what it now "
        "holds before repeating this."
    ),
    "edit_changes_nothing": (
        "new_string is identical to old_string, so this edit would change "
        "nothing. Pass the text you want the file to hold instead."
    ),
    "no_checkout": "Run cli.repository.checkout first.",
    "checkout_unrecordable": (
        "The checkout ran, but Jhin could not read back its own account of what "
        "it produced, so nothing was recorded and a push would have had nothing "
        "to compare against. Check the repository out again."
    ),
    "no_checkout_record": (
        "Jhin has no record of checking this repository out into this sandbox "
        "workspace, so there is no trusted account of what it holds and nothing "
        "is pushed. Run cli.repository.checkout for this repository first."
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
    "repository_empty": (
        "That repository has no commits yet, so there is nothing to check out. "
        "Push an initial commit to it first."
    ),
    "push_rejected": (
        "The remote refused this push and the branch is not there, so nothing "
        "was published — read the detail for what the remote said. A "
        "permission error is for an operator to fix on the remote; anything "
        "else may be worth another attempt."
    ),
    # Raised before a container starts rather than by one, so these carry no
    # exit code -- but they belong in the same table, because the model reads
    # one list of named refusals and should not have to know which layer
    # produced which.
    **WORKSPACE_REFUSAL_HINTS,
}


class CliToolError(ToolExecutionError):
    """A sandbox tool failure that has *finished*, with its evidence attached.

    A ``ToolExecutionError`` rather than a bare ``Exception``, and that is the
    whole point of the class. The gateway reads the difference: a bare
    exception out of an executor cannot be shown to have failed before an
    external effect, so a durably claimed call becomes ``execution_unknown``
    and the run stops for manual reconciliation. That is the right answer for
    a mutation nobody can vouch for and a ruinous one for a checkout of a
    branch that does not exist, a test suite that failed, or a runner that
    blinked — each of which used to destroy the run it happened in.

    So every raise site says which it is, in a comment, and every one carries
    ``detail``: the exit code and stderr tail the agent needs in order to fix
    the thing and try again.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        side_effect_possible: bool,
        detail: str = "",
        hint: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            side_effect_possible=side_effect_possible,
            hint=hint or _REFUSAL_HINTS.get(code, ""),
            detail=detail,
        )


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
        # Proven side-effect free: this is a configuration check, reached
        # before a credential is minted and before any container exists.
        raise CliToolError(
            "no GitHub connection configured: set git_connection_id on the CLI connection",
            code="git_connection_missing",
            side_effect_possible=False,
            hint=(
                "This CLI connection has no GitHub connection attached, so it cannot do "
                "repository work. An operator has to set one on the connection."
            ),
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


async def _binding(ctx: ToolExecutionContext, *, enforce_size: bool = True) -> WorkspaceBinding:
    """Which disk this run's jobs run on (``cli/workspace.py``).

    Called on every ``cli.*`` job. The first call of a run decides; every later
    one renews the same lease and gets the same key back, so a run can never
    straddle two workspaces. The lease is committed in its own transaction,
    which is why it survives a tool call that later rolls back.

    ``enforce_size=False`` on the push, and nowhere else: a workspace over its
    cap refuses every call *except* the one that gets the branch out of it,
    which is the action ``workspace_full``'s own hint asks for.
    """
    binding = await bind_workspace(
        ctx.session_factory,
        workspace_id=ctx.workspace_id,
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
        enforce_size=enforce_size,
    )
    if enforce_size:
        from jhin_connectors.cli.chat_snapshots import prepare_chat_disk

        await prepare_chat_disk(ctx, binding)
        await stage_chat_inputs(ctx, binding.key)
    return binding


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
    return sanitized_output_tail(value, max_chars=_MAX_TAIL_CHARS)


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


def _raise_for_failure(row: SandboxJob, *, what: str, code: str) -> None:
    """Turn a non-zero Jhin-authored job into either a named refusal or an
    ordinary tool error.

    ``side_effect_possible=False`` for every caller, and the reason is the same
    one each time rather than a judgement per tool: the job is *finished* — its
    status and exit code are known — and none of the tools that end here can
    change anything outside the sandbox. The file tools have no network at all;
    the checkout's only remote traffic is ``ls-remote``, ``fetch`` and
    ``clone``, which are reads. What they can change is the agent's own
    workspace, which the agent can look at, and which is why the failure is
    worth reporting rather than worth abandoning the run over.
    """
    if row.status == SandboxJobStatus.COMPLETED.value and row.exit_code == 0:
        return
    refusal = _refusal_code(row)
    if refusal in _REFUSAL_HINTS:
        raise _refusal(refusal)
    raise CliToolError(
        f"{what} failed ({row.status}, exit {row.exit_code})",
        code=code,
        side_effect_possible=False,
        detail=_failure_detail(row),
    )


def _failure_detail(row: SandboxJob) -> str:
    """What a finished job has to say for itself, for the agent and the row.

    Already worker-side redacted and size-capped by :func:`_tail` before it
    reached the row; bounded again at the error boundary. It is the container's
    words, not Jhin's, so it travels as ``detail`` rather than as ``hint``.
    """
    parts = [f"status={row.status}", f"exit_code={row.exit_code}"]
    stderr = (row.stderr_tail or "").strip()
    stdout = (row.stdout_tail or "").strip()
    if stderr:
        parts.append(f"stderr: {stderr}")
    elif stdout:
        parts.append(f"stdout: {stdout}")
    return "\n".join(parts)


@dataclass(frozen=True)
class _OwnConnection:
    """Established: the row and its audit events are committed as they
    happen, on a connection that is not the tool call's.

    The connection is carried here rather than re-read from the context at
    every write, because the home and the connection it names are one fact.
    An evidence object that says "committed on its own connection" must not
    be able to discover later that it has nothing to commit on.
    """

    sessions: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class _CallersTransaction:
    """Established: the row lives in the transaction the tool call runs in,
    and ``basis`` is what makes that sound for *this* job.

    One basis reaches here, and it is a proof rather than a guess: the executor
    was given no isolated session factory — a unit test, a caller with no
    isolated sessions. Then :func:`_dispatch_history` reads through this same
    transaction too, so the read and the write agree about what exists, and
    nothing can find a row this transaction lost.

    There used to be a second one, and it was wrong twice over. It read a
    constraint refusal as proof that the ``tool_call`` this row references is
    uncommitted — but ``sandbox_job`` has *four* foreign keys, not the three
    that argument counted (``workspace_id`` is one, and it is ``NOT NULL``),
    plus a primary key and its own not-null columns, so an ``IntegrityError``
    names none of them in particular. And the branch was dead in production for
    a different reason than the one it gave: the gateway commits the
    ``tool_call`` row — ``claimed``, then the compare-and-set to ``executing``
    — *before* an executor is entered, and the workspace, run and task rows are
    older still, so every row this insert references is committed on any
    connection by the time it is built. Nothing was being protected, and what
    the branch actually did was move the record of a dispatch into a
    transaction :func:`_dispatch_history` reads past.

    So "the insert failed" has one answer now, whatever the failure was:
    :class:`_UnrecordableDispatch`, and it refuses.
    """

    basis: str


#: Where a job's evidence is written. Two homes and no third: the state in
#: which that could not be established is not a home at all — it is
#: :class:`_UnrecordableDispatch`, raised rather than represented, so that no
#: caller can go on holding an evidence object whose record does not exist.
_EvidenceHome = _OwnConnection | _CallersTransaction


class _UnrecordableDispatch(Exception):
    """Not established: this worker could not write down that it is about to
    dispatch a job, and cannot prove where the record went.

    The predecessor of this type was a ``self._durable = False`` set inside a
    bare ``except``, and its readers only ever consulted it to choose *where*
    to write the next audit row — so a transient insert failure quietly
    demoted the row into the caller's transaction and the dispatch went ahead.
    A later rollback of that transaction then erased the only record that a
    container had been started, which is precisely the state
    :func:`_dispatch_history` exists to make impossible: the next dispatch of
    that call reads no earlier job, tells the runner there was none, and a
    restarted runner starts a second container.

    So this is an exception and not a flag. There is no value a caller can
    read and shrug at, and the only way past the write is to have completed
    it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _job_event(
    ctx: ToolExecutionContext,
    row: SandboxJob,
    *,
    network: str,
    shared: Mapping[str, Any],
    action: str,
    metadata: Mapping[str, Any],
) -> AuditEvent:
    """One audit row about one sandbox job.

    A free function because the first of these has to be built before a
    :class:`_JobEvidence` exists — the evidence object is only created once
    its record has a home, and the record is what this describes.
    """
    return AuditEvent(
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


async def _open_job_record(
    ctx: ToolExecutionContext,
    row: SandboxJob,
    *,
    network: str,
    shared: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> _EvidenceHome:
    """Write the row for a job that is about to start, and answer where it
    went — or refuse, having written nothing.

    The order matters and is the same order the interlock is read in: the row
    is committed *before* the job is submitted, so a dispatch that happens is
    a dispatch a later re-dispatch can find. A write that cannot be completed
    therefore stops the dispatch rather than downgrading it, because the
    alternative is a container that ran with nothing on record saying so.
    """
    factory = ctx.session_factory
    if factory is not None:
        try:
            async with factory() as session:
                session.add(
                    SandboxJob(
                        **{
                            column.key: getattr(row, column.key)
                            for column in sa_inspect(SandboxJob).mapper.column_attrs
                            if getattr(row, column.key) is not None
                        }
                    )
                )
                session.add(
                    _job_event(
                        ctx,
                        row,
                        network=network,
                        shared=shared,
                        action="sandbox.job.started",
                        metadata=metadata,
                    )
                )
                await session.commit()
        except Exception as exc:
            # Every failure, including the one the database understood. A
            # constraint refusal proves the row was not written and nothing
            # else: ``sandbox_job`` carries four foreign keys, a primary key
            # and its own not-null columns, and an ``IntegrityError`` says
            # which of them only in a message. The row cannot be demoted into
            # the caller's transaction on the strength of that guess, because
            # :func:`_dispatch_history` will read past that transaction on
            # this very factory — and a dropped connection, a timeout or a
            # deadlock proves even less. See :class:`_CallersTransaction` for
            # the argument this replaced.
            raise _UnrecordableDispatch(
                redact_text(f"{type(exc).__name__}: {exc}")[:_MAX_REASON_CHARS]
            ) from exc
        return _OwnConnection(sessions=factory)
    basis = (
        "this executor was given no isolated session factory, so the dispatch-history "
        "read runs on this same transaction and cannot find a row it lost"
    )
    ctx.session.add(row)
    ctx.session.add(
        _job_event(
            ctx,
            row,
            network=network,
            shared=shared,
            action="sandbox.job.started",
            metadata=metadata,
        )
    )
    await ctx.session.flush()
    return _CallersTransaction(basis=basis)


class _JobEvidence:
    """Where a ``sandbox_job`` row and its audit events are written.

    On their own connection, committed as they happen, whenever the executor
    has a session factory — which in the tool worker it always does.

    They used to be written into the gateway's transaction, and that made the
    record of a job conditional on the tool call *succeeding*. A push that
    came back with exit 128 raised, the gateway rolled the transaction back to
    persist the outcome, and the row describing the container went with it: no
    exit code, no stderr, no branch, no evidence at all behind a call whose
    stored outcome was the single word "unknown". The one job an operator most
    needs to read is the one that failed, so the record of a job that ran is
    not the tool call's to keep or to discard.

    **An instance of this class is proof that the record exists.** It is built
    only by :meth:`opened`, which either establishes one of the two
    :data:`_EvidenceHome` cases or raises — so there is no way to be holding
    evidence for a job whose opening was never written down, and no flag on
    it that a reader can decline to act on. Only where the record lives is
    still a question, and it is a question with an answer attached to it.

    Past the opening, nothing here may fail a tool call: a job that ran is a
    fact whether or not Jhin manages to keep writing it down, and a lost
    terminal update is a worse record rather than a failed call.
    """

    def __init__(
        self,
        ctx: ToolExecutionContext,
        row: SandboxJob,
        *,
        network: str,
        shared: dict[str, Any],
        home: _EvidenceHome,
    ) -> None:
        self._ctx = ctx
        self._row = row
        self._network = network
        self._shared = shared
        self._home = home
        self._last_progress: tuple[str, ...] = ("", "")

    @classmethod
    async def opened(
        cls,
        ctx: ToolExecutionContext,
        row: SandboxJob,
        *,
        network: str,
        shared: dict[str, Any],
        metadata: dict[str, Any],
    ) -> _JobEvidence:
        """Record that a job is about to start, and hand back the evidence.

        Raises :class:`_UnrecordableDispatch` if that record cannot be
        established, in which case nothing has been written and the caller
        must not dispatch.
        """
        return cls(
            ctx,
            row,
            network=network,
            shared=shared,
            home=await _open_job_record(
                ctx, row, network=network, shared=shared, metadata=metadata
            ),
        )

    def _event(self, action: str, metadata: dict[str, Any]) -> AuditEvent:
        return _job_event(
            self._ctx,
            self._row,
            network=self._network,
            shared=self._shared,
            action=action,
            metadata=metadata,
        )

    async def noted(self, action: str, metadata: dict[str, Any]) -> None:
        """Record something about a job without claiming it has ended.

        The audit trail's only entry that leaves the row alone, and it exists
        because there is now one thing worth saying about a job that is still
        running: that this worker stopped watching it. Closing the row to say
        that would be a statement about the container, which this process is
        in no position to make once it has let go of it.
        """
        if isinstance(self._home, _OwnConnection):
            try:
                async with self._home.sessions() as session:
                    session.add(self._event(action, metadata))
                    await session.commit()
            except Exception:
                return
            return
        with suppress(Exception):
            self._ctx.session.add(self._event(action, metadata))
            await self._ctx.session.flush()

    async def closed(self, action: str, metadata: dict[str, Any]) -> None:
        """Write what the job finished as. Never raises.

        The tails are coerced rather than passed through, and that is not
        defensive tidying. ``stdout_tail`` and ``stderr_tail`` are NOT NULL
        with a Python-side default, which applies on *insert*; the in-memory
        row this UPDATE reads still holds ``None`` for whichever tail the
        ending never set. Every failure ending sets one and not the other, so
        every one of them sent a NULL — the statement was refused, this
        ``except`` swallowed the refusal because a lost record must not fail a
        tool call, and the row stayed ``running`` for good. A job that did not
        finish is exactly the job whose row most needs closing.
        """
        if not isinstance(self._home, _OwnConnection):
            # The row is the object in the caller's session, and the endings
            # have already set its fields; there is nothing to UPDATE.
            self._ctx.session.add(self._event(action, metadata))
            return
        try:
            async with self._home.sessions() as session:
                await session.execute(
                    sa_update(SandboxJob)
                    .where(SandboxJob.id == self._row.id)
                    .values(
                        status=self._row.status,
                        exit_code=self._row.exit_code,
                        duration_ms=self._row.duration_ms,
                        completed_at=self._row.completed_at,
                        stdout_tail=self._row.stdout_tail or "",
                        stderr_tail=self._row.stderr_tail or "",
                        error_code=self._row.error_code,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.add(self._event(action, metadata))
                await session.commit()
        except Exception:
            # The started row is already durable and says the job was running;
            # losing the terminal update is a worse record, not a failed tool
            # call.
            return

    async def progress(self, snapshot: dict[str, Any]) -> None:
        """Replace safe output tails without changing the dispatch lifecycle.

        Separate-session writes survive the outer call's rollback. A stale
        poll cannot overwrite a terminal result written by reconciliation.
        """
        if snapshot.get("status") != "running":
            return
        tails = tuple(_tail(str(snapshot.get(stream) or "")) for stream in ("stdout", "stderr"))
        if tails == self._last_progress:
            return
        values = {"stdout_tail": tails[0], "stderr_tail": tails[1]}
        if isinstance(self._home, _OwnConnection):
            async with self._home.sessions() as session:
                changed = await session.scalar(
                    sa_update(SandboxJob)
                    .where(
                        SandboxJob.id == self._row.id,
                        SandboxJob.workspace_id == self._ctx.workspace_id,
                        SandboxJob.run_id == self._ctx.run_id,
                        SandboxJob.tool_call_id == self._ctx.tool_call_id,
                        SandboxJob.status == SandboxJobStatus.RUNNING.value,
                        SandboxJob.completed_at.is_(None),
                    )
                    .values(**values)
                    .returning(SandboxJob.id)
                )
                await session.commit()
                if changed is None:
                    return
        elif self._row.status != SandboxJobStatus.RUNNING.value:
            return
        self._row.stdout_tail, self._row.stderr_tail = tails
        self._last_progress = tails


@dataclass(frozen=True)
class _FirstDispatch:
    """Established: no earlier dispatch of this tool call started a job.

    ``basis`` is how that was established, and there are two ways. Either the
    indexed read came back empty, or the call carries no invocation identity
    at all — in which case the runner is offered none, keys no idempotency on
    it, and never reads the field this answer fills in. Both are answers; the
    distinction is kept because "the field is unread" and "the field is read
    and says no" are not the same statement about the world.
    """

    basis: str

    @property
    def prior_dispatch_at(self) -> str:
        return ""


@dataclass(frozen=True)
class _EarlierDispatch:
    """Established: a dispatch of this tool call started a job at ``at``."""

    at: datetime

    @property
    def prior_dispatch_at(self) -> str:
        return self.at.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class _UnknownDispatchHistory:
    """Not established, either way: the question could not be answered.

    **This type deliberately has no ``prior_dispatch_at``.** There is no value
    of that field which says "I do not know" — the empty string says "there
    was no earlier dispatch", which is the answer that makes the runner run
    the job — so the way to stop a future caller putting this on the wire is
    to leave it with nothing to put there. A caller that reaches for the field
    without handling this case does not compile.
    """

    reason: str


#: What :func:`_dispatch_history` answers with. Three cases and no sentinel:
#: the one that cannot be answered is a different type from the one answered
#: "no", because they demand opposite behaviour and a shared representation is
#: how they came to be treated alike.
_DispatchHistory = _FirstDispatch | _EarlierDispatch | _UnknownDispatchHistory


async def _dispatch_history(ctx: ToolExecutionContext) -> _DispatchHistory:
    """Whether an earlier dispatch of this tool call already started a sandbox
    job, and when.

    One indexed read of ``sandbox_job`` on ``tool_call_id``, and the runner
    needs it for one decision only: an invocation it has no record of is
    either brand new or one it forgot — when it restarted, or when it dropped
    that stretch of its memory — and those must not be treated alike. A
    dispatch stamped *before* the point from which the runner remembers
    everything is one it cannot vouch for; a dispatch stamped after is one it
    would be holding if it had ever received it, so its absence proves the job
    was never submitted and nothing ran.

    **This is an interlock, and it fails closed.** It used to answer "no
    earlier dispatch" for every failure, on the reasoning that the real
    interlock was the runner's ledger and this was only an optimisation of
    honesty. That reasoning has the direction of the dependency backwards.
    Inside one runner incarnation the ledger is exact and this read is indeed
    redundant; *across a restart* the ledger is empty by construction, and
    then this is the only interlock there is. The moment it matters is exactly
    the moment it was answering from ignorance: a caller that says "no earlier
    dispatch" when it cannot tell is a caller asking for the effect to be
    applied a second time.

    So a lookup that fails answers :class:`_UnknownDispatchHistory`, and the
    caller refuses to dispatch. The trade is deliberate and it is not close: a
    refusal costs a tool call that ran nothing and said why, and it costs it
    only while the database is unreachable — which is a state in which nothing
    else is working either. A wrong "no" costs an edit, a checkout or a push
    applied twice, silently, and there is nothing downstream that can catch
    it.

    Never raises, and never swallows either. The reason a lookup failed
    travels in the answer, out through the refusal's ``detail``, and into the
    ``tool_call`` the gateway records — which is where a person looks. It is
    redacted and capped on the way, because a database error can carry a
    connection string.

    What makes the *answered* cases trustworthy is that a dispatch's row is
    durable exactly when that dispatch can be repeated — and that this is
    enforced rather than hoped for. :meth:`_JobEvidence.opened` writes the row
    on its own connection and commits it before the job is submitted, and the
    fallback into the caller's transaction — the one a later rollback could
    erase — is reachable exactly two ways, neither of which can hide a
    dispatch from this read. Either there is no isolated connection at all,
    and then this read runs on that same transaction and agrees with it; or
    the database refused the insert *on a constraint*, which for that row
    means the ``tool_call`` it points at is not committed, and a call the
    gateway never claimed is a call nothing re-dispatches. Any other failure
    of that insert refuses the dispatch outright
    (:class:`_UnrecordableDispatch`), because a row lost to a dropped
    connection is precisely a container this read would later fail to see.
    """
    if ctx.tool_call_id is None:
        return _FirstDispatch(
            basis=(
                "this call carries no invocation identity, so the runner is offered none "
                "and no dispatch of it can be recognised as a repeat"
            )
        )
    factory = ctx.session_factory
    statement = select(func.min(SandboxJob.started_at)).where(
        SandboxJob.workspace_id == ctx.workspace_id,
        SandboxJob.tool_call_id == ctx.tool_call_id,
    )
    try:
        if factory is not None:
            async with factory() as session:
                earliest = await session.scalar(statement)
        else:
            earliest = await ctx.session.scalar(statement)
    except Exception as exc:
        return _UnknownDispatchHistory(
            reason=redact_text(f"{type(exc).__name__}: {exc}")[:_MAX_REASON_CHARS]
        )
    if earliest is None:
        return _FirstDispatch(basis="no sandbox job of this tool call is on record")
    return _EarlierDispatch(
        at=earliest if earliest.tzinfo is not None else earliest.replace(tzinfo=UTC)
    )


#: How long the cancellation path may spend recording that it walked away
#: from a running job. It runs inside a shutdown somebody else is timing —
#: Docker sends SIGKILL ten seconds after SIGTERM by default — so it is
#: deliberately small: one audit row is worth a second or two and nothing
#: more, and the sweep closes the job's row whether or not this was written.
#:
#: It is the middle of three consecutive budgets that share that grace, and
#: ``jhin_tool_worker.drain`` owns the arithmetic (the worker's drain, this,
#: and the telemetry flush, summing to eight with two seconds spare). The
#: constant lives here rather than there because a package may not import a
#: service; the tool worker's own tests assert the three still add up. Four
#: seconds was one third of that sum and left the total at exactly ten, which
#: is not a budget — it is a coincidence with SIGKILL on the other side of it.
_CANCEL_CLEANUP_SECONDS = 2.0


async def _note_abandoned(evidence: _JobEvidence, job_id: str) -> None:
    """Record that this worker walked away from a running job, while being
    cancelled.

    The write is shielded, because the caller is already inside a
    ``CancelledError`` and an unshielded await would be cancelled on its
    first suspension — which is precisely how this record came to be absent
    rather than merely slow. Shielded work still has to end, so it is
    bounded; a note that cannot be written in its budget is only a note, and
    the sweep closes the row regardless.
    """
    cleanup = asyncio.ensure_future(_write_abandoned(evidence, job_id))
    cleanup.add_done_callback(_swallow)
    # Suppressing everything, including a second cancellation: the caller
    # re-raises the first one, which is the one that matters, and nothing in
    # this cleanup may replace it.
    with suppress(BaseException):
        await asyncio.wait({cleanup}, timeout=_CANCEL_CLEANUP_SECONDS)
    if not cleanup.done():
        cleanup.cancel()


def _swallow(finished: asyncio.Future[None]) -> None:
    """Retrieve a cleanup task's outcome so asyncio never logs it as unheard."""
    if not finished.cancelled():
        finished.exception()


async def _write_abandoned(evidence: _JobEvidence, job_id: str) -> None:
    await evidence.noted(
        "sandbox.job.abandoned",
        {
            "reason": "worker_shutdown",
            "evidence": (
                "the worker running this job was shut down before it finished; the "
                "container was left running and the row is closed by the sandbox "
                "sweep from the runner's own account of it"
            ),
        },
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
    binding: WorkspaceBinding | None = None,
    external_effect: bool = False,
    workspace_effect: bool = True,
    publish_output: bool = False,
) -> tuple[SandboxJob, dict[str, Any]]:
    """Submit one sandbox job, poll it to a terminal state, and persist the
    ``sandbox_job`` row + audit trail (plan 14, 23).

    ``binding`` is passed in by the two tools that need to know which disk they
    are on *before* they can build their script (checkout, which reuses a clone,
    and push, which reads the record keyed on it); every other tool lets this
    bind. Either way the bind happens before the ``sandbox_job`` row is written,
    so a refusal to bind is a refusal with no job behind it.

    ``external_effect`` says whether a job of this shape can change anything
    outside the sandbox, and it is read for exactly one decision: what a
    *runner* failure means. A runner that stops answering mid-job leaves no
    account of the container, so for a job that could have reached the world
    (a push; a command with egress) the outcome is genuinely unknown, while for
    a job that could not (no network, or read-only traffic) it is a finished
    failure the agent may read and retry.

    ``workspace_effect`` is the same kind of statement one wall in: whether a
    job of this shape can change the sandbox's own disk. It is read for the
    two endings where that is the question, and they are the same question
    asked at two removes — a re-dispatch the runner will not repeat because it
    has no record of the first attempt, and one this worker will not submit
    because it cannot establish whether there *was* a first attempt. For a
    listing or a read, an unaccounted-for first attempt changed nothing and
    this one is a clean failure the agent can simply make again; for an edit,
    a write, a checkout or a test command, it is exactly the unknown that must
    reach a person. It defaults to True, so a tool says nothing only by being
    one that writes.

    **Every job submitted from here is idempotent on its invocation.** The
    payload carries ``invocation_id`` — the tool call this job belongs to,
    which recovery keeps stable across a re-dispatch — and the runner answers
    a second dispatch of one invocation with the first dispatch's job rather
    than a second container. So the question "did my earlier dispatch already
    run this?" is settled by the process that watched both of them, once, for
    every sandbox tool. No tool answers it by looking at what is in the
    workspace afterwards, and none should: the contents of a file are the
    *effect* of a job, and no amount of reading them can distinguish "my
    earlier dispatch did this" from "somebody else did" or from "this is what
    the file always said". Only the runner holds the fact.

    **Except across a restart, where this end of the wire holds it.** A runner
    that has restarted has an empty ledger, so the only thing that can tell it
    "you had this call before" is ``prior_dispatch_at``, computed here from
    Jhin's own ``sandbox_job`` rows (:func:`_dispatch_history`). That makes
    this an interlock and not a hint, and it is why the one case it cannot
    answer refuses instead of dispatching: a claim of "no earlier dispatch"
    made from ignorance is the one input that turns the runner's careful
    refusal into a second container.
    """
    bound = binding if binding is not None else await _binding(ctx)
    # Read before this job's own row exists, so what comes back is only ever
    # an *earlier* dispatch of the same tool call — and before the row is
    # written at all, because a dispatch that may not happen must not leave a
    # record saying a job started.
    history = await _dispatch_history(ctx)
    if isinstance(history, _UnknownDispatchHistory):
        # Nothing was submitted, and nothing will be: this worker cannot tell
        # whether an earlier dispatch of this call already ran, and the runner
        # cannot tell it — that is the whole reason this read exists. The
        # honest ending is the same one the runner's own refusal produces, and
        # for the same reason: what is unknown is not this attempt, which
        # certainly did nothing, but the earlier one, which may have written.
        #
        # No ``sandbox_job`` row and no audit event. There is no job to
        # describe, and the connection that would record one is the connection
        # that just failed; the reason travels with the error instead.
        raise CliToolError(
            f"sandbox job not started: {history.reason}",
            code="redispatch_uncheckable",
            side_effect_possible=workspace_effect,
            detail=(
                "this worker could not establish whether an earlier dispatch of this "
                f"tool call already started a sandbox job: {history.reason}"
            ),
        )
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
    try:
        evidence = await _JobEvidence.opened(
            ctx,
            row,
            network=network,
            shared=dict(audit_metadata or {}),
            metadata={"timeout_seconds": timeout_seconds},
        )
    except _UnrecordableDispatch as exc:
        # Nothing was submitted. The row is the thing a *later* dispatch of
        # this call reads to learn that this one happened, so dispatching
        # without it is how the same edit gets applied twice: the re-dispatch
        # finds no earlier job, tells the runner there was none, and a runner
        # that has restarted in the meantime starts a second container.
        #
        # Unlike the two refusals it sits between, what an earlier dispatch
        # did is *not* an open question here — the history read succeeded, and
        # it says. A first dispatch that never left is a clean failure the
        # agent can simply make again; only a re-dispatch leaves a container
        # nobody can account for behind it.
        earlier_may_have_written = workspace_effect and isinstance(history, _EarlierDispatch)
        raise CliToolError(
            f"sandbox job not started: {exc.reason}",
            code="redispatch_uncheckable",
            side_effect_possible=earlier_may_have_written,
            detail=(
                "this worker could not durably record that it was about to start a "
                f"sandbox job, so it started none: {exc.reason}"
            ),
        ) from exc

    payload: dict[str, Any] = {
        "job_id": str(row.id),
        # What makes a re-dispatch recognisable as the same call. The tool
        # call id is stable across re-dispatch — recovery reopens the same
        # ``tool_call`` row rather than making a new one — and distinct for
        # every call an agent makes, which is exactly the identity the runner
        # needs in order to hand a second dispatch the first one's job instead
        # of starting a second container.
        "invocation_id": str(ctx.tool_call_id) if ctx.tool_call_id else "",
        # And when the first of those dispatches began, which is the only
        # thing that tells a runner with no memory of this invocation whether
        # it is looking at a new call or at one of its own it has forgotten.
        # Empty here is a claim that there was none, never a shrug: the case
        # where that could not be established never reaches this line.
        "prior_dispatch_at": history.prior_dispatch_at,
        "image": image,
        "command": argv,
        "workspace_key": bound.key,
        "working_dir": _WORKSPACE_PATH,
        "env": env or {},
        "secret_env": secret_env or {},
        "network_policy": network,
        "timeout_seconds": timeout_seconds,
    }
    try:
        with sandbox_job_progress(evidence.progress if publish_output else None):
            result = await run_sandbox_job(payload, job_timeout_seconds=timeout_seconds)
    except SandboxInvocationUnknownError as exc:
        # The runner would not repeat a dispatch it cannot account for, and
        # started nothing. That is a *finished* failure of this attempt with
        # an unfinished question behind it: the earlier attempt's container
        # was killed by the runner's own restart, at an unknown point.
        #
        # ``side_effect_possible`` is not about this job — nothing ran — but
        # about the earlier one, which is why it is ``workspace_effect`` and
        # not the tool's ordinary classification. A read whose first attempt
        # is unaccounted for changed nothing, so the agent may simply ask
        # again; a write whose first attempt is unaccounted for is the unknown
        # that has to reach a person, and the gateway stops for one.
        row.status = SandboxJobStatus.FAILED.value
        row.completed_at = datetime.now(UTC)
        row.error_code = "redispatch_unprovable"
        row.stderr_tail = _tail(str(exc))
        await evidence.closed(
            "sandbox.job.failed",
            {"status": SandboxJobStatus.FAILED.value, "error": str(exc)[:300]},
        )
        raise CliToolError(
            f"sandbox job not started: {exc}",
            code="redispatch_unprovable",
            side_effect_possible=workspace_effect,
            detail=str(exc),
            hint=(
                "This call was started once before and the sandbox runner has "
                "restarted since, so what the first attempt did to the workspace "
                "cannot be established and it was not run again. Read the file or "
                "the checkout to see what it now holds before repeating this."
            ),
        ) from exc
    except (asyncio.CancelledError, GeneratorExit):
        # The worker is going away underneath a job that is still running:
        # a redeploy, a shutdown, an activity Temporal cancelled.
        #
        # It leaves the container alone, and that is a deliberate reversal.
        # Killing it looked like tidiness — close the row, stop the process,
        # leave nothing behind — but the container is the only thing that
        # still knows what this call did, and the re-dispatch that follows a
        # redeploy is going to ask the runner for exactly that. Let it finish
        # and the second dispatch is handed a real outcome; kill it and the
        # second dispatch is handed a workspace stopped halfway through a
        # write with nobody able to say where. Nothing outside the sandbox is
        # at stake either way: the container has the network policy it was
        # given, its own timeout, and a disk Jhin owns.
        #
        # The row is left in ``running`` for the same reason — because it is.
        # Writing ``cancelled`` over a container that is still going would be
        # the first false statement in this table, and the row does not need
        # this path to be closed: the sweep
        # (``jhin_tool_worker.sandbox_reconcile``) closes it from the runner's
        # own account of the job once it is overdue, with the real exit code
        # and the real output, and closes it as ``runner_gone`` if the runner
        # is gone too. What is written here is the fact this path actually
        # knows: that the worker walked away from it.
        await _note_abandoned(evidence, str(row.id))
        raise
    except SandboxRunnerError as exc:
        # Connection loss is not evidence that a container terminated. Keep
        # its lease fenced until the reconciler obtains the actual outcome.
        row.status = SandboxJobStatus.RUNNING.value
        row.completed_at = None
        row.error_code = "runner_error"
        row.stderr_tail = _tail(str(exc))
        await evidence.closed("sandbox.job.outcome_uncertain", {"error": str(exc)[:300]})
        raise CliToolError(
            f"sandbox job failed: {exc}",
            code="sandbox_runner_error",
            side_effect_possible=workspace_effect or external_effect,
            detail=f"the sandbox runner did not complete this job: {exc}",
            hint=(
                "The sandbox runner did not answer. Wait for its recorded outcome "
                "and inspect the workspace before repeating a command."
            ),
        ) from exc

    status = str(result.get("status", SandboxJobStatus.FAILED.value))
    # Which container this outcome came out of. Equal to this row's own id for
    # an ordinary dispatch; a *different* id when the runner recognised the
    # invocation and handed this dispatch the job an earlier one started. Both
    # rows then carry the same outcome, and this field is what says which of
    # them ran a container — without it the trail would show one tool call
    # having produced two identical jobs and no way to tell that only one of
    # them was real.
    ran_as = str(result.get("polled_job_id") or row.id)
    attached = {"attached_to_job_id": ran_as} if ran_as != str(row.id) else {}
    row.status = status
    row.exit_code = cast("int | None", result.get("exit_code"))
    row.duration_ms = cast("int | None", result.get("duration_ms"))
    row.completed_at = datetime.now(UTC)
    row.stdout_tail = _tail(str(result.get("stdout", "")))
    row.stderr_tail = _tail(str(result.get("stderr", "")))
    runner_error = str(result.get("error") or "")
    if not row.stderr_tail.strip() and runner_error:
        # A job that never got a container has nothing on either stream, and
        # everything it has to say is in the runner's own ``error`` — the
        # image was missing, or the workspace was still held by an earlier
        # dispatch's container. That used to be recorded nowhere the agent
        # reads, so the failure arrived as "status=failed exit_code=None" and
        # named nothing. It is the runner's words rather than a container's,
        # and it goes through the same redaction and cap as any other tail.
        row.stderr_tail = _tail(runner_error)
    # What the runner's init container counted on this workspace, and whether
    # its walk finished, stored on the binding in its own transaction. The
    # runner measures on every job, so the row is at most one job stale; a
    # walk that did not finish stores a floor and marks the size unknown,
    # which refuses this workspace's next call rather than enforcing the cap
    # against a number that is not the disk's. Storing it is best effort in
    # the other direction: a failure here must never fail the tool call that
    # just succeeded.
    await record_size(
        ctx.session_factory,
        row_id=bound.row_id,
        size_bytes=cast("int | None", result.get("workspace_size_bytes")),
        partial=bool(result.get("workspace_size_partial", False)),
    )
    if status != SandboxJobStatus.COMPLETED.value:
        row.error_code = status
        await evidence.closed(
            "sandbox.job.failed",
            {"status": status, "error": str(result.get("error") or "")[:300], **attached},
        )
    else:
        completion = dict(completion_metadata(result)) if completion_metadata else {}
        await evidence.closed(
            "sandbox.job.completed",
            {
                "exit_code": row.exit_code,
                "duration_ms": row.duration_ms,
                **attached,
                **completion,
            },
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


def _record_checkout(
    ctx: ToolExecutionContext, binding: WorkspaceBinding, metadata: Mapping[str, Any]
) -> None:
    """Write Jhin's own account of a completed checkout.

    ``cli.repository.push`` reads it back to answer two questions it must not
    ask the container: which ref this branch was cut from, and what the
    repository's git config looked like when Jhin was last the one writing it.
    The row lives in the append-only audit table, keyed on the workspace the
    checkout landed in, so no sandbox job can reach it and the operator sees
    the same facts the push checked.
    """
    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action=CHECKOUT_RECORD_ACTION,
            target_type=binding.record_target_type,
            target_id=binding.record_target_id,
            metadata_json={
                "run_id": str(ctx.run_id),
                "tool_call_id": str(ctx.tool_call_id) if ctx.tool_call_id else None,
                "workspace_key": binding.key,
                **metadata,
            },
        )
    )


async def _last_checkout(ctx: ToolExecutionContext, binding: WorkspaceBinding) -> Mapping[str, Any]:
    """The most recent checkout Jhin recorded against this disk, or ``{}``."""
    event = await ctx.session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.workspace_id == ctx.workspace_id,
            AuditEvent.action == CHECKOUT_RECORD_ACTION,
            AuditEvent.target_type == binding.record_target_type,
            AuditEvent.target_id == binding.record_target_id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    return (event.metadata_json or {}) if event is not None else {}


async def _reusable_config_sha(
    ctx: ToolExecutionContext, binding: WorkspaceBinding, repository: str
) -> str:
    """What ``.git/config`` must hash to for the clone on this disk to be
    treated as this repository's cache -- or ``""``, meaning clone fresh.

    The reuse test used to be ``remote.origin.url`` plus a name-only config
    audit, and neither says whose tree this is. ``git remote set-url origin``
    is one command: a clone of some other repository, pointed at the URL Jhin
    is about to ask for, passed both checks and was adopted as the requested
    repository's cache -- so the agent would have read, edited and tested
    somebody else's code believing it was looking at the repository it named.

    So the question is not "does this look right" but "is this the tree Jhin
    left here". The answer is Jhin's own record, in a table no sandbox job can
    reach: the last checkout on this disk must name this repository, and
    ``.git/config`` must still hash to the value that checkout wrote. That
    covers the remote URL and every other key at once, which is the same proof
    ``cli.repository.push`` already demands before it will push.
    """
    record = await _last_checkout(ctx, binding)
    if str(record.get("repository") or "") != repository:
        return ""
    recorded = str(record.get("config_sha") or "")
    return recorded if _CONFIG_SHA.match(recorded) else ""


async def _checkout_record(
    ctx: ToolExecutionContext, binding: WorkspaceBinding, repository: str
) -> Mapping[str, Any]:
    """The most recent checkout recorded against the disk this run is bound to,
    which must be the one on it. A push for any other repository — or with no
    record at all — is refused before a container starts."""
    metadata = await _last_checkout(ctx, binding)
    if not metadata:
        raise _refusal("no_checkout_record")
    if str(metadata.get("repository") or "") != repository:
        raise _refusal(
            "no_checkout_record",
            detail=(f"this workspace last checked out {metadata.get('repository') or 'nothing'}"),
        )
    return metadata


def _job_output_fields(row: SandboxJob, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_job_id": str(row.id),
        "status": row.status,
        "network_policy": row.network_policy,
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
        # The command is the model's, so what it can reach is decided by the
        # network policy and nothing else: with egress it may have posted
        # something before the runner stopped answering, and without egress
        # the only thing it can have touched is the agent's own workspace.
        external_effect=network == "internet",
        publish_output=True,
    )
    return CommandExecuteOutput(command=data.command, **_job_output_fields(row, result))


# --- cli.repository.checkout ---


def _slug(repository: str) -> str:
    name = repository.split("/", 1)[-1].lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")
    return cleaned[:40] or "repo"


def _default_branch(task_id: object, repository: str) -> str:
    """The working branch a checkout creates when the caller names none.

    It carries the whole task id, and that is the fix rather than the taste.
    The name used to be ``agent/{str(task_id)[:8]}-{repo}``, and eight hex
    characters of a uuid7 are the top 32 bits of its 48-bit millisecond
    timestamp — a prefix that advances once every 65.536 seconds and says
    nothing about *which* task. Two tasks on one repository started inside the
    same minute got byte-identical branch names, so the second one's checkout
    resumed the first one's branch and pushed its work onto it. Reproduced
    twice, and it is not a rare shape: a person asking one agent for two
    things, or a trigger fanning out, starts tasks a second apart.

    A hash would collide only improbably; the id collides never, and it is
    also the thing an operator pastes back into Jhin to find the task that
    made the branch. So the name is long and exact rather than short and
    nearly unique. The repository slug stays in front of it because one task
    may check out more than one repository, and each of those needs a branch
    of its own.
    """
    return f"agent/{_slug(repository)}-{task_id}"


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
        # An input check, made before a URL exists to send anywhere. Proven
        # side-effect free by construction.
        raise CliToolError(
            f"repository '{repository}' is not an owner/name pair",
            code="repository_invalid",
            side_effect_possible=False,
            hint="A repository is spelled owner/name, with no scheme, path or wildcard.",
        )
    return f"{git_base}/{repository}.git"


def _reuse_prologue(*, url: str, credential: str, config_sha: str, purge: bool = False) -> str:
    """Reuse the clone already on this disk, or throw it away and start again.

    A durable workspace turns "clone the repository" into "make the repository
    on this disk be the one asked for, at the ref asked for". The rules are
    deliberately blunt, because a cache is not worth a subtle failure:

    * **The tree has to be the one Jhin left, not one that looks like it.**
      ``.git/config`` must hash to exactly the value the last checkout on this
      disk recorded, in a table no sandbox job can reach, and that record must
      name this repository. A clone of something else with
      ``git remote set-url origin`` run on it satisfies every check about
      shape and none about identity, and the cost of getting that wrong is an
      agent reading, editing and testing another repository's code while
      believing it is looking at the one it asked for. The URL and the config
      key audit stay as well: they are cheap, and they name *why* a tree was
      discarded.
    * **Validate before trusting.** The clone is kept only if ``remote.origin.url``
      has exactly one value and it is the URL Jhin computes, and if
      ``git config --local`` holds nothing outside the keys a Jhin checkout
      produces. A different repository, a rewritten remote, a planted
      ``credential.*`` or ``url.*.insteadOf`` -- none of it is *repaired*. The
      tree is deleted and cloned fresh, and the reason travels to the audit
      trail. Discarding costs Jhin one clone and costs an attacker all of their
      state, which is the right way round.
    * **Hooks do not survive.** ``.git/hooks`` is emptied before any git command
      touches the tree, on top of ``core.hooksPath=/nonexistent`` everywhere.
    * **``git clean -ffd``, deliberately without ``-x``.** Untracked strays go;
      *ignored* files stay. That one missing flag is the whole promise: the
      ``.venv``, ``node_modules``, ``__pycache__`` and build output an agent
      paid minutes for are exactly what ``-x`` would delete. Caches outside the
      repository (``HOME`` is ``/workspace``, so pip, npm and cargo write to
      ``/workspace/.cache``) survive regardless.
    * **A refresh that fails is not a stuck agent.** Any failure in the reuse
      path -- a corrupt object store, a half-finished merge, a fetch that
      cannot reach the remote -- falls through to the fresh clone rather than
      aborting, so a bad cache can never wedge an agent permanently.

    What this leaves behind is a tree at the base ref (``jhin_basepoint``) and
    nothing decided about the working branch: that is
    :func:`_branch_selection`'s question, and it is a different one. This
    function is about *which repository is on this disk*; that one is about
    *which commit this run builds on*.

    ``purge`` is the one case where "throw the clone away" is not enough,
    because the thing being thrown away is not the clone. The connection's
    allow-list no longer covers something this disk has held, and a disk is
    not a path: ``rm -rf /workspace/repo`` leaves a copy made anywhere else,
    and it leaves ``/workspace/.cache``, where pip and npm have been writing a
    private repository's packages because ``HOME`` is ``/workspace``. So the
    whole workspace goes -- every entry under it, dotfiles included, the mount
    point itself untouched -- before anything is cloned, and the checkout
    records that it did. Nothing is reused on a purge, by construction: there
    is nothing left to reuse.
    """
    allowed = shlex.quote(_ALLOWED_REPO_CONFIG)
    # ``find -mindepth 1 -delete`` rather than a glob: it needs no shell
    # expansion to see dotfiles, it cannot be defeated by an entry whose name
    # starts with a dash, and it empties the directory without removing the
    # volume's mount point. ``-xdev`` keeps it on the workspace's own
    # filesystem. A failure here is fatal to the job -- the caller runs under
    # ``set -e`` -- because a partial purge that reported success would clear
    # the disk's history while leaving the files it is about.
    purge_prologue = (
        (f"cd {_WORKSPACE_PATH}\nfind {_WORKSPACE_PATH} -xdev -mindepth 1 -delete\njhin_purged=1\n")
        if purge
        else ""
    )
    return (
        "jhin_reused=0\n"
        "jhin_reclone=\n"
        "jhin_discarded=\n"
        "jhin_purged=0\n" + purge_prologue + f"if [ -d {_REPO_PATH}/.git ]; then\n"
        f"  cd {_REPO_PATH}\n"
        "  jhin_count=$(git config --local --get-all remote.origin.url 2>/dev/null "
        "| wc -l | tr -d ' ')\n"
        "  jhin_origin=$(git config --local --get-all remote.origin.url 2>/dev/null "
        "| head -n 1)\n"
        "  jhin_bad=$(git config --local --list --name-only 2>/dev/null "
        f"| grep -v -E {allowed} || true)\n"
        "  jhin_cfg=$(sha256sum -- .git/config 2>/dev/null | cut -c1-64)\n"
        '  if [ "$jhin_count" != "1" ]; then jhin_reclone=remote_rewritten\n'
        f'  elif [ "$jhin_origin" != {url} ]; then jhin_reclone=different_repository\n'
        '  elif [ -n "$jhin_bad" ]; then jhin_reclone=config_unexpected\n'
        # An empty expected sha is the "no record for this repository on this
        # disk" case, and it compares unequal to every real hash, so a tree
        # Jhin has no account of is cloned fresh rather than adopted.
        f'  elif [ "$jhin_cfg" != {shlex.quote(config_sha)} ]; '
        "then jhin_reclone=config_unrecorded\n"
        "  else jhin_reused=1\n"
        "  fi\n"
        f"  cd {_WORKSPACE_PATH}\n"
        "fi\n"
        # ``reset --hard`` with no argument, which resets to HEAD: it drops
        # whatever the last run left uncommitted in the tree and keeps the
        # commits that run made. Rewinding the branch itself is not this
        # function's business -- see :func:`_branch_selection`.
        "jhin_refresh() {\n"
        f"  cd {_REPO_PATH} || return 1\n"
        "  rm -rf .git/hooks || return 1\n"
        "  mkdir -p .git/hooks || return 1\n"
        f"  git {_HOOKLESS} reset --hard || return 1\n"
        f"  git {_HOOKLESS} clean -ffd || return 1\n"
        f'  git {credential} fetch --prune origin "$jhin_base" || return 1\n'
        "  jhin_basepoint=$(git rev-parse FETCH_HEAD) || return 1\n"
        "  return 0\n"
        "}\n"
        'if [ "$jhin_reused" = "1" ] && ! jhin_refresh; then\n'
        "  jhin_reused=0\n"
        "  jhin_reclone=refresh_failed\n"
        "fi\n"
        'if [ "$jhin_reused" != "1" ]; then\n'
        f"  cd {_WORKSPACE_PATH}\n"
        # What is about to be deleted, when there is a head to read. A tree
        # discarded for being another repository, or for a rewritten config,
        # used to go without a word; the head is the only handle anybody would
        # have on work that was on it.
        f"  jhin_discarded=$(git -C {_REPO_PATH} rev-parse HEAD 2>/dev/null || true)\n"
        f"  rm -rf {_REPO_PATH}\n"
        f'  git {credential} clone --branch "$jhin_base" {url} {_REPO_PATH}\n'
        f"  cd {_REPO_PATH}\n"
        "  jhin_basepoint=$(git rev-parse HEAD)\n"
        "fi\n"
    )


def _branch_selection(credential: str) -> str:
    """Put the working branch on the commit this run should build on.

    **A checkout onto a branch that already exists continues it.** The base ref
    decides where a *new* branch starts and nothing else, and that is the whole
    of the rule.

    It replaces one that could not survive a second run. The refresh used to
    end in ``git checkout -B <branch> FETCH_HEAD``, which force-moves the
    working branch onto the base — so a second run on a reused workspace began
    by rewinding past the commit the first run had already pushed, built on
    the base again, and had its push rejected as a non-fast-forward. The run's
    work then sat in the sandbox with nothing published, which is the exact
    opposite of what a durable per-agent workspace is for: a second turn is
    supposed to be able to build on the first.

    So the starting point is chosen from what exists, in this order:

    * **the branch as the remote holds it**, whenever ``refs/heads/<branch>``
      is there. That is this branch's published truth — the commit an earlier
      run pushed, or one a person pushed on top of it — and a push from
      anywhere else is a non-fast-forward. Deliberately independent of whether
      the workspace was reused: an evicted, purged or re-cloned disk must
      resume the same branch, because the branch lives on the remote and not
      on the disk.
    * **the branch as this disk holds it**, when that copy already contains
      everything the published one does (``merge-base --is-ancestor``) — or
      when the remote has no such branch at all. Commits a run made and never
      pushed are its own work, kept rather than thrown away for being
      unpublished. A local branch that has *diverged* from the published one
      is the case this does not cover: it cannot be pushed without rewriting
      somebody else's history, so the published tip wins and the abandoned
      local head is reported in the trailer instead of vanishing.
    * **the base ref**, when the branch does not exist anywhere yet. The
      ordinary first checkout, unchanged.

    There is deliberately no "start this branch over from the base" flag. It
    would be a trap: the branch it rewinds is the branch the push then has to
    fast-forward, so every use of it would end in the rejection above. Starting
    from the base is spelled by asking for a branch name that is not in use.
    """
    return (
        "jhin_started=base\n"
        'jhin_local=$(git rev-parse --verify --quiet "refs/heads/$jhin_branch" || true)\n'
        'if [ -n "$jhin_published_sha" ]; then\n'
        f'  git {credential} fetch origin "refs/heads/$jhin_branch"\n'
        "  jhin_published=$(git rev-parse FETCH_HEAD)\n"
        '  if [ -n "$jhin_local" ] && '
        f'git {_HOOKLESS} merge-base --is-ancestor "$jhin_published" "$jhin_local"; then\n'
        "    jhin_started=workspace_branch\n"
        f'    git {_HOOKLESS} checkout "$jhin_branch"\n'
        "  else\n"
        "    jhin_started=remote_branch\n"
        '    if [ -n "$jhin_local" ]; then jhin_discarded=$jhin_local; fi\n'
        f'    git {_HOOKLESS} checkout -B "$jhin_branch" "$jhin_published"\n'
        "  fi\n"
        'elif [ -n "$jhin_local" ]; then\n'
        "  jhin_started=workspace_branch\n"
        f'  git {_HOOKLESS} checkout "$jhin_branch"\n'
        "else\n"
        f'  git {_HOOKLESS} checkout -B "$jhin_branch" "$jhin_basepoint"\n'
        "fi\n"
    )


async def _repository_checkout(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryCheckoutInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, git_connection_id = _connection_defaults(connection)
    git_base, token = await _git_credentials(ctx, git_connection_id)
    branch = data.branch or _default_branch(ctx.task_id, data.repository)
    clone_url = _clone_url(git_base, data.repository)
    # Bound before the script is built: the record this checkout writes is keyed
    # on the disk, and a refusal to bind must happen before anything is deleted.
    binding = await _binding(ctx)
    # Does this disk still hold only repositories the connection allows? A
    # checkout is the remedy the workspace validator's denial prints, and the
    # remedy has to be true: the reuse prologue removes ``/workspace/repo`` and
    # nothing else, so an allowed checkout onto a disk whose history is no
    # longer allowed used to move the record forward and leave the files. When
    # the answer is no, the whole workspace is emptied first and the record
    # says so, which is what lets the history start again from here.
    purge = bool(
        binding.durable
        and binding.row_id is not None
        and await forbidden_repositories_on_disk(
            ctx.session,
            workspace_id=ctx.workspace_id,
            row_id=binding.row_id,
            allowed=allowed_repositories(connection),
        )
    )
    # Jhin's own account of what it last left on this disk for this repository.
    # Empty means "no account", which is the same answer as "not the tree Jhin
    # left": clone fresh.
    reusable_config = "" if purge else await _reusable_config_sha(ctx, binding, data.repository)

    credential = _credential_args(git_base)
    quoted_url = shlex.quote(clone_url)
    trailer = _new_trailer(ctx)
    script = (
        "set -e\n"
        f"cd {_WORKSPACE_PATH}\n"
        f"jhin_branch={shlex.quote(branch)}\n"
        # The base ref is asked of the *remote*, never of repository state. On
        # a reused checkout the local HEAD is the agent's own branch, so
        # `rev-parse --abbrev-ref HEAD` would record the wrong base -- and this
        # is in any case the same reason the push refuses to read
        # refs/remotes/origin/HEAD. It doubles as the credential's first proof:
        # a bad token fails here, before anything on disk is touched.
        #
        # The working branch is asked for in the same breath, because it is the
        # same question about the same remote and one round trip answers both:
        # a branch the remote already holds is the commit this run must build
        # on (:func:`_branch_selection`).
        f"jhin_ls=$(git {credential} ls-remote --symref {quoted_url} HEAD "
        '"refs/heads/$jhin_branch")\n'
        "jhin_symref=$(printf '%s\\n' \"$jhin_ls\" | awk '$1==\"ref:\"{print $2; exit}')\n"
        'if [ -z "$jhin_symref" ]; then '
        "printf 'JHIN_ERR=repository_empty\\n' >&2; exit 65; fi\n"
        "jhin_base=${jhin_symref#refs/heads/}\n"
        "jhin_published_sha=$(printf '%s\\n' \"$jhin_ls\" | "
        "awk -v jhin_ref=\"refs/heads/$jhin_branch\" '$2==jhin_ref{print $1; exit}')\n"
        + (f"jhin_base={shlex.quote(data.ref)}\n" if data.ref else "")
        + _reuse_prologue(
            url=quoted_url,
            credential=credential,
            config_sha=reusable_config,
            purge=purge,
        )
        + f"cd {_REPO_PATH}\n"
        + _branch_selection(credential)
        + f'git {_HOOKLESS} config user.name "Jhin Agent"\n'
        f'git {_HOOKLESS} config user.email "agent@jhin.local"\n'
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
        "printf 'reused=%s\\n' \"$jhin_reused\"\n"
        "printf 'recloned=%s\\n' \"$jhin_reclone\"\n"
        "printf 'discarded=%s\\n' \"$jhin_discarded\"\n"
        "printf 'purged=%s\\n' \"$jhin_purged\"\n"
        "printf 'started=%s\\n' \"$jhin_started\"\n"
    )
    row, result = await _run_job(
        ctx,
        command_display=f"git checkout {clone_url} -> {branch}",
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
            "workspace_key": binding.key,
        },
        binding=binding,
        # Egress, but every byte of it is a read: ``ls-remote``, ``fetch`` and
        # ``clone``. A checkout cannot change the remote, so a runner that
        # dropped one leaves nothing outside the sandbox in doubt.
        external_effect=False,
    )
    _raise_for_failure(row, what="checkout", code="checkout_failed")

    _, entries = trailer.split(_raw_stdout(result))
    head_sha = _meta_one(entries, "head")
    base_ref = _meta_one(entries, "base")
    config_sha = _meta_one(entries, "config")
    reused = _meta_one(entries, "reused") == "1"
    # The purge is recorded from what the job reported rather than from what
    # Jhin asked for. The job runs under ``set -e``, so a completed job that
    # says ``purged=1`` is a workspace that was emptied; anything else is a
    # workspace that was not, and the disk keeps its history.
    purged = _meta_one(entries, "purged") == "1"
    # Which of the three starting points the working branch was put on. It is
    # one of exactly three words, so an unrecognised one is an unreadable
    # trailer and not a fact to record with a shrug.
    started_from = _meta_one(entries, "started")
    # Every recorded value is checked for its shape before it becomes the
    # record a push trusts. An unreadable trailer records *nothing*: a missing
    # config sha used to mean "skip the comparison", which is the one outcome
    # an attacker would choose, so it now means "there was no checkout".
    if not (
        _OBJECT_ID.match(head_sha)
        and _REF_NAME.match(base_ref)
        and _CONFIG_SHA.match(config_sha)
        and started_from in _BRANCH_STARTING_POINTS
    ):
        raise _refusal("checkout_unrecordable")
    _record_checkout(
        ctx,
        binding,
        {
            "repository": data.repository,
            "branch": branch,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "config_sha": config_sha,
            "remote_url": clone_url,
            "remote_host": _remote_host(clone_url),
            "git_connection_id": git_connection_id,
            "reused": reused,
            # Why a cache was thrown away, when it was, and what unpushed head
            # went with it: a discarded tree leaves a record rather than a
            # mystery.
            "reclone_reason": _meta_one(entries, "recloned"),
            "discarded_head": _meta_one(entries, "discarded"),
            # Where this run's branch started, which is the difference between
            # a run that began the work and a run that continued it.
            "started_from": started_from,
            # The disk's history restarts here when this is true, so it is
            # written on every record rather than only on the true ones: a
            # missing key and a false one must not be different questions.
            "purged": purged,
        },
    )
    return RepositoryCheckoutOutput(
        repository=data.repository,
        branch=branch,
        head_sha=head_sha,
        base_ref=base_ref,
        top_level=_top_level(entries),
        path=_REPO_PATH,
        reused=reused,
        started_from=started_from,
        **_job_output_fields(row, result),
    )


# --- cli.repository.push ---


async def _repository_push(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryPushInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, git_connection_id = _connection_defaults(connection)
    # The disk this run is pinned to, then Jhin's own account of what was
    # checked out *into that disk*, both read before a credential is minted: a
    # push with nothing to compare against never reaches the runner. An
    # approval resume re-enters with the same run id, so a parked push binds to
    # the very workspace it was staged against.
    binding = await _binding(ctx, enforce_size=False)
    record = await _checkout_record(ctx, binding, data.repository)
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
    trailer = _new_trailer(ctx)
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
        #
        # A push that fails asks the remote what it holds rather than guessing.
        # This is the difference between a run that survives a 403 and a run
        # that ends on one: "the branch is not there at my sha" is *proof* that
        # nothing of this agent's landed, and proof is what the gateway needs
        # before it may record an ordinary failure instead of an unknown
        # outcome. If the question itself cannot be answered — the remote is
        # unreachable, or the credential can no longer read — the exit code is
        # git's own and the call reconciles as unknown, which is the honest
        # answer. ``set -e`` does not fire inside an ``if`` condition.
        "jhin_local=$(git rev-parse HEAD)\n"
        # ``jhin_code=$?`` inside ``if ! cmd; then`` would read the *negated*
        # status, which is zero, and report a failed push as exit 0. The status
        # is captured from git itself.
        "jhin_code=0\n"
        f"git {credential} push {quoted_url} {refspec} || jhin_code=$?\n"
        'if [ "$jhin_code" != "0" ]; then\n'
        f"  if jhin_after=$(git {credential} ls-remote {quoted_url} "
        f"{shlex.quote(f'refs/heads/{data.branch}')}); then\n"
        "    jhin_remote=$(printf '%s\\n' \"$jhin_after\" | awk 'NR==1{print $1}')\n"
        '    if [ "$jhin_remote" != "$jhin_local" ]; then\n'
        "      printf 'JHIN_ERR=push_rejected\\n' >&2; exit 70\n"
        "    fi\n"
        "  fi\n"
        '  exit "$jhin_code"\n'
        "fi\n" + trailer.echo + "printf 'previous=%s\\n' \"$jhin_previous\"\n"
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
        "workspace_key": binding.key,
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
        binding=binding,
        # The one tool here that can change something outside the sandbox.
        external_effect=True,
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
        if code == "push_rejected":
            # The script asked the remote and the remote does not have this
            # branch at this workspace's sha, so nothing of the agent's landed:
            # a finished, retryable failure rather than an unknown outcome, and
            # the run stays alive. The detail is git's own stderr, which is
            # where the remote's reason lives — "Permission to owner/repo
            # denied" names an operator's fix that a bare error code never did.
            raise CliToolError(
                f"push rejected by the remote (exit {row.exit_code})",
                code="push_rejected",
                side_effect_possible=False,
                detail=_failure_detail(row),
            )
        if code in _REFUSAL_HINTS:
            raise _refusal(code)
        # Every other push failure. The remote could not be asked, or it
        # answered that the branch *is* at this sha — a 502 after the ref was
        # updated looks exactly like one before it — so this is not provably
        # side-effect free and reconciles as unknown. The evidence still
        # travels: an unknown outcome with no exit code and no stderr behind it
        # is the thing an operator cannot reconcile.
        raise ToolExecutionError(
            f"push failed ({row.status}, exit {row.exit_code})",
            code="push_failed",
            side_effect_possible=True,
            hint="The push did not complete. Check the branch on the remote before retrying.",
            detail=_failure_detail(row),
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
        publish_output=True,
    )
    return TestRunOutput(
        command=data.command,
        passed=row.exit_code == 0 and row.status == SandboxJobStatus.COMPLETED.value,
        **_job_output_fields(row, result),
    )


# --- cli.file.read ---

_TEXT_FILE_CHECK = r"""
import codecs, sys
decoder = codecs.getincrementaldecoder("utf-8")("strict")
try:
    with open(sys.argv[1], "rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            if b"\x00" in chunk:
                raise UnicodeError("binary file")
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
except UnicodeError:
    sys.stderr.write("JHIN_ERR=binary_file\n")
    sys.exit(65)
"""


async def _file_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileReadInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    quoted = shlex.quote(data.path)
    last = data.offset + data.limit - 1
    trailer = _new_trailer(ctx)
    script = _guarded(
        f"jhin_guard {quoted}\n"
        f"test -f {quoted} || {{ printf 'JHIN_ERR=not_a_file\\n' >&2; exit 65; }}\n"
        f"python3 -c {shlex.quote(_TEXT_FILE_CHECK)} {quoted}\n"
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
        workspace_effect=False,  # a read changes nothing, this attempt or any earlier one
    )
    _raise_for_failure(row, what="file read", code="file_read_failed")

    body, entries = trailer.split(_raw_stdout(result))
    # A retained answer from an older runner may predate the binary guard.
    if "\x00" in body or any(0xD800 <= ord(char) <= 0xDFFF for char in body):
        raise _refusal("binary_file")
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
    trailer = _new_trailer(ctx)
    # The write is staged beside the file and renamed over it, and that is the
    # difference between a file this tool can be interrupted in the middle of
    # and one it cannot. ``printf ... > file`` truncates first and then writes
    # up to 48,000 characters through stdio; a container killed between two
    # flushes -- by its own timeout, an operator's cancel, the runner draining
    # or the next runner reaping what the last one left -- leaves the file cut
    # off at a buffer boundary, with a read token nobody holds and a checkout
    # that looks edited. ``mv`` within one directory is ``rename``, which is
    # atomic: the file is the old one entire or the new one entire, at every
    # instant, whatever happens to this process.
    #
    # Everything acts on the resolved path rather than the given one, because
    # the rename must land on the file the guard approved: writing *through* a
    # symlink is what the redirection did, and replacing the symlink instead
    # would be a different act on a different file. ``.jhin``-prefixed, so a
    # staged file left by a kill is pruned from cli.file.list along with the
    # rest of Jhin's own state.
    #
    # The mode is carried over explicitly. ``mktemp`` creates 0600 and the
    # rename would take that with it, which would quietly un-execute every
    # script an agent rewrote.
    script = _guarded(
        f"jhin_guard {quoted}\n"
        f"jhin_target=$(realpath -m -- {quoted}) || "
        "{ printf 'JHIN_ERR=path_unresolvable\\n' >&2; exit 66; }\n"
        'if [ -e "$jhin_target" ]; then\n'
        "  [ -f \"$jhin_target\" ] || { printf 'JHIN_ERR=not_a_file\\n' >&2; exit 65; }\n"
        '  if [ -z "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_exists_pass_read_token\\n' >&2; exit 65; fi\n"
        '  jhin_actual=$(sha256sum -- "$jhin_target" | cut -c1-64)\n'
        '  if [ "$jhin_actual" != "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_changed\\n' >&2; exit 65; fi\n"
        '  jhin_mode=$(stat -c %a -- "$jhin_target")\n'
        "else\n"
        '  if [ -n "$JHIN_READ_TOKEN" ]; then '
        "printf 'JHIN_ERR=file_missing_for_read_token\\n' >&2; exit 65; fi\n"
        "  jhin_mode=644\n"
        "fi\n"
        'jhin_dir=$(dirname -- "$jhin_target")\n'
        'mkdir -p -- "$jhin_dir"\n'
        f"jhin_guard {quoted}\n"
        'jhin_staged=$(mktemp -- "$jhin_dir/.jhin-write-XXXXXX") || '
        "{ printf 'JHIN_ERR=write_staging_failed\\n' >&2; exit 65; }\n"
        'printf \'%s\' "$JHIN_FILE_CONTENT" > "$jhin_staged"\n'
        'chmod "$jhin_mode" -- "$jhin_staged"\n'
        'mv -f -- "$jhin_staged" "$jhin_target"\n'
        + trailer.echo
        + 'printf \'bytes=%s\\n\' "$(wc -c < "$jhin_target")"\n'
        'printf \'sha=%s\\n\' "$(sha256sum -- "$jhin_target" | cut -c1-64)"\n'
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
    _raise_for_failure(row, what="file write", code="file_write_failed")

    _, entries = trailer.split(_raw_stdout(result))
    bytes_written = _meta_int(entries, "bytes", default=len(data.content.encode()))
    return FileWriteOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        bytes_written=bytes_written,
        read_token=_read_token(entries),
    )


# --- cli.file.edit ---


_EDIT_PROGRAM = r"""import hashlib, os, stat, sys, tempfile
# The path as the guard resolved it. The shell's ``jhin_guard`` ran
# ``realpath`` over this same name and passed judgement on what it found, so
# the file this program writes has to be that one and not a link that has
# since been pointed elsewhere -- and the replace at the end must land on the
# real file rather than turning a symlink into a regular file.
path = os.path.realpath(os.environ["JHIN_EDIT_PATH"])
old = os.environ["JHIN_EDIT_OLD"]
new = os.environ["JHIN_EDIT_NEW"]
expected = int(os.environ["JHIN_EDIT_EXPECTED"])
try:
    handle = open(path, "rb")
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
# One guard, one question. The count answers "is this the one place I meant?"
# -- ambiguity -- and that is the only question a file's contents can answer.
# It was briefly asked to answer a second one, "has this edit already been
# applied?", and no test over contents can: a file holding the result of an
# edit is the same file whether this call put it there, another call did, or
# it was written that way to begin with. That question is about an event, and
# it is settled where the events are visible -- one dispatch per invocation,
# at the sandbox runner (``JobManager.submit``). Nothing here guesses at it.
count = data.count(old)
if count != expected:
    sys.stderr.write("JHIN_ERR=edit_count_mismatch\n")
    sys.stderr.write("JHIN_ACTUAL=%d\n" % count)
    raise SystemExit(65)
written = data.replace(old, new).encode("utf-8")
# Write beside the file and rename over it, rather than truncating it and
# writing in place. ``rename`` within a directory is atomic, so every observer
# -- including the next dispatch of this call, and the person reading the
# workspace afterwards -- sees either the old file entire or the new file
# entire. In place, a container killed between two flushes leaves a file that
# is neither, and this container can be killed: by its own timeout, by an
# operator's cancel, by the runner draining, by the next runner reaping what
# the last one left. The temporary name begins with ``.jhin`` so a leftover
# from such a kill is pruned from cli.file.list like the rest of Jhin's own
# state.
directory = os.path.dirname(path) or "."
descriptor, staged = tempfile.mkstemp(prefix=".jhin-edit-", dir=directory)
try:
    with os.fdopen(descriptor, "wb") as out:
        out.write(written)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(staged, stat.S_IMODE(info.st_mode))
    os.replace(staged, path)
except BaseException:
    if os.path.exists(staged):
        os.unlink(staged)
    raise
# Trailer *entries* only. The sentinel is the shell's to print, from the one
# place that knows this job's nonce; a program that printed its own would be a
# second emitter, and the two would drift (they did).
sys.stdout.write("replacements=%d\n" % count)
sys.stdout.write("sha=%s\n" % hashlib.sha256(written).hexdigest())
"""


async def _file_edit(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileEditInput, payload)
    # The degenerate edit, refused before a container starts: it writes the
    # file back byte for byte, so it is a container's worth of work to change
    # nothing, and an agent that asked for it has almost certainly made a
    # mistake it would rather be told about than have silently succeed.
    if data.new_string == data.old_string:
        raise _refusal("edit_changes_nothing")
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    # Both strings and the path travel in the environment: never in argv, so
    # nothing about them can be read from ``ps`` or reinterpreted by a shell.
    trailer = _new_trailer(ctx)
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
        _raise_for_failure(row, what="file edit", code="file_edit_failed")

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
    trailer = _new_trailer(ctx)
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
        workspace_effect=False,
    )
    _raise_for_failure(row, what="file list", code="file_list_failed")

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
    trailer = _new_trailer(ctx)
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
        workspace_effect=False,
    )
    _raise_for_failure(row, what="file search", code="file_search_failed")

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


def _typed(executor: ToolExecutor) -> ToolExecutor:
    """One cli executor, with the last untyped refusal on its path typed.

    ``ConnectionResolutionError`` is a plain ``Exception``, and it is raised by
    the first statement of every tool here: an unknown, disabled or
    wrong-workspace connection. Reaching the gateway untyped, it was read as
    "the executor may have done something", so a call that never got as far as
    a container reconciled as ``execution_unknown`` and stopped the run. It is
    the same shape the MCP, Supabase and Vercel connectors already convert, and
    it is converted here for the same reason: nothing has happened yet.
    """

    @wraps(executor)
    async def run(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        try:
            return await executor(ctx, payload)
        except ConnectionResolutionError as error:
            raise ToolExecutionError(
                str(error),
                code="connection_unavailable",
                side_effect_possible=False,
                hint=(
                    "That connection cannot be used right now. Check the connection id, "
                    "or ask an operator whether it is still enabled."
                ),
            ) from None

    return run


CLI_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (FILE_PUBLISH_TOOL, _typed(file_publish)),
    (
        ToolDefinition(
            name="cli.command.execute",
            description=(
                "Run a shell command inside an ephemeral sandbox container. The "
                "workspace (and any repository checkout at /workspace/repo) is "
                "yours and persists between turns, so an install or a build you "
                "do now is still there next time. This tool never holds a git "
                "credential: commit and push a branch with cli.repository.push "
                "instead."
            ),
            risk=RiskLevel.WRITE,
            input_model=CommandExecuteInput,
            output_model=CommandExecuteOutput,
            required_capability="cli.command.execute",
            supports_approval=True,
            scope_keys=("connection_id", "command", "image", "network"),
            # The mandatory CLI validator resolves connection defaults and
            # treats Internet allow as a ceiling, without widening denies.
            defers_scope=True,
            # No. The command is the model's, and ``network`` may be
            # ``internet``: one call of this tool can POST to anything the
            # sandbox bridge can reach. The classification is per tool, not
            # per call, so the networkless majority is decided by the
            # networked minority — which is the right way round, because the
            # cost of being wrong here is a duplicated external effect.
            redispatch_is_safe=False,
        ),
        _typed(_command_execute),
    ),
    (
        ToolDefinition(
            name="cli.repository.checkout",
            description=(
                "Check a repository out into your sandbox workspace using a short-lived "
                "credential and put yourself on a working branch (default: "
                "agent/<repo>-<task id>). A branch that already exists is CONTINUED, "
                "not restarted: if an earlier run of this task pushed it, you start on "
                "that commit and its work is already done — started_from says which of "
                "base, remote_branch or workspace_branch you got. The base ref decides "
                "where a new branch starts and nothing else, so to begin again from the "
                "base, ask for a branch name that is not in use. "
                "If the workspace already holds this repository it is refreshed in "
                "place — reused=true in the result, and any dependency install or "
                "build output from an earlier turn is still there. "
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
            # Yes, and it is the case this field exists for. A checkout has
            # egress, but every byte of it is a read — ``ls-remote``,
            # ``fetch``, ``clone`` — and its writes land on a disk Jhin owns
            # and deliberately reuses: the tool's own contract is that
            # running it on a workspace that already holds the repository
            # refreshes it in place. The same reasoning the executor already
            # applies to a dropped runner (``external_effect=False`` below),
            # applied to a dropped worker.
            redispatch_is_safe=True,
        ),
        _typed(_repository_checkout),
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
            # No. This is the tool the at-most-once guarantee was written
            # for: a push that may or may not have reached origin is exactly
            # the outcome no amount of database state can settle, and the
            # only honest ending is to stop and show a person the branch.
            redispatch_is_safe=False,
        ),
        _typed(_repository_push),
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
            # Yes, and this is the one that shows the field is not a
            # restatement of ``risk``. The command is arbitrary, which is why
            # it is WRITE; the job is ``network="none"`` with no git
            # credential, which is why re-running it cannot reach anything.
            # The worst a second run can do is repeat work on the agent's own
            # disk, and nothing leaves that disk without ``cli.repository.push``.
            redispatch_is_safe=True,
        ),
        _typed(_test_run),
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
            # Yes. A networkless ``find`` over Jhin's own disk. This is the
            # call the operator actually asked for when a redeploy ended
            # their conversation, and there was never anything about it to
            # reconcile.
            redispatch_is_safe=True,
        ),
        _typed(_file_list),
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
            # Yes. A networkless ``grep`` over Jhin's own disk; it writes
            # nothing anywhere.
            redispatch_is_safe=True,
        ),
        _typed(_file_search),
    ),
    (
        ToolDefinition(
            name="cli.file.read",
            description=(
                "Read UTF-8 text/code from the sandbox workspace (path relative to the "
                "checkout). Returns a line window plus total_lines, has_more and a "
                "read_token; page through a large file with offset and limit. Keep the "
                "read_token: cli.file.write requires it. Binary documents require terminal "
                "document libraries such as python-docx, openpyxl, python-pptx or pypdf."
            ),
            risk=RiskLevel.READ,
            input_model=FileReadInput,
            output_model=FileReadOutput,
            required_capability="cli.file.read",
            scope_keys=("connection_id", "path"),
            # Yes. A networkless read of Jhin's own disk. The read_token it
            # returns is derived from the file it just read, so a second read
            # hands back a token for the same state rather than a stale one.
            redispatch_is_safe=True,
        ),
        _typed(_file_read),
    ),
    (
        ToolDefinition(
            name="cli.file.edit",
            description=(
                "Replace an exact string in one file of the sandbox checkout. "
                "old_string must occur exactly expected_count times or nothing is "
                "written and the real count is reported. This is the safe way to "
                "change part of a file you have only read part of."
            ),
            risk=RiskLevel.WRITE,
            input_model=FileEditInput,
            output_model=FileEditOutput,
            required_capability="cli.file.edit",
            supports_approval=True,
            scope_keys=("connection_id", "path"),
            # Yes, and for a reason that has nothing to do with this tool.
            #
            # A second dispatch of one invocation never reaches a second
            # container: the runner recognises the invocation and hands it the
            # first dispatch's job (``JobManager.submit``), so the edit is
            # applied once and both dispatches are told the same thing about
            # it. Two attempts at proving this from the file's *contents* both
            # failed, and had to — the occurrence count guards ambiguity and
            # cannot see repetition, and the guard added on top of it refused
            # ordinary first-time deletions while still passing the case where
            # ``old`` and ``new`` are anagrams of each other. There is no test
            # over a file that distinguishes "my earlier dispatch wrote this"
            # from "somebody else did", because the file records an effect and
            # the question is about an event.
            #
            # What remains here is the ambiguity guard, which is a real and
            # different promise, and an atomic replace, so an interrupted
            # attempt leaves the file as it was rather than half rewritten.
            redispatch_is_safe=True,
        ),
        _typed(_file_edit),
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
            # Yes, on the same ground as the edit: a second dispatch of one
            # invocation is answered with the first dispatch's job and never
            # reaches a container.
            #
            # The read token is not that guarantee and is not asked to be. It
            # is a promise to a *person* — that a write built on a partial
            # read cannot silently drop the rest of a file — and it happens
            # also to refuse most repeats, which is a pleasant accident and
            # not an argument: it says nothing at all about the one repeat
            # that writes the bytes already there. The change reaches nobody
            # outside the sandbox until ``cli.repository.push`` puts it there.
            redispatch_is_safe=True,
        ),
        _typed(_file_write),
    ),
)
