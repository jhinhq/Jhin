"""Agent concurrency limit (plan 30).

``agent.max_concurrent_runs`` caps simultaneously active runs per agent;
admission happens at run start and excess work queues visibly. The workspace
ceiling lives in ``workspace.settings_json`` (no schema change needed).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent",
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("agent", "max_concurrent_runs")
