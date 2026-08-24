"""Widen ``skill.description`` and backfill built-in skill categories
(docs/architecture/skills.md).

Two related corrections to the category release (``0024``):

* ``description`` grows from 500 to 2000 characters. The 500-char cap was
  stricter than the open format actually is in practice — Anthropic's own
  public library ships docx (837), pptx (740), and xlsx (952) descriptions,
  and every one of those skills was being rejected outright at import. The
  agent prompt truncates descriptions to 300 characters independently, so
  nothing downstream grows.
* The five shipped starters get their hand-assigned categories, for
  workspaces whose starters were installed *before* ``0024`` added the
  column and therefore carry ``NULL``.

The backfill is deliberately narrow: it touches only rows with
``source = 'built_in'`` whose name is one of the five shipped starters, and
only their ``category`` — never content, never a skill an admin authored,
imported, or re-sourced. That distinction matters: this app's rule is that a
migration never retroactively changes a workspace's *skill content* (see
docs/architecture/skills.md), and a display grouping on a Jhin-shipped
starter is metadata, not content.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A snapshot of the shipped starters' categories. Deliberately inlined rather
# than imported from the app: a migration must keep describing the schema as
# it was at this revision even if the app's mapping later changes.
_BUILTIN_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("writing-clear-updates", "Communication"),
    ("code-review-checklist", "Engineering"),
    ("bug-report-triage", "Support"),
    ("meeting-notes-summary", "Communication"),
    ("release-notes", "Engineering"),
)


def upgrade() -> None:
    op.alter_column(
        "skill",
        "description",
        existing_type=sa.String(500),
        type_=sa.String(2000),
        existing_nullable=False,
    )
    skill = sa.table(
        "skill",
        sa.column("name", sa.String),
        sa.column("source", sa.String),
        sa.column("category", sa.String),
    )
    for name, category in _BUILTIN_CATEGORIES:
        op.execute(
            skill.update()
            .where(skill.c.name == op.inline_literal(name))
            .where(skill.c.source == op.inline_literal("built_in"))
            .where(skill.c.category.is_(None))
            .values(category=op.inline_literal(category))
        )


def downgrade() -> None:
    # The backfilled categories are left in place: dropping them would lose
    # an admin's subsequent edits too, and the column itself is 0024's.
    op.alter_column(
        "skill",
        "description",
        existing_type=sa.String(2000),
        type_=sa.String(500),
        existing_nullable=False,
    )
