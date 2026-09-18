"""Job execution engine: fresh ephemeral Docker container per job (plan 14).

Isolation invariants enforced here (plan 14.1, 14.3, 48.7):

- jobs NEVER receive the Docker socket, host mounts, or the compose
  control/data networks — the only mounts are a named workspace volume and
  a tmpfs;
- ``network_policy=none`` → Docker ``none`` network; ``internet`` → a
  dedicated sandbox bridge network that carries no control-plane services;
- read-only root filesystem, cap_drop ALL, no-new-privileges, non-root
  uid 1000, CPU/memory/pids caps, wall-clock timeout;
- containers are force-removed in a ``finally`` and labeled so orphans from
  a crashed runner are reaped at the next startup;
- captured stdout/stderr is redacted (job secret values) and size-capped
  before it ever leaves this process;
- **one dispatch per invocation.** A tool call whose worker died is
  re-dispatched with a fresh ``job_id``, so nothing on the wire relates the
  two attempts and the duplicate-id check in :meth:`JobManager.submit` could
  never catch them. This process is the only one that sees both, so it is the
  only one that can refuse to run the second: a request carries the caller's
  identity for the *invocation*, and a second dispatch of one is handed the
  first dispatch's job — running, or finished with its outcome intact — in
  place of a container of its own. See :meth:`JobManager.submit`. This is
  what makes every sandbox tool safe to re-dispatch, and it is why no tool
  needs a guard of its own that inspects the file it wrote.

  Three properties hold the ledger up, and each of them is a way it stopped
  being true:

  * **it records what happened, not what was intended.** An invocation is
    written into the ledger when its dispatch is accepted, so two dispatches
    arriving together cannot both start — but a dispatch that ends without
    ever leaving the workspace queue ran nothing at all, and being remembered
    as that invocation's answer made every later dispatch of it a replay of a
    job that never happened. Such a dispatch is dropped from the ledger as it
    ends (:meth:`JobManager._forget_dispatch`);
  * **it is bounded, and says what it has forgotten.** Records do not live
    forever (:meth:`JobManager._forget_expired`), and forgetting an invocation
    silently would turn "I have no record of that" back into "so it never
    ran". Every forgotten dispatch moves :attr:`JobManager._forgotten_through`
    forward, and the absence of a record is only read as proof that nothing
    ran for dispatches *after* that moment;
  * **it fails closed at both ends.** A dispatch this runner cannot account
    for is refused rather than repeated — here, and in the caller that decides
    what to put in ``prior_dispatch_at``
    (``jhin_connectors.cli.tools._dispatch_history``). The dependency between
    the two ends is spelled out rather than assumed: a request that offers an
    ``invocation_id`` must *state* ``prior_dispatch_at``, because a client
    that simply omitted it would be read as claiming there was no earlier
    dispatch, which is the claim that starts a container.
- **one job at a time per workspace volume.** The disk is shared, mutable
  state. With invocations settled above, the jobs that meet here are
  genuinely different calls that happen to want the same disk — a run's
  cleanup against a turn that is still going, two runs of one agent — and for
  ``cli.repository.checkout`` the second one would delete the tree the first
  is still writing. Jobs for a busy workspace wait for it
  (:meth:`JobManager._hold_workspace`) and give up, having run nothing, if it
  does not come free.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from jhin_observability import get_logger, normalize_sandbox_outcome
from jhin_sandbox_runner.docker_socket import (
    DockerSocketConfigurationError,
    daemon_is_docker_desktop,
    normalize_supplemental_groups,
    validate_docker_authority,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest, SandboxJobStatusResponse
from jhin_sandbox_runner.settings import Settings
from jhin_secrets.redaction import SecretRedactor

logger = get_logger(__name__)

JOB_LABEL = "jhin.sandbox.job"
WORKSPACE_LABEL = "jhin.sandbox.workspace"
WORKSPACE_KIND_LABEL = "jhin.sandbox.workspace.kind"
WORKSPACE_INIT_LABEL = "jhin.sandbox.workspace.init"
WORKSPACE_VOLUME_PREFIX = "jhin-sandbox-ws-"

#: A workspace that belongs to an agent and outlives every run that uses it.
#: Its lifetime is decided by the control plane (idle, size, an operator's
#: reset), never by this process's startup age sweep — creation age is not use
#: age, and a volume created three weeks ago and used ten minutes ago is
#: exactly the thing durable workspaces exist to keep.
WORKSPACE_KIND_AGENT = "agent"
#: A workspace that belongs to one run and is deleted when that run finalizes.
#: Startup reaping is the backstop for the runs whose cleanup never ran.
WORKSPACE_KIND_RUN = "run"

#: How often a queued job re-asks whether its workspace has come free. Small
#: enough that a job which has just finished hands the disk on immediately,
#: large enough that a queue of waiters costs nothing measurable.
_WORKSPACE_POLL_SECONDS = 0.05

#: How far before the point this runner remembers from a dispatch may have
#: started and still be treated as one it could not have seen.
#:
#: The two clocks being compared are the tool worker's and this runner's, and
#: **the deployment assumption is that they are the same clock**: the shipped
#: topology runs both as containers on one Docker host, where a container
#: reads the host's clock and there is no skew to allow for. So the margin is
#: not for skew but for order — the caller stamps its row and *then* submits,
#: and a submit that arrived a moment after this process opened its socket may
#: have been stamped a moment before. Five seconds is generous for that gap by
#: three orders of magnitude, and it is not a synchronisation budget.
#:
#: The margin errs towards refusing, because the two mistakes are not the same
#: size: a needless refusal costs a tool call that ran nothing and said so,
#: and a needless run costs an edit applied twice. Which is also why the
#: assumption is not left implicit. Splitting the tool worker and this runner
#: across hosts makes this the one input that can turn a refusal into a second
#: container — a caller whose clock runs ahead stamps dispatches that look
#: newer than this runner's memory — so such a deployment must keep the two
#: hosts NTP-synchronised to well inside this margin, and is stated as a
#: requirement in docs/architecture/sandboxing.md. The half of a violation
#: that is detectable from here is detected: a dispatch stamped in this
#: runner's own future by more than the margin is proof the clocks disagree,
#: and :meth:`JobManager.submit` refuses it rather than running on a
#: comparison it can see is meaningless.
_INCARNATION_SKEW = timedelta(seconds=5)

_TRUNCATION_MARKER = "\n…[truncated by sandbox runner]"
#: What a job's streams say once the runner has let go of what the container
#: printed. Not an empty string: "the job printed nothing" and "the runner no
#: longer holds what it printed" are different facts, and a caller reading the
#: first when the second is true reports a job that succeeded as one whose
#: trailer could not be found, with no way to tell why.
_OUTPUT_RELEASED_MARKER = "…[output no longer retained by sandbox runner]"
_POLL_INTERVAL_SECONDS = 0.5
DOCKER_CHECK_TIMEOUT_SECONDS = 5.0
_FORBIDDEN_JOB_ENV_NAME_PREFIXES = ("DOCKER_", "SANDBOX_DOCKER_")
_FORBIDDEN_JOB_ENV_SOCKET_PATHS = (
    "/var/run/docker.sock",
    "/run/jhin/docker.sock",
    "/run/host/docker.sock",
)
_ROOTLESS_TRANSPORT_HOSTNAME = "rootless-docker-transport"
_WORKSPACE_INIT_TARGET = "/jhin-workspace-init"
# The root init container already runs against the volume on every job, so it
# is the cheapest place to answer "how big is this workspace". The walk is
# bounded by a wall-clock budget and prints exactly two lines, neither of which
# contains anything a repository chose — so there is nothing here to inject
# into, and the strict parser below is the second half of that argument.
#
# A walk that skipped anything at all, for any reason, reports a lower bound
# (``JHIN_WS_PARTIAL=1``): the budget ran out, an entry could not be stat'd, a
# directory could not be opened, another filesystem was mounted underneath.
# A lower bound is not a size, and the control plane reads it as exactly that —
# the workspace's size is *unknown*, and an unknown size refuses the agent's
# next call rather than being enforced against. It used to be read as a
# conservative size, which is a contradiction the numbers settle: 3,000
# directories of 1,000 empty files with a 6 GiB payload in the directory
# ``scandir`` returns last reported 12 to 21 MB of a real 6,530,826,240 through
# this runner, six runs in a row. Nothing about that number is conservative;
# it is under every cap in the product.
#
# The direction of the error is still the reason it must never be an
# over-report: an over-report is acted on by destroying an agent's tree, and an
# under-report is acted on by refusing a call.
#
# The walk needs CAP_DAC_READ_SEARCH, and the reason is the two lines above it:
# the directory has just been given to uid 1000 with mode 0700, and a root
# process that dropped every capability cannot read a directory it does not own
# and has no permission bit for. Without it every scandir raised, the script
# reported ``BYTES=0 PARTIAL=1``, and the size accounting that stands in for a
# filesystem quota measured every workspace as empty forever. The capability is
# read-and-search only, it is added only on the jobs that measure, and the
# container it is added to has no network, a read-only rootfs, no-new-privileges
# and exactly one thing mounted: the workspace it is counting.
_WORKSPACE_INIT_SCRIPT = """\
import os
import stat
import time

