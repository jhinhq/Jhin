"""Wire schemas of the internal runner API (plan 14.2).

Design decision (documented in docs/architecture/sandboxing.md): the caller
resolves credentials and sends short-lived plaintext values in ``secret_env``
over the internal runner network, instead of the runner resolving secret
refs itself. That keeps the master key out of this service entirely — the
component that holds the Docker socket holds no key material at rest.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NetworkPolicy = Literal["none", "internet"]

_JOB_ID_RE = re.compile(r"^[a-f0-9-]{8,64}$")
# The shape of every workspace key, applied to the job schema's field and
# to the DELETE route's path parameter: a name that reaches ``volumes.get()``
# unvalidated is not a shape to leave lying around.
WORKSPACE_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SandboxJobRequest(BaseModel):
    """One job = one fresh ephemeral container (plan 14.2)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    #: Who is asking, as opposed to which attempt is asking.
    #:
    #: ``job_id`` is minted fresh for every dispatch, so two dispatches of the
    #: same tool call are two unrelated jobs as far as this service can see —
    #: which is exactly the blind spot that let one call's container run
    #: twice. This carries the caller's own identity for the *invocation*
    #: (Jhin's ``tool_call.id``): stable across a re-dispatch, distinct for
    #: every genuinely new call. The runner keys its idempotency on it.
    #:
    #: Empty means "no invocation identity offered", and such a job gets no
    #: idempotency at all. That is the honest default for a caller that has
    #: nothing stable to name.
    invocation_id: str = ""
    #: When the earliest dispatch of this invocation began, ISO-8601 UTC, or
    #: empty when there has been none.
    #:
    #: It exists to answer the one question the runner cannot answer from its
    #: own memory: an invocation it has never heard of is either brand new or
    #: one it *forgot*, and those need opposite treatment. Comparing this
    #: against the moment from which this runner remembers every dispatch it
    #: accepted settles it — see
    #: :meth:`jhin_sandbox_runner.jobs.JobManager.submit`.
    #:
    #: **Empty is a claim, not a shrug.** It says the caller established that
    #: no earlier dispatch of this invocation exists; it does not mean "I
    #: could not find out", because this field is the only interlock left once
    #: the runner's own ledger has been emptied by a restart, and a caller
    #: that answers an unanswerable question with the empty string is asking
    #: for the effect to be applied twice. The caller keeps the two apart in
    #: its own types and refuses rather than submitting — see
    #: ``jhin_connectors.cli.tools._dispatch_history``.
    #:
    #: **And omitting it is not a way to say "none" either.** The default
    #: below exists for callers that offer no ``invocation_id``, which never
    #: reach the ledger and whose value here is never read. Offer an
    #: invocation and the field becomes required — see
    #: :meth:`_prior_dispatch_is_stated`. Shape validation cannot tell a
    #: deliberate "there was no earlier dispatch" from a client that has never
    #: heard of the question, and those are the two answers the whole
    #: interlock turns on; today only one caller exists and it always answers,
    #: so the way a second one stops being able to break this silently is that
    #: the schema refuses it loudly.
    prior_dispatch_at: str = ""
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

    @field_validator("invocation_id")
    @classmethod
    def _invocation_id_shape(cls, value: str) -> str:
        if value and not _JOB_ID_RE.match(value):
            raise ValueError("invocation_id must be a lowercase hex/uuid-like token")
        return value

    @field_validator("prior_dispatch_at")
    @classmethod
    def _prior_dispatch_shape(cls, value: str) -> str:
        # Parsed here so nothing downstream has to decide what an unparseable
        # timestamp means. A caller that offers one offers a usable one.
        if not value:
            return value
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("prior_dispatch_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("prior_dispatch_at must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _prior_dispatch_is_stated(self) -> SandboxJobRequest:
        """A caller that asks for idempotency must answer the question the
        idempotency rests on.

        ``prior_dispatch_at`` is read for exactly one decision and only when
        an ``invocation_id`` is offered: whether an invocation this runner has
        no record of is one it never saw, or one it forgot when it restarted.
        A client that omits the field gets the default, and the default is
        indistinguishable on the wire from "I established that there was no
        earlier dispatch" — which is the answer that makes the runner start a
        container. So an omission is refused here rather than read as that
        answer, and a second client of ``POST /v1/jobs`` finds out at its
        first request instead of at its first duplicated edit.

        A caller offering no invocation is unaffected: it gets no ledger
        entry, it is never asked to be recognised as a repeat, and this field
        is never consulted for it. That is what keeps an older tool worker —
        which sends neither field — working against a newer runner.
        """
        if self.invocation_id and "prior_dispatch_at" not in self.model_fields_set:
            raise ValueError(
                "prior_dispatch_at is required when invocation_id is set: send the "
                "moment the earliest dispatch of this invocation began, or an empty "
                'string to state that there was none. Omitting it would read as "no '
                'earlier dispatch" and start a second container for a call that has '
                "already run once"
            )
        return self

    def states_prior_dispatch(self) -> bool:
        """Whether the caller answered the prior-dispatch question at all.

        Re-asked at the point of use so the interlock does not rest on
        validation having run — :meth:`jhin_sandbox_runner.jobs.JobManager
        .submit` is reachable in-process, not only through the route.
        """
        return "prior_dispatch_at" in self.model_fields_set

    def prior_dispatch_moment(self) -> datetime | None:
        """``prior_dispatch_at`` as an aware datetime, or ``None``.

        ``None`` is the caller's *stated* claim that there was no earlier
        dispatch, never an unanswered question: an unanswered one cannot get
        this far past :meth:`_prior_dispatch_is_stated`, and the caller in
        front of it has no way to spell "I do not know" at all.
        """
        if not self.prior_dispatch_at:
            return None
        return datetime.fromisoformat(self.prior_dispatch_at).astimezone(UTC)

    @field_validator("workspace_key")
    @classmethod
    def _workspace_key_shape(cls, value: str) -> str:
        if value and not WORKSPACE_KEY_RE.match(value):
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
    #: The invocation this job belongs to, echoed back.
    #:
    #: It is how a caller can tell that its submit *attached* to an earlier
    #: dispatch rather than starting anything: the answer carries a
    #: ``job_id`` the caller did not send, for the ``invocation_id`` it did.
    invocation_id: str = ""
    #: The most this runner may spend on a job before its container starts:
    #: waiting for the workspace volume to come free, plus walking that volume
    #: to measure it.
    #:
    #: Reported rather than assumed, because it is the runner's number and the
    #: caller's deadline. A caller that hard-codes a copy of it is a caller
    #: that cancels legitimate work the day somebody widens one of these
    #: budgets here — which is exactly what happened when the measurement
    #: budget went from five seconds to sixty.
    pre_start_budget_seconds: int = 0
    # ``queued`` is accepted and never terminal: the job holds no workspace
    # yet, so no container of its exists. A poller treats it exactly as it
    # treats ``running`` — keep asking — which is why adding it changes no
    # caller, and saying ``running`` for a job with no container would have
    # been the only alternative.
    status: Literal["queued", "running", "completed", "failed", "timeout", "cancelled"]
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
    # What this job's init container counted on the workspace. ``None`` means
    # this job took no measurement (no workspace, or the volume step never
    # ran), and is read as "leave the stored number alone" rather than as
    # "empty". ``partial`` marks a walk that stopped early — the budget ran
    # out, an entry could not be read — which makes the number a *floor* and
    # the workspace's size unknown, not merely approximate. The control plane
    # refuses an agent's next call on an unknown size rather than enforcing a
    # cap against a number that is not the disk's.
    workspace_size_bytes: int | None = None
    workspace_size_partial: bool = False


class RunnerMemoryResponse(BaseModel):
    """What this runner process can still be asked about.

    Two facts and no job in sight, because the caller that needs them is
    asking about a job the runner has just said it does not have. A 404 means
    two different things — "I was not here when that job ran, so its container
    was reaped" and "I ran it, finished it, and have since let the record go" —
    and only the first is proof that no outcome is coming. The tool worker's
    reconciliation sweep tells them apart from these:

    * ``serving_since`` — when this process began serving, *after* it
      force-removed the previous incarnation's containers. A job that started
      before it was killed by that reap.
    * ``job_record_retention_seconds`` — how long a finished job's record is
      kept past the later of its ending and its own deadline. Inside that, a
      404 is proof the job was never submitted here; outside it, the record
      may simply have been dropped and the runner is no longer a witness.

    Both are read rather than hard-coded on the other side on purpose: they
    are this service's configuration, and a copy of a number in another
    repository directory is a copy that goes stale the day somebody widens it.
    """

    model_config = ConfigDict(frozen=True)

    serving_since: str
    job_record_retention_seconds: float


class SandboxLogsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
