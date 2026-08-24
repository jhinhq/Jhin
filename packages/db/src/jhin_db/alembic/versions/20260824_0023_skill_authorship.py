"""Skill authorship: ``created_by_agent_id`` on ``skill`` for agent-authored
skills created through the ``skills.create`` gateway tool
(docs/architecture/skills.md).

Purely additive.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill",
        sa.Column("created_by_agent_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_skill_created_by_agent_id", "skill", ["created_by_agent_id"])
    op.create_foreign_key(
        "fk_skill_created_by_agent_id_agent",
        "skill",
        "agent",
        ["created_by_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_skill_created_by_agent_id_agent", "skill", type_="foreignkey")
    op.drop_index("ix_skill_created_by_agent_id", table_name="skill")
    op.drop_column("skill", "created_by_agent_id")