path = "/jhin-workspace-init"
before = os.lstat(path)
if not stat.S_ISDIR(before.st_mode):
    raise SystemExit(2)
os.chmod(path, 0o700)
os.chown(path, 1000, 1000)
after = os.stat(path)
expected = (1000, 1000, 0o700)
actual = (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode))
if actual != expected:
    raise SystemExit(3)

if os.environ.get("JHIN_WS_MEASURE") == "1":
    try:
        budget = float(os.environ.get("JHIN_WS_BUDGET") or 5)
    except ValueError:
        budget = 5.0
    deadline = time.monotonic() + budget
    # This walk is the disk's only bound, so it is written to be the same
    # number ``du -sB1`` prints -- not approximately, and not by patching one
    # divergence at a time. Two properties get it there, and both are
    # structural rather than defensive:
    #
    # 1. **It descends by directory file descriptor**, opening each child with
    #    ``openat`` and stat-ing each entry with ``fstatat`` relative to the
    #    directory it lives in. Names are what the kernel is given; absolute
    #    paths are never built, so PATH_MAX does not exist here. A walk that
    #    used ``DirEntry.stat``'s implicit ``lstat(entry.path)`` failed with
    #    ENAMETOOLONG on every file below about 4 KiB of ancestry, and the one
    #    ``except OSError`` that did not raise ``partial`` swallowed it:
    #    twenty 200-character directories holding one 6 GiB file measured
    #    90112 bytes, ``partial = False``, durably, against a 5 GiB cap. The
    #    cap never bit, the bind never recycled, eviction never saw it.
    #
    # 2. **A file with more than one link is counted once**, keyed on its
    #    inode, which is exactly what ``du`` does and why its number is the
    #    disk's. Summing every link instead read 200 links to a 50 MB file as
    #    10538196992 bytes against du's 52436992 -- a factor of 201 -- and two
    #    ``git clone --local`` copies of a 40 MB repository 49.9% high. That is
    #    ordinary behaviour, not an attack: pnpm, uv and pip all hardlink from
    #    a store. Over-counting is *not* the safe direction, whatever this
    #    comment used to say. The number is read by an eviction that destroys
    #    the volume, so an over-count is a false positive that throws away an
    #    agent's unpushed work -- the one trade the control plane says is never
    #    worth making. Under-counting only delays a cap; over-counting deletes
    #    a tree.
    #
    # Directories are counted with everything else, against a specific
    # evasion: counting only regular files reported this very workspace at
    # 66301 bytes where ``du`` said 180989, the whole difference being
    # directory inodes -- and a tree of a million empty directories would have
    # measured as zero while filling the disk. They are never entered into the
    # link table: every directory has ``st_nlink > 1`` and none of them can be
    # hardlinked, so keying them would spend the table on the one thing that
    # cannot alias.
    #
    # ``st_blocks * 512`` is disk usage, and ``st_size`` -- what this walk used
    # to add up -- is not. A file the kernel has allocated without extending
    # (``fallocate -n -l 2G``) has st_size 0 and 2 GiB of blocks; 100k
    # one-byte files are 412418048 bytes of 4 KiB blocks and 2557600 bytes of
    # content. Blocks are what ``du`` counts, which is what an operator checks
    # this number against.
    device = after.st_dev
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    # Seeded with the root's own inode so the number is exactly what an
    # operator gets from ``du -sB1 /workspace``, which is what they will check
    # it against.
    total = after.st_blocks * 512
    partial = 0
    seen = 0
    # One inode per multiply-linked file. Bounded by memory, not by taste:
    # past the bound a multiply-linked file is skipped rather than counted
    # again, which keeps the total a lower bound (``partial = 1``) instead of
    # turning it into the over-count above. The bound is two million because
    # exceeding it now costs the agent its next call rather than a slightly
    # low number, and a pnpm or uv store hardlinks hundreds of thousands of
    # files into a tree without anybody intending anything by it. Two million
    # inodes is roughly 130 MB of set, which is why the init container's
    # memory is 256 MiB.
    links = set()
    stack = []
    try:
        opened = os.open(path, flags)
    except OSError:
        partial = 1
    else:
        try:
            stack.append((opened, os.scandir(opened)))
        except OSError:
            os.close(opened)
            partial = 1
    # Depth-first, so the open descriptors are bounded by the depth of the
    # tree rather than by its width.
    while stack:
        parent, entries = stack[-1]
        try:
            entry = next(entries)
        except StopIteration:
            entry = None
        except OSError:
            partial = 1
            entry = None
        if entry is None:
            entries.close()
            os.close(parent)
            stack.pop()
            # Every 128 entries *and* every finished directory, so a tree of
            # many tiny directories is bounded by the same clock as a
            # directory of many files. The runner waits the budget plus a
            # fixed margin, so a walk that overran both would fail the job
            # rather than report a lower bound.
            if stack and time.monotonic() > deadline:
                partial = 1
                break
            continue
        seen += 1
        if seen % 128 == 0 and time.monotonic() > deadline:
            partial = 1
            break
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            partial = 1
            continue
        # Stay on the volume: another mount under it is somebody else's disk
        # and not this workspace's size. Nothing mounts anything under the
        # init container's single mount, so this costs nothing in practice --
        # and if it ever fires, the entry was skipped and the total says so.
        if info.st_dev != device:
            partial = 1
            continue
        directory = stat.S_ISDIR(info.st_mode)
        if info.st_nlink > 1 and not directory:
            if info.st_ino in links:
                # Not a skip: these blocks are already in the total, counted
                # under the first link this walk reached.
                continue
            if len(links) >= 2000000:
                partial = 1
                continue
            links.add(info.st_ino)
        total += info.st_blocks * 512
        if directory:
            try:
                child = os.open(entry.name, flags, dir_fd=parent)
            except OSError:
                partial = 1
                continue
            try:
                stack.append((child, os.scandir(child)))
            except OSError:
                os.close(child)
                partial = 1
    for parent, entries in stack:
        entries.close()
        os.close(parent)
    print("JHIN_WS_BYTES=%d" % total)
    print("JHIN_WS_PARTIAL=%d" % partial)
