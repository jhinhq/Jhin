"""Sandbox job records (plan 14): one row per ephemeral job container.

The sandbox runner itself is stateless with respect to Postgres — it holds
neither database credentials nor the master key. The caller (the CLI
connector executor inside the *tool* worker) writes these rows in the same
transaction as the ``tool_call`` row, so a job is always attributable to the
run and tool call that started it. ``stdout_tail``/``stderr_tail`` are
sanitized and size-capped before they reach this table (plan 48.9).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import SandboxJobStatus


class SandboxJob(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "sandbox_job"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None, index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None, index=True
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("tool_call.id", ondelete="SET NULL"), default=None, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=SandboxJobStatus.RUNNING.value, index=True
    )
    image: Mapped[str] = mapped_column(String(300))
    # Sanitized, truncated display form of the executed command.
    command: Mapped[str] = mapped_column(Text, default="")
    network_policy: Mapped[str] = mapped_column(String(16), default="none")
    cpu_limit: Mapped[float] = mapped_column(Float, default=2.0)
    memory_mb: Mapped[int] = mapped_column(Integer, default=4096)
    pids_limit: Mapped[int] = mapped_column(Integer, default=256)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    # Sanitized + size-capped output tails (never raw container logs).
    stdout_tail: Mapped[str] = mapped_column(Text, default="")
    stderr_tail: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)


class SandboxWorkspace(Base, UuidPkMixin, TimestampMixin):
    """What disk an agent's sandbox jobs run on, and who is using it now.

    One row per durable workspace volume. The row is the control plane's
    account of a Docker volume it cannot see: the runner knows the volume
    exists, and only this table knows whose it is, when it was last used, and
    whether a live run currently holds it.

    Two kinds share the table because they share a lifecycle and an operator's
    question ("what disk is this agent on?"):

    * ``agent`` — the persistent per-agent workspace, one per
      ``(workspace_id, agent_id)``, which survives a run and carries a
      checkout, a dependency install and a build cache from one turn to the
      next. Its key is derived from identity alone.
    * ``run`` — a private, run-scoped workspace, created when a second
      concurrent run of the same agent finds the agent workspace already held.
      It behaves exactly as every sandbox workspace did before this table
      existed: cloned fresh, deleted at finalize.

    ``holder_run_id`` is the lease. It is taken and released by a single
    ``UPDATE ... RETURNING`` so two concurrent runs can never both win, and it
    is never stolen from a run whose ``agent_run.status`` is still live —
    liveness is read from the holder's own row, not from a clock, so a run
    parked on an approval for six hours keeps its disk.

    No sandbox job can reach this table: it lives on the control-plane
    database, exactly like the ``sandbox.checkout.recorded`` audit row that a
    push compares against.
    """

    __tablename__ = "sandbox_workspace"
    __table_args__ = (
        UniqueConstraint("workspace_key", name="uq_sandbox_workspace_key"),
        # One durable workspace per agent, enforced by the database rather
        # than by whichever bind happens to run first.
        Index(
            "uq_sandbox_workspace_agent",
            "workspace_id",
            "agent_id",
            unique=True,
            postgresql_where=text("kind = 'agent'"),
            sqlite_where=text("kind = 'agent'"),
        ),
        # The LRU eviction scan: unheld agent workspaces, oldest use first.
        Index("ix_sandbox_workspace_kind_last_used", "kind", "last_used_at"),
        Index(
            "uq_sandbox_workspace_conversation",
            "workspace_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("kind = 'conversation'"),
            sqlite_where=text("kind = 'conversation'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    # The owner. Deleting an agent takes its workspace row with it; the volume
    # itself is then an orphan the runner reaps by label.
    agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True, default=None
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), index=True, default=None
    )
    holder_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(16), default="agent", index=True)
    # The exact string sent to the runner as ``workspace_key``, which becomes
    # the Docker volume name. Derived from identity, never from tool input.
    workspace_key: Mapped[str] = mapped_column(String(96))
    # For kind='run', the run this private workspace belongs to.
    run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None, index=True
    )
    # The live lease: the one run whose jobs may touch this disk.
    holder_run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None, index=True
    )
    # Who held it last, so a run that LOST a lease can be told so rather than
    # silently continuing on a different disk. Not a foreign key: it must
    # outlive the run row it names.
    last_holder_run_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None, index=True)
    # What the runner's init container last counted on this disk. Read
    # together with ``size_state``, never alone: on an ``unknown`` row this is
    # a lower bound the walk reached before it gave up, which is a number for
    # an operator to look at and not one any policy may act on.
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    size_measured_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    # Whether ``size_bytes`` is this disk's size or only a floor under it.
    # ``measured`` — a walk that finished, so the number is the disk's usage
    # to the byte. ``unknown`` — a walk that skipped something (its budget ran
    # out, an entry could not be read), so nothing is known except that the
    # disk holds at least ``size_bytes``.
    #
    # The distinction is the whole difference between a bound and a guess. An
    # unmeasurable workspace is refused, never destroyed and never treated as
    # small: a walk that stopped 0.44% of the way through a 6.5 GB disk read
    # as 28 MB, which is under every cap there is, so nothing was recycled and
    # nothing was refused. It is also why the size a partial walk reports is
    # no longer allowed to survive as though it were the answer.
    size_state: Mapped[str] = mapped_column(
        String(16), default="measured", server_default=text("'measured'")
    )
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # An operator's deferred reset. jhin-admin runs in the API container,
    # which is deliberately not on the runner network, so a reset is recorded
    # here and applied by the next bind — before any job of the next run
    # starts, which is the one moment the disk is provably idle.
    reset_requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    reset_requested_by: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
