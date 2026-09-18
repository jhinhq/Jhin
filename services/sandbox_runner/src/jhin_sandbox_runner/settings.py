"""Runner configuration (plan 14.3 caps, section 39 env names).

Everything here is infrastructure configuration; the runner never sees the
master key, database credentials, or long-lived user secrets — job env
values arrive per request over the internal ``runner`` network and die with
the job.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings
from jhin_sandbox_runner.docker_socket import (
    ROOTLESS_TRANSPORT_URL,
    DockerSocketMode,
)


class Settings(ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

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
    # How many finished jobs keep the output their container produced.
    #
    # This is the bound on the runner's largest retained thing: two streams of
    # at most ``sandbox_max_output_bytes`` each, which used to be held for the
    # life of the process — forty jobs of ordinary build output measured about
    # 340 KB retained apiece and moved the runner's RSS by 14 MiB with nothing
    # running. What the output is still *for* once a job has ended is a
    # re-dispatch of that same tool call being handed the first dispatch's
    # answer, which arrives while a worker restarts rather than hours later.
    # Thirty-two of them is minutes of ordinary work and about 4 MB.
    #
    # A job whose output has been released still answers with its status, its
    # exit code and its timings; only the two streams are gone, and they say
    # so rather than reading as empty.
    sandbox_job_output_retained_jobs: int = 32
    # How long a *finished* job stays answerable at all, past the later of its
    # own ending and the deadline its caller measures it against.
    #
    # Sized for the tool worker's reconciliation sweep, which closes a
    # ``sandbox_job`` row whose worker died by asking this runner about the
    # job: it looks at a row 300s past its deadline
    # (``DEFAULT_OVERDUE_GRACE_SECONDS``) and runs every 300s
    # (``tool_worker_sandbox_sweep_seconds``), so a record that outlives 600s
    # of that is one the sweep can still get a real answer out of. An hour
    # leaves the margin generous in the direction that costs nothing: a
    # stripped record is a few hundred bytes, and an hour of them is a rounding
    # error beside one job's output.
    #
    # What this number is *not* is the thing that keeps the sweep honest. It
    # cannot be: no window covers a sweep that is not running, and a worker
    # down for an afternoon comes back to rows hours past their deadline. So
    # the sweep reads a 404 against what this runner says its memory covers
    # (``GET /v1/runner/memory``, which publishes this very number) rather than
    # against an assumption about it — see ``JobManager._forgettable``. Tuning
    # it down therefore costs answers, not correctness.
    sandbox_job_record_retention_seconds: float = 3600.0
    # Startup reaping: RUN-scoped workspace volumes older than this are
    # removed. Agent-scoped volumes are never reaped by age — see
    # JobManager.reap_orphans.
    sandbox_workspace_max_age_hours: int = 24
    # How long the workspace walk may take. Every job walks its workspace —
    # the walk rides the root init container that already runs there — so this
    # is the only knob, and what it buys is the difference between a size and
    # a refusal: a walk that does not finish reports a floor, and the control
    # plane refuses the agent's call rather than enforcing a cap against a
    # number that is not the disk's.
    #
    # Sixty seconds where it used to be five, because five could not finish a
    # tree an agent can build in seventy: three million entries measure in
    # about sixteen seconds on this host, and at five they measured 0.3% of a
    # 6.5 GB disk and reported it complete enough to act on. It costs nothing
    # on an ordinary tree, which finishes in well under a second and stops —
    # the budget is a ceiling, not a duration.
    sandbox_workspace_measure_budget_seconds: int = 60
    # How long shutdown waits for in-flight jobs to end properly (kill the
    # container, collect its logs, mark the record ``cancelled``) before
    # abandoning them. Under Docker's default ten-second stop grace this has
    # to leave room for the HTTP server to close as well, so it is small on
    # purpose: draining is worth a few seconds, and a job that needs longer
    # than that was never going to survive the SIGKILL either.
    sandbox_drain_timeout_seconds: float = 5.0
    # How long a job waits for another job to let go of its workspace volume.
    # A workspace carries at most one job at a time (see ``jobs.py``).
    #
    # This used to be the answer to a re-dispatched tool call arriving while
    # the first dispatch's container was still on the disk, and it was a bad
    # one: waiting for the first container and then running a second is how
    # an edit got applied twice. That case is now settled where it belongs, at
    # the invocation (``JobManager.submit``), and what is left here is
    # genuinely concurrent work — two calls that are not the same call and
    # both want the same disk.
    #
    # Thirty seconds covers the ordinary file tool and most of a checkout;
    # past that the caller is better served by a job that ran nothing and said
    # so than by one that starts long after it was asked for. It is spent
    # before the job's container exists, so it is reported to the caller in
    # ``pre_start_budget_seconds`` and covered by the caller's own deadline
    # rather than taken out of it.
    sandbox_workspace_queue_seconds: float = 30.0

    @model_validator(mode="after")
    def validate_docker_mode(self) -> Settings:
        if self.sandbox_docker_mode == "rootless":
            if self.sandbox_docker_socket is not None or self.sandbox_docker_gid is not None:
                raise ValueError("rootless mode accepts no socket path or socket GID")
            if self.sandbox_docker_transport_url != ROOTLESS_TRANSPORT_URL:
                raise ValueError("rootless mode requires the exact private transport URL")
            return self

        if self.sandbox_docker_mode == "desktop":
            if self.sandbox_docker_socket is None:
                raise ValueError("desktop mode requires a Docker socket path")
            if not self.sandbox_docker_socket.is_absolute():
                raise ValueError("desktop Docker socket path must be absolute")
            if self.sandbox_docker_gid is not None:
                raise ValueError("desktop mode accepts no socket GID")
            if self.sandbox_docker_transport_url is not None:
                raise ValueError("desktop mode accepts no transport URL")
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