raise SystemExit(0)
"""
# Deliberately anchored and bounded: exactly one match is required, so a line
# that merely looks like a measurement cannot become one.
_WORKSPACE_BYTES_RE = re.compile(r"^JHIN_WS_BYTES=(\d{1,20})$", re.MULTILINE)
_WORKSPACE_PARTIAL_RE = re.compile(r"^JHIN_WS_PARTIAL=([01])$", re.MULTILINE)


class JobValidationError(Exception):
    """The request asks for more than the configured caps allow."""


class InvocationOutcomeUnknownError(Exception):
    """This invocation was dispatched before, and this runner cannot say what
    that dispatch did.

    Not a validation failure and not an infrastructure failure: it is a
    refusal to guess. There are two ways to arrive here, and they are the two
    ways this process's memory can fail to cover an earlier dispatch:

    * **it was not here.** The caller says an earlier dispatch of this same
      invocation began at a moment when this process was not yet serving, so
      whatever container that dispatch started belonged to a runner that is
      gone — and a runner's first act on startup is to force-remove the
      containers of its previous life (:meth:`JobManager.reap_orphans`), which
      means that container was killed rather than finished;
    * **it has forgotten.** The dispatch began after this process started, but
      long enough ago that its record has been dropped
      (:meth:`JobManager._forget_expired`). The outcome was real and is no
      longer held.

    Either way, running the job now would apply its effect a second time on
    top of however far the first got. Nothing is started, and the caller is
    told which of the two things it is: not "your job failed" but "the outcome
    of your earlier attempt cannot be established here".
    """


class DockerDaemonConfigurationError(RuntimeError):
    """The selected Docker daemon does not match the configured trust mode."""


@dataclass(frozen=True)
class _Dispatch:
    """One invocation's first dispatch: which job it got, and when.

    The moment matters as much as the id. It is what
    :attr:`JobManager._forgotten_through` is made of, so that dropping this
    entry weakens the runner's answer honestly instead of silently turning
    "this ran" back into "nothing ran".
    """

    job_id: str
    at: datetime


@dataclass
class JobRecord:
    request: SandboxJobRequest
    image: str
    cpu_limit: float
    memory_mb: int
    pids_limit: int
    timeout_seconds: int
    #: When this runner accepted the dispatch. Not the same as
    #: ``started_at``, which waits for the workspace queue, and the difference
    #: is why this exists: retention is measured against the moment the
    #: caller's own ``sandbox_job`` row was stamped, and that is this one.
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: ``queued`` until this job holds its workspace and is about to start a
    #: container. Every non-terminal status means the same thing to a poller —
    #: keep polling — so the distinction costs the caller nothing and is the
    #: difference between a status document that is true and one that says a
    #: container is running when none exists.
    status: str = "queued"
    container_id: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    #: When this job's *container* started, which is the only clock its
    #: ``timeout_seconds`` may be measured against. ``started_at`` is set when
    #: the job begins being worked on, and between the two lie the workspace
    #: queue and the workspace measurement — up to ninety seconds of the
    #: runner's own housekeeping that used to be billed to the job. A file
    #: edit with a thirty-second timeout on a workspace that took forty to
    #: walk was killed for exceeding a budget it had not been given a second
    #: of.
    container_started_at: datetime | None = None
    finished_at: datetime | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    #: Whether this record still holds what its container printed. Set False
    #: by :meth:`JobManager._release_surplus_output`, and reported rather than
    #: hidden: it is the difference between a job that said nothing and a job
    #: whose words this process has thrown away.
    output_retained: bool = True
    error: str | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
    # What the init container counted on this workspace, and whether the walk
    # finished. None means "no measurement was taken on this job", which the
    # caller reads as "leave the stored number alone"; a walk that ran and did
    # not finish reports a floor with ``partial`` set, which the caller reads
    # as "this disk's size is no longer known".
    workspace_size_bytes: int | None = None
    workspace_size_partial: bool = False
    #: The most the runner may spend before this job's container starts, as
    #: reported to the caller so its poll deadline can cover it.
    pre_start_budget_seconds: int = 0

    def release_output(self) -> None:
        """Let go of everything this job printed, and of its secrets.

        What is kept is the answer — status, exit code, timings, the runner's
        own ``error`` — because that is what a poller and the tool worker's
        reconciliation sweep ask this record for, and it costs a few hundred
        bytes. What goes is the two capped streams (up to 64 KiB each), the
        redactor holding this job's secret values, and the plaintext
        ``secret_env`` the request arrived with: a credential that outlives
        the container it was minted for is retention nobody asked for, and the
        only code that reads it has long since run.

        Idempotent, and never applied to a job that has not finished — a
        running job's streams are still being written.
        """
        self.stdout = _OUTPUT_RELEASED_MARKER
        self.stderr = _OUTPUT_RELEASED_MARKER
        self.stdout_truncated = True
        self.stderr_truncated = True
        self.output_retained = False
        self.redactor = SecretRedactor()
        self.request.secret_env = {}

    def to_response(self) -> SandboxJobStatusResponse:
        duration_ms: int | None = None
        if self.started_at is not None and self.finished_at is not None:
            duration_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)
        return SandboxJobStatusResponse(
            job_id=self.request.job_id,
            invocation_id=self.request.invocation_id,
            pre_start_budget_seconds=self.pre_start_budget_seconds,
            status=self.status,
            image=self.image,
            network_policy=self.request.network_policy,
            exit_code=self.exit_code,
            started_at=self.started_at.isoformat() if self.started_at else None,
            finished_at=self.finished_at.isoformat() if self.finished_at else None,
            duration_ms=duration_ms,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            error=self.error,
            workspace_size_bytes=self.workspace_size_bytes,
            workspace_size_partial=self.workspace_size_partial,
        )


def resolve_limits(request: SandboxJobRequest, settings: Settings) -> tuple[float, int, int, int]:
    """(cpu, memory_mb, pids, timeout) — request values bounded by hard caps.

    Requests above a cap are rejected loudly rather than silently clamped,
    so a misconfigured grant surfaces as an error instead of a surprise.
    """
    cpu = request.cpu_limit if request.cpu_limit is not None else settings.sandbox_max_cpus
    memory = request.memory_mb if request.memory_mb is not None else settings.sandbox_max_memory_mb
    pids = request.pids_limit if request.pids_limit is not None else settings.sandbox_max_pids
    timeout = (
        request.timeout_seconds
        if request.timeout_seconds is not None
        else settings.sandbox_max_timeout_seconds
    )
    if cpu > settings.sandbox_max_cpus:
        raise JobValidationError(f"cpu_limit {cpu} exceeds cap {settings.sandbox_max_cpus}")
    if memory > settings.sandbox_max_memory_mb:
        raise JobValidationError(f"memory_mb {memory} exceeds cap {settings.sandbox_max_memory_mb}")
    if pids > settings.sandbox_max_pids:
        raise JobValidationError(f"pids_limit {pids} exceeds cap {settings.sandbox_max_pids}")
    if timeout > settings.sandbox_max_timeout_seconds:
        raise JobValidationError(
            f"timeout_seconds {timeout} exceeds cap {settings.sandbox_max_timeout_seconds}"
        )
    return cpu, memory, pids, timeout


def workspace_volume_name(workspace_key: str) -> str:
    return f"{WORKSPACE_VOLUME_PREFIX}{workspace_key}"


def _workspace_volume_mount(workspace_key: str, target: str) -> dict[str, Any]:
    return {
        "Type": "volume",
        "Source": workspace_volume_name(workspace_key),
        "Target": target,
        "ReadOnly": False,
        "VolumeOptions": {"NoCopy": True},
    }


def _job_environment_value_is_safe(name: str, value: str) -> bool:
    if name.startswith(_FORBIDDEN_JOB_ENV_NAME_PREFIXES):
        return False
    if _ROOTLESS_TRANSPORT_HOSTNAME in value.casefold():
        return False
    return not any(path in value for path in _FORBIDDEN_JOB_ENV_SOCKET_PATHS)


def build_container_config(
    request: SandboxJobRequest,
    settings: Settings,
    *,
    image: str,
    cpu_limit: float,
    memory_mb: int,
    pids_limit: int,
) -> dict[str, Any]:
    """The Docker container create payload for one job.

    Pure function so the security-relevant knobs are directly unit-testable
    (no privileged mode, no host network, cap drop, read-only root, ...).
    """
    requested_env = {**request.env, **request.secret_env}
    safe_env = {
        name: value
        for name, value in requested_env.items()
        if _job_environment_value_is_safe(name, value)
    }
    env = {
        # Read-only root: HOME must live on the writable workspace so tools
        # like git can write their config.
        "HOME": request.working_dir,
        **safe_env,
    }
    network_mode = "none" if request.network_policy == "none" else settings.sandbox_network
    if network_mode in {"runner", "engine"}:
        raise JobValidationError("jobs cannot join a control-plane network")
    host_config: dict[str, Any] = {
        "NetworkMode": network_mode,
        "Memory": memory_mb * 1024 * 1024,
        "MemorySwap": memory_mb * 1024 * 1024,  # no swap beyond the cap
        "NanoCpus": int(cpu_limit * 1_000_000_000),
        "PidsLimit": pids_limit,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
        "Privileged": False,
        "Tmpfs": {"/tmp": "rw,size=268435456,mode=1777"},
        "AutoRemove": False,
    }
    if request.workspace_key:
        host_config["Mounts"] = [
            _workspace_volume_mount(request.workspace_key, request.working_dir)
        ]
    else:
        # No persistent workspace requested: still give the job a writable
        # working directory that dies with the container.
        host_config["Tmpfs"][request.working_dir] = "rw,size=268435456,mode=1777"
    return {
        "Image": image,
        "Cmd": list(request.command),
        "Env": [f"{name}={value}" for name, value in env.items()],
        "WorkingDir": request.working_dir,
        "User": "1000:1000",
        "Labels": {JOB_LABEL: request.job_id},
        "HostConfig": host_config,
    }


class JobManager:
    """In-memory job registry + container lifecycle. Durable job records live
    in Postgres and are written by the caller — this registry only needs to
    survive for the duration of a job plus a polling grace period."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._docker: aiodocker.Docker | None = None
        self._jobs: dict[str, JobRecord] = {}
        #: workspace_key -> the job_id currently allowed to touch that disk.
        #: One entry is one held volume; the absence of an entry is the only
        #: thing that lets a job start on it.
        self._workspace_holders: dict[str, str] = {}
        #: invocation_id -> the dispatch of it this runner is answering with.
        #:
        #: Deliberately an index into ``_jobs`` rather than a store of its own,
        #: and therefore with exactly ``_jobs``'s lifetime. Any moment where
        #: this process still remembered a job but had forgotten which
        #: invocation it belonged to would be a moment where it started a
        #: duplicate while holding the proof that it should not, so the two
        #: are not allowed to expire independently. One authority, one
        #: lifetime. It costs a small record beside one that holds up to
        #: 128 KB of captured output.
        #:
        #: In insertion order, which is dispatch order — which is what lets
        #: :meth:`_forget_expired` state exactly what it has forgotten.
        self._invocations: dict[str, _Dispatch] = {}
        #: When this process began serving jobs. Re-stamped by :meth:`start`,
        #: because that is where the previous incarnation's containers are
        #: reaped and therefore where this one's memory honestly begins.
        self._serving_since = datetime.now(UTC)
        #: The newest dispatch this runner has deliberately forgotten, or None
        #: while it has forgotten none.
        #:
        #: A bounded store has to be allowed to drop things, and dropping them
        #: quietly would undo the whole ledger: "I have no record of that
        #: invocation" would go back to meaning "so nothing ran", which is the
        #: inference that repeats an effect. Recording *how far back* the
        #: memory now reaches keeps the inference sound — see
        #: :attr:`_remembering_since`.
        self._forgotten_through: datetime | None = None

    @property
    def docker(self) -> aiodocker.Docker:
        assert self._docker is not None, "JobManager.start() not called"
        return self._docker

    @property
    def serving_since(self) -> datetime:
        """When this process began serving jobs, after reaping its
        predecessor's containers.

        Published on ``GET /v1/runner/memory`` because one caller cannot do its
        job without it. The tool worker's reconciliation sweep reads a 404 for
        a job as "whatever was running this is gone", and that reading is true
        for a job that began *before* this moment — its container was reaped
        here — and false for one that began after, which this process ran,
        finished, and later dropped (:meth:`_forget_expired`). The two silences
        are identical from the outside, so the boundary between them is a fact
        only this process holds, and it is now told rather than assumed.
        """
        return self._serving_since

    @property
    def record_retention_seconds(self) -> float:
        """How long a finished job's record is kept past the later of its
        ending and its own deadline. Published for the same caller: it is what
        turns "you were serving then, and you do not have it" into a date."""
        return max(self._settings.sandbox_job_record_retention_seconds, 0.0)

    @property
    def _remembering_since(self) -> datetime:
        """The moment from which this runner remembers every dispatch it
        accepted.

        Two things can move it, and they are the two ways a memory ends: this
        process not existing yet, and this process letting go. Everything
        after it is covered, which is what makes "I hold no record of that
        invocation" mean "it was never submitted here" — and only then.

        It can run ahead of entries the ledger still holds: retention is
        measured per job, so a long-timeout job outlives a short one submitted
        after it, and forgetting the short one moves this past the long one's
        dispatch. That is the safe direction and not an accident of ordering.
        An invocation this runner still holds is answered from the ledger
        before this is consulted at all; one it does not hold is refused. The
        cost of the overlap is a needless refusal, and the alternative — a
        watermark that lags what has actually been forgotten — is a repeated
        effect.
        """
        if self._forgotten_through is None:
            return self._serving_since
        return max(self._serving_since, self._forgotten_through)

    async def start(self) -> None:
        effective_uid = os.geteuid()
        effective_gid = os.getegid()
        if self._settings.sandbox_docker_mode in {"rootless", "desktop"} and effective_gid != 10001:
            raise DockerSocketConfigurationError(
                f"{self._settings.sandbox_docker_mode} runner requires UID/GID 10001:10001"
            )
        authority_groups = normalize_supplemental_groups(
            effective_gid=effective_gid,
            process_groups=os.getgroups(),
        )
        if (
            self._settings.sandbox_docker_mode == "rootful"
            and self._settings.sandbox_docker_gid == effective_gid
        ):
            authority_groups.add(effective_gid)
        validated_url = validate_docker_authority(
            mode=self._settings.sandbox_docker_mode,
            socket_path=self._settings.sandbox_docker_socket,
            transport_url=self._settings.sandbox_docker_transport_url,
            configured_gid=self._settings.sandbox_docker_gid,
            effective_uid=effective_uid,
            supplemental_groups=authority_groups,
        )
        client = aiodocker.Docker(url=validated_url)
        self._docker = client
        try:
            await asyncio.wait_for(client.version(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
            info = await asyncio.wait_for(
                client.system.info(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS
            )
            security_options = info.get("SecurityOptions", [])
            if self._settings.sandbox_docker_mode == "rootless" and (
                not isinstance(security_options, list) or "name=rootless" not in security_options
            ):
                raise DockerDaemonConfigurationError(
                    "configured rootless Docker daemon is not rootless"
                )
            if self._settings.sandbox_docker_mode == "desktop" and not daemon_is_docker_desktop(
                info
            ):
                raise DockerDaemonConfigurationError(
                    "desktop mode requires a Docker Desktop daemon; use rootful or rootless "
                    "mode for a Linux host daemon"
                )
            await self._ensure_sandbox_network()
            await self.reap_orphans()
            # After the reap, not before: everything the previous incarnation
            # was running has just been force-removed, so this is the first
            # instant at which "I have no record of that invocation" can mean
            # anything other than "I was not here".
            self._serving_since = datetime.now(UTC)
        except BaseException:
            self._docker = None
            with contextlib.suppress(Exception):
                await client.close()
            raise

    async def _ensure_sandbox_network(self) -> None:
        """Create the dedicated job bridge network if compose has not.

        In production no compose service is attached to it (plan 14.4 — the
        sandbox network must carry no control-plane services), so the runner
        owns its creation.
        """
        name = self._settings.sandbox_network
        networks = await self.docker.networks.list(filters={"name": name})
        if not any(entry.get("Name") == name for entry in networks):
            await self.docker.networks.create(
                {"Name": name, "Driver": "bridge", "Labels": {"jhin.sandbox.network": "1"}}
            )
            logger.info("sandbox.network_created")

    async def close(self) -> None:
        """Drain: end every in-flight job properly, then let go of Docker.

        This used to cancel the ``_run`` tasks and close the Docker client in
        the same breath, which is two problems in one line. A cancelled
        ``_run`` reaches its ``finally`` on a later turn of the loop, by which
        time the client it needs to remove the container has been closed
        underneath it; and a cancelled job never becomes anything an operator
        can read — it just stops, mid-status, with the caller left polling a
        record that has no ending.

        So the ordinary cancel path does the work instead. Setting
        ``cancel_requested`` is what a caller's ``POST /cancel`` sets, and
        ``_wait`` already knows what to do with it: kill the container,
        collect its logs, mark the record ``cancelled``, remove the
        container. The job's own poller sees a terminal status and the tool
        call ends as a clean, readable failure rather than a mystery — the
        same ending it would get from any other cancelled job.

        Bounded, because a shutdown is on somebody else's clock: Docker sends
        SIGKILL a short while after SIGTERM. Anything that has not ended when
        the budget runs out is cancelled the old way, and the runner's own
        startup reaping removes whatever container that leaves behind.

        This *does* kill, unlike the tool worker's shutdown, and the asymmetry
        is not an oversight. A worker that is leaving is leaving a runner that
        will still be here to say what the container did; a runner that is
        leaving takes that answer with it and reaps the container at its next
        startup regardless, so killing it now costs nothing and buys the
        caller a terminal status instead of a poll to its deadline. What it
        must not cost is a half-written file, and it cannot: both writing file
        tools stage their content beside the target and rename it into place,
        so a kill at any instant leaves the old file or the new one.
        """
        draining = [
            record
            for record in self._jobs.values()
            if record.task is not None and not record.task.done()
        ]
        if draining and self._docker is not None:
            for record in draining:
                record.cancel_requested = True
                if record.container_id is not None:
                    with contextlib.suppress(Exception):
                        await self.docker.containers.container(record.container_id).kill()
            tasks = [record.task for record in draining if record.task is not None]
            # Each drained job announces itself through the ordinary
            # ``sandbox.job.finished`` record as it ends, with
            # ``outcome=cancelled``, so the drain needs no log line of its own.
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=self._settings.sandbox_drain_timeout_seconds)
        for record in self._jobs.values():
            if record.task is not None and not record.task.done():
                record.task.cancel()
        if self._docker is not None:
            client = self._docker
            self._docker = None
            await client.close()

    async def ping(self) -> bool:
        try:

            async def ping_daemon() -> bool:
                async with self.docker._query("_ping", versioned_api=False) as response:
                    return response.status == 200 and await response.text() == "OK"

            return await asyncio.wait_for(ping_daemon(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
        except Exception:
            return False

    # --- lifecycle ---

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def _pre_start_budget_seconds(self) -> int:
        """The most this runner may spend before a job's container starts.

        Three terms, and they are the three waits that happen with the caller
        already polling and none of which is the job: queuing for the
        workspace volume, the init container's own start allowance, and the
        walk of that volume. The middle two are exactly what
        :meth:`_ensure_workspace_volume` gives the initializer, and the sum
        rounds up, because a partial second of waiting is still a second the
        caller must not spend out of the job's clock.
        """
        return int(
            math.ceil(max(self._settings.sandbox_workspace_queue_seconds, 0.0))
            + math.ceil(DOCKER_CHECK_TIMEOUT_SECONDS)
            + max(self._settings.sandbox_workspace_measure_budget_seconds, 0)
        )

    async def submit(self, request: SandboxJobRequest) -> JobRecord:
        """Start one job — or hand back the one this invocation already has.

        This is where a re-dispatch stops being a second container.

        The problem it solves cannot be solved anywhere else. A tool call
        whose worker dies is re-dispatched with a fresh ``job_id``, so nothing
        on the wire relates the two attempts; the control plane cannot help
        because the fact it needs — what the first container did — is
        precisely the fact its dead worker failed to write down; and the file
        the job edits cannot answer it either, because a file's contents are
        an *effect* and the question is about an *event*. This process is the
        only one that sees both attempts, so it is the only one that can
        answer from knowledge instead of inference. ``invocation_id`` is what
        makes the two attempts recognisable as one thing.

        Three endings, and the middle one is the whole point:

        * **A new invocation** (or none offered) — run it, and remember which
          job this invocation got.
        * **An invocation this runner is already running or has already run**
          — return that record, untouched. Nothing is created, no container
          starts, no workspace is taken. The caller polls the job it is given
          and receives the first dispatch's real outcome: its exit code, its
          output, its refusals. A second dispatch of an edit therefore cannot
          apply the edit twice, and neither can a second dispatch of anything
          else, for the same reason and with no per-tool reasoning anywhere.
        * **An invocation this runner cannot account for** — one whose earlier
          dispatch the caller stamps at or before :attr:`_remembering_since`,
          which is either before this process was serving or before it let go
          of that stretch of its memory. Refuse.
          :class:`InvocationOutcomeUnknownError` explains why that is the only
          honest answer.

        Two things are refused before any of that, and both are about the
        stamp rather than the invocation. A caller that offers an
        ``invocation_id`` but does not state ``prior_dispatch_at`` is refused
        as malformed, because the field's default and its "there was no
        earlier dispatch" answer are the same value and only one of them may
        start a container. And a stamp in this runner's own future by more
        than :data:`_INCARNATION_SKEW` is refused as unanswerable, because it
        is proof that the two clocks the comparison below rests on disagree by
        more than it tolerates.

        The test-and-claim is synchronous from the ``_invocations`` lookup to
        the write, exactly like :meth:`_workspace_is_free`, and on one event
        loop that is what makes it atomic: two dispatches of one invocation
        arriving together cannot both find it absent.

        What is claimed here is an *intention* to run, which is what makes the
        claim atomic — and it is why :meth:`_forget_dispatch` exists to undo
        it for a dispatch that turns out to have run nothing at all. A ledger
        that only ever grows records intentions; this one records outcomes.
        """
        if request.job_id in self._jobs:
            raise JobValidationError(f"job {request.job_id} already exists")
        accepted_at = datetime.now(UTC)
        # Before the lookup, not after: a record this runner is about to stop
        # answering for must not be handed to a dispatch as an outcome, and
        # the invocation it belonged to must be reported as forgotten rather
        # than as never seen.
        self._forget_expired(accepted_at)
        invocation = request.invocation_id
        if invocation:
            dispatched = self._invocations.get(invocation)
            if dispatched is not None:
                existing = self._jobs.get(dispatched.job_id)
                if existing is not None:
                    # No log line. A replay is a durable fact about a tool
                    # call, and it is written where the durable facts about
                    # jobs live: the control plane records
                    # ``attached_to_job_id`` on the dispatch's own audit
                    # event. A second, weaker account of the same thing in
                    # this process's log is the sort of record that drifts.
                    return existing
                # Unreachable while the ledger and the record are dropped
                # together, which :meth:`_forget_expired` is careful to do.
                # Written as a refusal rather than left to fall through
                # because the fall-through is the duplicate effect: an entry
                # naming a job this process no longer holds is proof that the
                # invocation ran and that its outcome is gone, which is
                # exactly what must never be read as "so run it".
                raise InvocationOutcomeUnknownError(
                    "this sandbox runner dispatched this call before and no longer holds "
                    "that job's outcome, so it cannot say what it did; nothing was started"
                )
            if not request.states_prior_dispatch():
                # The schema refuses this for anything arriving over the
                # route; this is the same refusal for anything reaching the
                # manager in process. The field cannot be defaulted here
                # because its default and its "there was no earlier dispatch"
                # answer are the same value, and only one of them may start a
                # container.
                raise JobValidationError(
                    "a job that offers invocation_id must state prior_dispatch_at "
                    "(empty for no earlier dispatch); nothing was started"
                )
            prior = request.prior_dispatch_moment()
            # ``None`` here is the caller's stated claim that this invocation
            # has never been dispatched, which the check above is what makes
            # it. Nothing to compare, and nothing to refuse.
            if prior is not None:
                if prior > accepted_at + _INCARNATION_SKEW:
                    # A dispatch that began in this runner's future. The
                    # caller stamps its row and *then* submits, so on one host
                    # this cannot happen; seeing it means the two clocks
                    # disagree by more than the comparison below tolerates,
                    # and that comparison is the whole interlock. It is worth
                    # catching in exactly this direction: a caller running
                    # ahead is the one that makes a dispatch look newer than
                    # this runner's memory and so gets a second container,
                    # while a caller running behind only causes needless
                    # refusals, which are the safe mistake.
                    raise InvocationOutcomeUnknownError(self._clock_disagreement_reason())
                if prior <= self._remembering_since + _INCARNATION_SKEW:
                    raise InvocationOutcomeUnknownError(self._unaccountable_reason())
        cpu, memory, pids, timeout = resolve_limits(request, self._settings)
        image = request.image or self._settings.sandbox_default_image
        record = JobRecord(
            request=request,
            image=image,
            cpu_limit=cpu,
            memory_mb=memory,
            pids_limit=pids,
            timeout_seconds=timeout,
            created_at=accepted_at,
            pre_start_budget_seconds=self._pre_start_budget_seconds(),
        )
        for value in request.secret_env.values():
            record.redactor.register(value)
        self._jobs[request.job_id] = record
        if invocation:
            self._invocations[invocation] = _Dispatch(job_id=request.job_id, at=accepted_at)
        record.task = asyncio.create_task(self._run(record))
        return record

    def _clock_disagreement_reason(self) -> str:
        """Why a dispatch stamped in this runner's future is refused.

        Named separately from :meth:`_unaccountable_reason` because it is a
        different thing to fix. That one is a fact about time passing and
        needs no action; this one is a misconfigured deployment, and an
        operator who reads "your clocks disagree" goes and looks at the
        clocks.
        """
        return (
            "an earlier dispatch of this call is stamped in this sandbox runner's own "
            "future, so the caller's clock and this runner's disagree by more than the "
            "dispatch interlock tolerates and it cannot say whether that dispatch is one "
            "it would remember; nothing was started. Run the tool worker and the sandbox "
            "runner against the same clock, or keep their hosts NTP-synchronised"
        )

    def _unaccountable_reason(self) -> str:
        """Why an earlier dispatch is beyond this runner's memory, in the
        words of whichever limit it fell outside. Both are true statements
        about the same silence, and an operator reading one of them should not
        have to guess which."""
        if self._forgotten_through is not None and self._forgotten_through > self._serving_since:
            return (
                "an earlier dispatch of this call is older than the oldest job this "
                "sandbox runner still holds, so its outcome has been dropped and this "
                "runner cannot say what it did; nothing was started"
            )
        return (
            "an earlier dispatch of this call began before this sandbox runner "
            "started, so its container was reaped rather than finished and this "
            "runner cannot say what it did; nothing was started"
        )

    def _forget_dispatch(self, record: JobRecord) -> None:
        """Take a dispatch that ran nothing back out of the ledger.

        The ledger's promise is that a second dispatch of an invocation is
        answered with what the first one *did*. A dispatch that gave up in the
        workspace queue did nothing — no init container, no measurement, no
        job container, by :meth:`_hold_workspace`'s own construction — so
        answering with it hands a later dispatch a failure in place of the
        work it was asking for, and the tool call is never run at all. That is
        not idempotency; it is a lost call.

        Deliberately *not* accompanied by a move of
        :attr:`_forgotten_through`. That watermark says "something ran here
        and I no longer hold it", which is the opposite of this: nothing ran,
        so the next dispatch of this invocation should run for real rather
        than be refused.

        Narrow on purpose. The only ending this may be used for is the one
        where the runner can prove nothing happened. Past the workspace queue
        an init container has already touched the disk and a container may
        have been created and started, and a ledger that forgot a job which
        *might* have run would be back to repeating effects.
        """
        invocation = record.request.invocation_id
        if not invocation:
            return
        dispatched = self._invocations.get(invocation)
        if dispatched is not None and dispatched.job_id == record.request.job_id:
            del self._invocations[invocation]

    def _forget_expired(self, now: datetime) -> None:
        """Bound what this process holds, without loosening what it claims.

        Two bounds, because there are two costs. The captured output of a
        finished job is the large one — two streams, capped at
        ``sandbox_max_output_bytes`` each — and it is useful for exactly as
        long as a re-dispatch might still ask for it, so only the most recent
        finished jobs keep it. The record itself is small and is what makes
        the runner's answers honest, so it is kept much longer, and the length
        is not a taste: it has to outlast the tool worker's reconciliation
        sweep (see :meth:`_forgettable`).

        Called from :meth:`submit`, because that is the only moment this store
        grows. An idle runner keeps what it last held, which is already inside
        both bounds.
        """
        self._release_surplus_output()
        for job_id, record in list(self._jobs.items()):
            if not self._forgettable(record, now):
                continue
            del self._jobs[job_id]
            invocation = record.request.invocation_id
            dispatched = self._invocations.get(invocation) if invocation else None
            if dispatched is None or dispatched.job_id != job_id:
                continue
            del self._invocations[invocation]
            # The record and its ledger entry go together — and the fact that
            # they went is kept, because an invocation dropped without a trace
            # would be indistinguishable from one that was never submitted.
            self._forgotten_through = (
                dispatched.at
                if self._forgotten_through is None
                else max(self._forgotten_through, dispatched.at)
            )

    def _release_surplus_output(self) -> None:
        """Keep the captured output of the most recently finished jobs only.

        By count rather than by age, because it is the count that bounds the
        memory: what this protects against is a run of jobs each retaining two
        capped streams for the life of the process. Ordered by when each job
        finished, so "recent" means recently *answered*, not recently asked
        for.
        """
        keep = max(self._settings.sandbox_job_output_retained_jobs, 0)
        finished: list[tuple[datetime, JobRecord]] = [
            (record.finished_at, record)
            for record in self._jobs.values()
            if record.output_retained
            and record.finished_at is not None
            and (record.task is None or record.task.done())
        ]
        if len(finished) <= keep:
            return
        finished.sort(key=lambda entry: entry[0])
        for _, record in finished[: len(finished) - keep]:
            record.release_output()

    def _forgettable(self, record: JobRecord, now: datetime) -> bool:
        """Whether this record may be dropped altogether.

        Two clocks have to have run out, and the second one is the interaction
        that makes this more than a TTL. The tool worker's reconciliation
        sweep (``jhin_tool_worker.sandbox_reconcile``) closes a ``sandbox_job``
        row whose worker died by asking this runner about the job, and a 404
        for a job this process finished and then dropped must not be read as
        "nothing will ever report an outcome". Asked too early it would record
        a job that *completed* as ``runner_gone``.

        So a record outlives the moment that sweep can reach it: the row's own
        deadline (``started_at`` plus its timeout, and this record's
        ``created_at`` is that same instant) plus the retention window, which
        is configured to exceed the sweep's grace and its interval together.

        **That window buys the ordinary case, and it cannot buy the other
        one.** It is sized against a sweep that is *running*; a worker that is
        down — crash-looping, or simply stopped for an afternoon — sweeps
        nothing, and comes back to ask about rows that are now hours old. No
        finite retention covers a caller that is absent for longer than it, so
        the sweep no longer rests on this window at all: it asks what this
        runner's memory *covers* (:attr:`serving_since`,
        :attr:`record_retention_seconds`) and declines to read a 404 as proof
        outside it. This bound is then free to be what it is — a bound on
        memory — instead of a promise to somebody else's clock.
        """
        if record.finished_at is None:
            return False
        if record.task is not None and not record.task.done():
            return False
        horizon = timedelta(seconds=max(self._settings.sandbox_job_record_retention_seconds, 0.0))
        reconcile_reach = record.created_at + timedelta(seconds=record.timeout_seconds)
        return now >= max(record.finished_at, reconcile_reach) + horizon

    async def cancel(self, job_id: str) -> JobRecord | None:
        record = self._jobs.get(job_id)
        if record is None:
            return None
        record.cancel_requested = True
        if record.status == "running" and record.container_id is not None:
            try:
                container = self.docker.containers.container(record.container_id)
                await container.kill()
            except DockerError:
                pass  # already gone
        return record

    def _workspace_is_free(self, record: JobRecord) -> bool:
        """Take the workspace if nothing holds it. Synchronous on purpose.

        The test and the claim are one statement with no ``await`` between
        them, which on a single event loop is what makes them atomic: two
        waiters cannot both read "free" and both write themselves in.
        """
        key = record.request.workspace_key
        holder = self._workspace_holders.get(key)
        if holder is not None and holder != record.request.job_id:
            return False
        self._workspace_holders[key] = record.request.job_id
        return True

    async def _hold_workspace(self, record: JobRecord) -> bool:
        """Wait for this job's workspace, and say whether it got it.

        Bounded, because waiting is not free either: the caller polling this
        job has its own deadline, and a queue that outlived it would start a
        container for a tool call nobody is listening to any more. That
        deadline now *covers* this budget rather than being eaten by it — the
        caller is told the pre-start budget on the submit response — so the
        bound is about the wait being worth having, not about staying inside
        somebody else's minute. A job that gives up here has run nothing at
        all — no init container, no measurement, no job container — which is
        what makes the refusal a clean one to report.
        """
        budget = max(self._settings.sandbox_workspace_queue_seconds, 0.0)
        deadline = asyncio.get_running_loop().time() + budget
        while True:
            if record.cancel_requested:
                return False
            if self._workspace_is_free(record):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                record.error = (
                    f"another job is still using workspace {record.request.workspace_key}; "
                    f"this one waited {budget:g}s for it and never started"
                )
                return False
            await asyncio.sleep(_WORKSPACE_POLL_SECONDS)

    def _release_workspace(self, record: JobRecord) -> None:
        if self._workspace_holders.get(record.request.workspace_key) == record.request.job_id:
            del self._workspace_holders[record.request.workspace_key]

    async def _run(self, record: JobRecord) -> None:
        request = record.request
        container = None
        timed_out = False
        terminal_status = "failed"
        held_workspace = False
        ran_nothing = False
        captures: list[asyncio.Task[None]] = []
        try:
            if request.workspace_key:
                held_workspace = await self._hold_workspace(record)
                if not held_workspace:
                    # Nothing ran. Cancelled while queued is a cancellation;
                    # anything else is the wait giving up, and ``record.error``
                    # already says which disk and for how long.
                    terminal_status = "cancelled" if record.cancel_requested else "failed"
                    ran_nothing = True
                    return
            record.status = "running"
            record.started_at = datetime.now(UTC)
            if request.workspace_key:
                size, partial = await self._ensure_workspace_volume(
                    request.workspace_key,
                    job_id=request.job_id,
                )
                record.workspace_size_bytes = size
                record.workspace_size_partial = partial
            config = build_container_config(
                request,
                self._settings,
                image=record.image,
                cpu_limit=record.cpu_limit,
                memory_mb=record.memory_mb,
                pids_limit=record.pids_limit,
            )
            container = await self.docker.containers.create(
                config, name=f"jhin-sbx-{request.job_id[:32]}"
            )
            record.container_id = container.id
            await container.start()
            record.container_started_at = datetime.now(UTC)
            captures = [
                asyncio.create_task(self._capture_stream(record, container, stream))
                for stream in ("stdout", "stderr")
            ]
            timed_out = await self._wait(record, container)
            info = await container.show()
            record.exit_code = int(info.get("State", {}).get("ExitCode", -1))
            # Docker closes each following stream when the process exits.
            # A broken log connection must never hold up the command result.
            await asyncio.wait(captures, timeout=5.0)
            if record.cancel_requested:
                terminal_status = "cancelled"
            elif timed_out:
                terminal_status = "timeout"
                record.error = f"job exceeded its {record.timeout_seconds}s timeout and was killed"
            else:
                terminal_status = "completed"
        except DockerError as exc:
            record.error = record.redactor.redact_text(f"docker error: {exc.message}")[:2000]
        except Exception as exc:  # infrastructure failure — never secrets
            record.error = record.redactor.redact_text(f"{type(exc).__name__}: {exc}")[:2000]
        finally:
            for capture in captures:
                if not capture.done():
                    capture.cancel()
            if captures:
                await asyncio.gather(*captures, return_exceptions=True)
            # Ephemeral always (plan 14.1): the container is force-removed no
            # matter how the job ended.
            if container is not None:
                # Startup reaping is the backstop if this delete fails.
                with contextlib.suppress(DockerError):
                    await container.delete(force=True, v=True)
            # After the container is gone, never before: the next job on this
            # disk must not start while this one still has a process on it.
            if held_workspace:
                self._release_workspace(record)
            record.finished_at = datetime.now(UTC)
            record.status = terminal_status
            if ran_nothing:
                # This dispatch is not an answer about the invocation, so it
                # does not get to be the answer. The record stays — the caller
                # polling it is owed its ending — but a later dispatch of the
                # same call runs for real instead of being handed this.
                self._forget_dispatch(record)
            logger.info(
                "sandbox.job.finished",
                job_id=request.job_id,
                outcome=normalize_sandbox_outcome(record.status),
                exit_code=max(0, record.exit_code or 0),
                network_policy=request.network_policy,
            )

    async def _wait(self, record: JobRecord, container: Any) -> bool:
        """Poll until exit/cancel/timeout. Returns True when timed out.

        Polling (instead of the blocking wait endpoint) keeps long jobs
        immune to HTTP client read timeouts and lets cancellation act fast.

        The clock is the container's own start, not the job's. Everything
        between the two — waiting for the workspace, walking it to measure it
        — is this runner's housekeeping, and billing it to the job's timeout
        meant a job could be killed for running over a budget most of which
        had been spent before it existed.
        """
        assert record.container_started_at is not None
        while True:
            info = await container.show()
            if not info.get("State", {}).get("Running", False):
                return False
            elapsed = (datetime.now(UTC) - record.container_started_at).total_seconds()
            if elapsed >= record.timeout_seconds:
                with contextlib.suppress(DockerError):
                    await container.kill()
                return True
            if record.cancel_requested:
                with contextlib.suppress(DockerError):
                    await container.kill()
                return False
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _capture_stream(self, record: JobRecord, container: Any, stream: str) -> None:
        """Keep one bounded raw tail, publishing only sanitized snapshots.

        The overlap allows redaction to see a whole secret across chunks or
        the truncation boundary. No complete log is accumulated in memory.
        """
        max_bytes = self._settings.sandbox_max_output_bytes
        max_chars = max_bytes + record.redactor.max_secret_length
        raw = ""
        clipped = False
        try:
            async for chunk in container.log(**{stream: True}, follow=True):
                raw += chunk
                if len(raw) > max_chars:
                    raw = raw[-max_chars:]
                    clipped = True
                safe = record.redactor.redact_partial_text(raw, clipped_start=clipped)
                text, truncated = self._sanitize(record, safe, max_bytes)
                setattr(record, stream, text)
                setattr(record, f"{stream}_truncated", clipped or truncated)
            # EOF alone does not prove the process exited; a disconnected
            # logger must not flush an unfinished secret into the snapshot.
            text, truncated = self._sanitize(
                record, record.redactor.redact_partial_text(raw, clipped_start=clipped), max_bytes
            )
            setattr(record, stream, text)
            setattr(record, f"{stream}_truncated", clipped or truncated)
        except asyncio.CancelledError:
            setattr(record, f"{stream}_truncated", True)
            raise
        except Exception:
            # Leave the last safe snapshot available; output failure cannot
            # change whether a command ran or cause another dispatch.
            setattr(record, f"{stream}_truncated", True)

    @staticmethod
    def _sanitize(record: JobRecord, text: str, max_bytes: int) -> tuple[str, bool]:
        redacted = record.redactor.redact_text(text)
        if len(redacted.encode()) <= max_bytes:
            return redacted, False
        # Keep the tail: for build/test output the end is what matters.
        clipped = redacted.encode()[-max_bytes:].decode(errors="ignore")
        return clipped + _TRUNCATION_MARKER, True

    async def current_logs(self, job_id: str) -> tuple[str, str, bool, bool] | None:
        """(stdout, stderr, stdout_truncated, stderr_truncated) — the stored
        output for finished jobs, a live (redacted, capped) read for running
        ones."""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        return record.stdout, record.stderr, record.stdout_truncated, record.stderr_truncated

    # --- workspaces ---

    @staticmethod
    def _parse_measurement(logs: str) -> tuple[int | None, bool]:
        """The two lines the init program prints, or nothing.

        Exactly one ``JHIN_WS_BYTES`` line is required. Two is ambiguous and a
        stream carrying two is read as carrying none, for the same reason the
        CLI connector's trailer refuses a duplicated sentinel: resolving the
        ambiguity by "last one wins" hands the decision to whoever printed
        last.
        """
        found = _WORKSPACE_BYTES_RE.findall(logs)
        if len(found) != 1:
            return None, False
        partial = _WORKSPACE_PARTIAL_RE.findall(logs)
        return int(found[0]), len(partial) == 1 and partial[0] == "1"

    @staticmethod
    async def _read_measurement(container: Any) -> tuple[int | None, bool]:
        """The init container's two lines, off the container's own log stream.

        Takes the container as a parameter rather than reading it inline, the
        same shape ``_collect_logs`` uses: ``.log()`` here is the aiodocker
        container API and not a logger, and the logging audit recognises that
        by the annotated parameter rather than by taking anyone's word for it.
        """
        return JobManager._parse_measurement("".join(await container.log(stdout=True)))

    async def _ensure_workspace_volume(
        self, workspace_key: str, *, job_id: str
    ) -> tuple[int | None, bool]:
        """Create (or adopt) the volume, chown it, and measure it.

        Returns ``(size_bytes, partial)``, where ``partial`` says the number is
        a floor rather than a size. ``(None, False)`` means this job took no
        measurement at all, which the control plane reads as "leave the stored
        number alone" rather than as "the workspace is empty" — and a job that
        was *supposed* to measure never returns it: a measurement that could
        not be read comes back as ``(0, True)``, a floor of zero, because
        "the walk did not answer" and "the walk did not finish" are the same
        thing to a caller that has to decide whether it knows this disk's
        size.

        Both kinds of workspace are measured on every job. The agent kind has
        to be, because this number is the only bound its disk has. The run
        kind used to be walked once every ten minutes on the grounds that it
        dies with its run — but its bytes are on the same host and count
        against the same tenant budget, and a stale number for a disk that
        counts is the thing this measurement is here to stop.

        There is deliberately no size option on the volume, and it is worth
        being exact about why, because the measure below is the only bound
        this disk has. Docker's ``local`` driver takes a ``size`` option, and
        on this install asking for one is refused by the daemon in as many
        words -- ``quota size requested but no quota support`` -- because the
        storage driver is overlayfs over ext4 and quota support means an xfs
        filesystem mounted with ``pquota``. It enforces a size without that
        only for ``type=tmpfs``, which is RAM and therefore the one thing a
        workspace holding a clone and a build tree must not be;
        ``HostConfig.StorageOpt`` bounds a container's writable layer, not a
        mounted volume, and needs the same xfs project quotas. So there is no
        filesystem quota to fall back on here, and this is not a case of
        preferring accounting to one.

        What that leaves is an accounting cap: measured here, stored on the
        lease row, and enforced by the control plane at bind time (refuse) and
        on the next bind (recycle). It is a sufficient bound only as long as
        the number is the disk's real usage, which is why the walk counts
        blocks rather than apparent size, descends by directory file
        descriptor, and counts a multiply-linked file once. Each of those was
        a way the number stopped being the disk's usage while still looking
        like one: an apparent-size total lagged the truth by a factor of
        262144 on a single ``fallocate -n``; an ``lstat`` on an assembled path
        dropped every file below 4 KiB of ancestry, reporting 6 GiB as 90112
        bytes; and summing every link read 200 links to a 50 MB file as
        10.5 GB, which is the direction that gets an agent's tree deleted.

        And it is only a bound while the walk *finishes*, which is the fourth
        way this number stopped being the disk's usage: a walk that runs out
        of its budget reports what it had counted so far, which on a wide
        enough tree is a rounding error. That is why an unfinished walk is
        reported as a floor and enforced as "unknown" rather than as a size.
        The budget is generous enough that finishing is the ordinary case --
        three million entries walk in about 16 seconds on this host -- and the
        cost of not finishing is a refused call rather than a silent hole.

        It is measured on every job rather than on an interval, so the worst
        case is one job's overrun rather than one interval's; within that one
        job nothing stops a container filling the host's disk, and the honest
        statement of the bound is "one job", not "5 GiB".
        """
        kind = self.workspace_kind({"Labels": {WORKSPACE_LABEL: workspace_key}})
        measure = True
        await self.docker.volumes.create(
            {
                "Name": workspace_volume_name(workspace_key),
                "Labels": {WORKSPACE_LABEL: workspace_key, WORKSPACE_KIND_LABEL: kind},
            }
        )
        initializer = await self.docker.containers.create(
            {
                "Image": self._settings.sandbox_default_image,
                "Entrypoint": ["python3", "-c"],
                "Cmd": [_WORKSPACE_INIT_SCRIPT],
                "User": "0:0",
                "Env": [
                    f"JHIN_WS_MEASURE={'1' if measure else '0'}",
                    f"JHIN_WS_BUDGET={self._settings.sandbox_workspace_measure_budget_seconds}",
                ],
                "Labels": {WORKSPACE_INIT_LABEL: workspace_key},
                "HostConfig": {
                    "NetworkMode": "none",
                    # Enough for the walk's inode table at its bound (two
                    # million multiply-linked files, ~130 MB of set) with room
                    # for the interpreter. The alternative to the memory is a
                    # smaller table, and a table that fills makes the walk
                    # partial, which now costs the agent its next call.
                    "Memory": 256 * 1024 * 1024,
                    "MemorySwap": 256 * 1024 * 1024,
                    "NanoCpus": 1_000_000_000,
                    "PidsLimit": 16,
                    "CapDrop": ["ALL"],
                    # DAC_READ_SEARCH only on the jobs that measure, and only
                    # because the two lines above it hand the directory to uid
                    # 1000 at mode 0700 before this root process has to read
                    # it. Read and search; no write, no ownership, no exec.
                    "CapAdd": (
                        ["CHOWN", "FOWNER", "DAC_READ_SEARCH"] if measure else ["CHOWN", "FOWNER"]
                    ),
                    "SecurityOpt": ["no-new-privileges:true"],
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "AutoRemove": False,
                    "Mounts": [_workspace_volume_mount(workspace_key, _WORKSPACE_INIT_TARGET)],
                },
            },
            name=f"jhin-sbx-init-{job_id[:32]}",
        )
        delete_failed = False
        size: int | None = None
        partial = False
        try:
            await initializer.start()
            wait_for = DOCKER_CHECK_TIMEOUT_SECONDS + (
                self._settings.sandbox_workspace_measure_budget_seconds if measure else 0
            )
            result = await initializer.wait(timeout=wait_for)
            status_code = result.get("StatusCode") if isinstance(result, dict) else None
            if type(status_code) is not int or status_code != 0:
                raise RuntimeError("sandbox workspace initialization failed")
            if measure:
                # Read before the container is removed. A measurement that
                # cannot be read is a floor of zero rather than no measurement
                # at all: this job was supposed to answer "how big is this
                # disk" and did not, and reporting nothing would leave the
                # previous answer standing as though it were still current.
                size, partial = 0, True
                with contextlib.suppress(Exception):
                    read_size, read_partial = await self._read_measurement(initializer)
                    if read_size is not None:
                        size, partial = read_size, read_partial
        finally:
            try:
                await initializer.delete(force=True, v=True)
            except Exception:
                delete_failed = True
        if delete_failed:
            raise RuntimeError("sandbox workspace initializer cleanup failed")
        return size, partial

    @staticmethod
    def workspace_kind(entry: dict[str, Any]) -> str:
        """What kind of workspace a volume is, from its label — or, for a
        volume created before the label existed, from its key.

        Every volume that predates this label is run-scoped (that was the only
        kind there was), so the fallback answers correctly for all of them, and
        defaults to ``run`` for anything it cannot read: reaping a stale
        run volume is the behaviour that already shipped, while keeping an
        agent volume alive is the new promise, and a guess should fall on the
        side of the old behaviour rather than silently retaining disk forever.
        """
        labels = entry.get("Labels") or {}
        kind = str(labels.get(WORKSPACE_KIND_LABEL) or "") if isinstance(labels, dict) else ""
        if kind in {WORKSPACE_KIND_AGENT, WORKSPACE_KIND_RUN, "conversation", "delegated"}:
            return kind
        key = str(labels.get(WORKSPACE_LABEL) or "") if isinstance(labels, dict) else ""
        if key.startswith(f"{WORKSPACE_KIND_AGENT}-"):
            return WORKSPACE_KIND_AGENT
        if key.startswith("conversation-"):
            return "conversation"
        if key.startswith("delegated-"):
            return "delegated"
        return WORKSPACE_KIND_RUN

    async def delete_workspace(self, workspace_key: str) -> bool:
        """True when the volume is *gone*, which is the only fact a caller can
        act on.

        Deleted and never-existed are the same answer to "is this disk still
        here", and both are True: an operator's reset of a workspace whose
        volume was already removed has nothing left to do. Everything else is
        False, and the one that matters is ``409 conflict`` -- Docker refuses
        to remove a volume some container still has mounted, and a caller that
        reads that as success clears a reset request, records ``size_bytes=0``
        for a disk that is still full, and returns it to service invisible to
        the cap. So the two are separated here rather than collapsed into one
        ``except DockerError``.
        """
        name = workspace_volume_name(workspace_key)
        try:
            volume = await self.docker.volumes.get(name)
        except DockerError as error:
            return error.status == 404
        try:
            await volume.delete()
        except DockerError as error:
            return error.status == 404
        return True

    # --- startup reaping ---

    async def reap_orphans(self) -> None:
        """Remove leftover job containers, and stale *run-scoped* workspace
        volumes, from a previous runner process that died mid-job.

        **This reaps by label, not by ownership, and the difference matters if
        there is ever more than one runner on a daemon.** Every container
        carrying ``jhin.sandbox.job`` is force-removed, including one a
        *different* live runner started a second ago: nothing on the container
        says which process made it, and nothing could — an identity minted at
        startup would not survive the restart this sweep exists for, and a
        stable one would have to be configured. One runner per daemon is what
        the compose topology gives (a single ``sandbox-runner`` service on the
        ``runner`` network), and the same assumption is already load-bearing
        one wall out: ``jhin_tool_worker.sandbox_reconcile`` reads a 404 from
        *the* runner as proof that a job is gone, which behind a pool would
        prove nothing. Putting a second runner on this daemon means giving
        both a configured identity, labelling jobs with it, and filtering both
        this sweep and that one on it — not just widening a deployment.

        Agent-kind volumes are deliberately never reaped by age. Creation age
        is not use age, so an age sweep would wipe a healthy agent's workspace
        every 24 hours — which is the bug durable workspaces exist to fix.
        Their eviction is idle- and size-driven and lives in the control plane,
        the only place that knows when a workspace was last used: Docker labels
        are immutable after create, so last-use cannot live on the volume.
        """
        seen_containers: set[str] = set()
        reaped_containers = 0
        for label in (JOB_LABEL, WORKSPACE_INIT_LABEL):
            containers = await self.docker.containers.list(
                all=1, filters=json.dumps({"label": [label]})
            )
            for container in containers:
                identifier = str(container.id)
                if identifier in seen_containers:
                    continue
                seen_containers.add(identifier)
                await container.delete(force=True, v=True)
                reaped_containers += 1
        if reaped_containers:
            logger.info("sandbox.reaped_container", count=reaped_containers)

        max_age = self._settings.sandbox_workspace_max_age_hours * 3600
        listing = await self.docker.volumes.list(filters={"label": [WORKSPACE_LABEL]})
        reaped_workspaces = 0
        for entry in listing.get("Volumes") or []:
            if self.workspace_kind(entry) != WORKSPACE_KIND_RUN:
                continue
            created = entry.get("CreatedAt", "")
            age = (datetime.now(UTC) - datetime.fromisoformat(created)).total_seconds()
            if age > max_age:
                volume = await self.docker.volumes.get(entry["Name"])
                await volume.delete()
                reaped_workspaces += 1
        if reaped_workspaces:
            logger.info("sandbox.reaped_workspace", count=reaped_workspaces)
