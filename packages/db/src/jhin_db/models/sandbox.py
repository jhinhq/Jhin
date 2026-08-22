"""Sandbox job records (plan 14): one row per ephemeral job container.

The sandbox runner itself is stateless with respect to Postgres — it holds
neither database credentials nor the master key. The caller (the CLI
connector executor inside the agent worker) writes these rows in the same
transaction as the ``tool_call`` row, so a job is always attributable to the
run and tool call that started it. ``stdout_tail``/``stderr_tail`` are
sanitized and size-capped before they reach this table (plan 48.9).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, StdUuid, UtcDateTime, UuidPkMixin
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
