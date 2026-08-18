"""Company topology membership, relationship, and public identity fields.

Membership and relationship rows are workspace-local routing context only;
they do not grant capabilities or data access. Legacy ``agent.team_id`` links
become primary memberships while the compatibility pointer remains in place.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent",
        sa.Column("public_purpose", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "agent",
        sa.Column(
            "expertise_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "agent",
        sa.Column(
            "discoverability",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'discoverable'"),
        ),
    )
    op.add_column(
        "agent",
        sa.Column(
            "availability",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'available'"),
        ),
    )

    op.create_unique_constraint("uq_agent_workspace_id_id", "agent", ["workspace_id", "id"])
    op.create_unique_constraint("uq_team_workspace_id_id", "team", ["workspace_id", "id"])

    op.create_table(
        "agent_team_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("role_label", sa.String(200), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agent_team_membership"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_team_membership_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_team_membership_workspace_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "team_id"],
            ["team.workspace_id", "team.id"],
            name="fk_agent_team_membership_workspace_team",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_agent_team_membership_active_primary",
        "agent_team_membership",
        ["workspace_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL AND is_primary"),
    )
    op.create_index(
        "uq_agent_team_membership_active_pair",
        "agent_team_membership",
        ["workspace_id", "agent_id", "team_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )
    op.create_index(
        "ix_agent_team_membership_workspace_agent",
        "agent_team_membership",
        ["workspace_id", "agent_id"],
    )
    op.create_index(
        "ix_agent_team_membership_workspace_team",
        "agent_team_membership",
        ["workspace_id", "team_id"],
    )

    agent = sa.table(
        "agent",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("team_id", sa.Uuid()),
    )
    membership = sa.table(
        "agent_team_membership",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("agent_id", sa.Uuid()),
        sa.column("team_id", sa.Uuid()),
        sa.column("is_primary", sa.Boolean()),
        sa.column("role_label", sa.String()),
    )
    bind = op.get_bind()
    legacy_agents = bind.execute(
        sa.select(agent.c.id, agent.c.workspace_id, agent.c.team_id).where(
            agent.c.team_id.is_not(None)
        )
    ).mappings()
    rows = [
        {
            "id": new_uuid7(),
            "workspace_id": row["workspace_id"],
            "agent_id": row["id"],
            "team_id": row["team_id"],
            "is_primary": True,
            "role_label": "",
        }
        for row in legacy_agents
    ]
    if rows:
        bind.execute(membership.insert(), rows)

    op.create_table(
        "agent_relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_agent_id", sa.Uuid(), nullable=False),
        sa.Column("target_agent_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_relationship"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_relationship_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_relationship_workspace_source_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "target_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_relationship_workspace_target_agent",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('close_collaborator', 'advisor', 'preferred_reviewer')",
            name=op.f("ck_agent_relationship_kind"),
        ),
        sa.CheckConstraint(
            "kind <> 'close_collaborator' OR source_agent_id < target_agent_id",
            name=op.f("ck_agent_relationship_close_collaborator_order"),
        ),
        sa.CheckConstraint(
            "kind = 'close_collaborator' OR source_agent_id <> target_agent_id",
            name=op.f("ck_agent_relationship_directed_not_self"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name=op.f("ck_agent_relationship_status"),
        ),
    )
    op.create_index(
        "uq_agent_relationship_active_pair_kind",
        "agent_relationship",
        ["workspace_id", "source_agent_id", "target_agent_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_agent_relationship_workspace_source",
        "agent_relationship",
        ["workspace_id", "source_agent_id"],
    )
    op.create_index(
        "ix_agent_relationship_workspace_target",
        "agent_relationship",
        ["workspace_id", "target_agent_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_relationship")
    op.drop_table("agent_team_membership")
    op.drop_constraint("uq_team_workspace_id_id", "team", type_="unique")
    op.drop_constraint("uq_agent_workspace_id_id", "agent", type_="unique")
    op.drop_column("agent", "availability")
    op.drop_column("agent", "discoverability")
    op.drop_column("agent", "expertise_json")
    op.drop_column("agent", "public_purpose")
