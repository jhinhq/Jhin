"""Agent Skills: the workspace ``skill`` library and the ``agent_skill``
per-agent enablement join table (docs/architecture/skills.md).

Purely additive.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("files_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_skill_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_skill_workspace_id_name"),
    )
    op.create_index("ix_skill_workspace_id", "skill", ["workspace_id"])

    op.create_table(
        "agent_skill",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_skill"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_skill_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            name="fk_agent_skill_agent_id_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skill.id"],
            name="fk_agent_skill_skill_id_skill",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill_agent_id_skill_id"),
    )
    op.create_index("ix_agent_skill_workspace_id", "agent_skill", ["workspace_id"])
    op.create_index("ix_agent_skill_agent_id", "agent_skill", ["agent_id"])
    op.create_index("ix_agent_skill_skill_id", "agent_skill", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_skill_skill_id", table_name="agent_skill")
    op.drop_index("ix_agent_skill_agent_id", table_name="agent_skill")
    op.drop_index("ix_agent_skill_workspace_id", table_name="agent_skill")
    op.drop_table("agent_skill")
    op.drop_index("ix_skill_workspace_id", table_name="skill")
    op.drop_table("skill")
