"""``sandbox_workspace``: which disk an agent's sandbox jobs run on.

Until now every sandbox workspace was named ``run-<run_id>`` and deleted when
the run finalized, so an agent re-cloned from scratch every turn and lost every
dependency install, build artefact and uncommitted edit between one message and
the next. This table is the control plane's account of a durable per-agent
volume: who owns it, when it was last used, how big it was at the last
measurement, and which run holds it right now.

Additive only. No existing table gains or loses a column, and nothing is
backfilled: a workspace row is written by the first ``cli.*`` call that binds
one, so an install that never runs a sandbox job never grows a row. Runs that
are in flight across this deploy keep working — they simply bind a workspace on
their next call.

Two indexes carry rules rather than performance:

* ``uq_sandbox_workspace_agent`` is partial on ``kind = 'agent'`` and is what
  makes "one agent, one durable workspace" a database fact rather than an
  argument about which bind ran first. Run-scoped rows are deliberately outside
  it: an agent may have many of those at once, one per concurrent run.
* ``uq_sandbox_workspace_key`` covers both kinds, because the key is the Docker
  volume name and two rows naming one volume would be two owners of one disk.

``downgrade`` drops the table. The volumes it described outlive it; the runner
reaps run-kind volumes by age at startup, and an agent-kind volume left behind
by a downgrade becomes an orphan an operator removes with ``docker volume rm``.
That is stated here rather than pretended away: a downgrade cannot reach the
Docker daemon from a migration.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_workspace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'agent'")),
        sa.Column("workspace_key", sa.String(96), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("holder_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_holder_run_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("size_measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("reset_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_requested_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_workspace"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_sandbox_workspace_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            name="fk_sandbox_workspace_agent_id_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_run.id"],
            name="fk_sandbox_workspace_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["holder_run_id"],
            ["agent_run.id"],
            name="fk_sandbox_workspace_holder_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reset_requested_by"],
            ["user.id"],
            name="fk_sandbox_workspace_reset_requested_by_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("workspace_key", name="uq_sandbox_workspace_key"),
    )
    op.create_index("ix_sandbox_workspace_workspace_id", "sandbox_workspace", ["workspace_id"])
    op.create_index("ix_sandbox_workspace_agent_id", "sandbox_workspace", ["agent_id"])
    op.create_index("ix_sandbox_workspace_kind", "sandbox_workspace", ["kind"])
    op.create_index("ix_sandbox_workspace_run_id", "sandbox_workspace", ["run_id"])
    op.create_index("ix_sandbox_workspace_holder_run_id", "sandbox_workspace", ["holder_run_id"])
    op.create_index("ix_sandbox_workspace_last_used_at", "sandbox_workspace", ["last_used_at"])
    op.create_index("ix_sandbox_workspace_state", "sandbox_workspace", ["state"])
    op.create_index(
        "ix_sandbox_workspace_kind_last_used", "sandbox_workspace", ["kind", "last_used_at"]
    )
    op.create_index(
        "uq_sandbox_workspace_agent",
        "sandbox_workspace",
        ["workspace_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'agent'"),
        sqlite_where=sa.text("kind = 'agent'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sandbox_workspace_agent", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_kind_last_used", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_state", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_last_used_at", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_holder_run_id", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_run_id", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_kind", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_agent_id", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_workspace_id", table_name="sandbox_workspace")
    op.drop_table("sandbox_workspace")
