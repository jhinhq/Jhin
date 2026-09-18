"""One agent name per workspace, enforced by the database.

An agent's name is asserted unhedged in layer 1 of its own system prompt and
read back by every colleague through the roster, so two agents called "QA
Engineer" make every roster line and every "ask QA Engineer to…" ambiguous.
``organization.identity.set_name`` has always refused that (``agent_name_taken``),
and the HTTP path now refuses it too — but both are read-then-write checks,
and two requests can pass the read before either writes. This index is what
makes the rule a fact rather than an argument about which write landed first.

Case-insensitive, because "qa engineer" and "QA Engineer" are one name to
anybody reading a roster. The *handle* half of the collision is already a
database fact: ``uq_agent_workspace_slug`` has always been there, so a name
that slugs onto a colleague's handle cannot be stored either.

**The install may already hold duplicates**, because until now nothing
stopped them (the reported case created two "QA Engineer" agents through
``PATCH``). Creating the index on that data would fail and take the deploy
with it, so the duplicates are resolved first: within a workspace, the
*oldest* row keeps the name and every later one gains the smallest free
" 2", " 3", … suffix. Deterministic, and never silent — each fix-up writes
the same ``agent.renamed`` audit row every other rename writes, attributed to
the system with ``via: "migration 0043"``, so an admin can find what this did
and set a better name by hand.

``downgrade`` drops the index only. The renames stand: a downgrade cannot
know which of two identical names was the one somebody meant, and inventing
one back would be a second silent rename.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-06
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import count
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_agent_workspace_lower_name"

# ``agent.name`` is String(200); a suffixed name has to stay inside it.
_MAX_NAME_CHARS = 200
_SUFFIX_ROOM = 10

_JSON_DICT = sa.JSON().with_variant(JSONB(), "postgresql")

_AGENT = sa.table(
    "agent",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("name", sa.String),
    sa.column("slug", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_AUDIT = sa.table(
    "audit_event",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("actor_type", sa.String),
    sa.column("actor_id", sa.Uuid(as_uuid=True)),
    sa.column("action", sa.String),
    sa.column("target_type", sa.String),
    sa.column("target_id", sa.Uuid(as_uuid=True)),
    sa.column("metadata_json", _JSON_DICT),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _free_name(base: str, taken: set[str]) -> str:
    """The smallest ``base N`` nobody in this workspace answers to."""
    root = base[: _MAX_NAME_CHARS - _SUFFIX_ROOM].rstrip() or "Agent"
    for suffix in count(2):
        candidate = f"{root} {suffix}"
        if candidate.casefold() not in taken:
            return candidate
    raise AssertionError("unreachable: count() is infinite")


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    rows = bind.execute(
        sa.select(_AGENT.c.id, _AGENT.c.workspace_id, _AGENT.c.name, _AGENT.c.slug).order_by(
            _AGENT.c.workspace_id, _AGENT.c.created_at, _AGENT.c.id
        )
    ).all()

    taken_by_workspace: dict[Any, set[str]] = {}
    audits: list[dict[str, Any]] = []
    for agent_id, workspace_id, name, slug in rows:
        taken = taken_by_workspace.setdefault(workspace_id, set())
        key = (name or "").casefold()
        if key and key not in taken:
            taken.add(key)
            continue
        renamed = _free_name(name or "Agent", taken)
        taken.add(renamed.casefold())
        bind.execute(_AGENT.update().where(_AGENT.c.id == agent_id).values(name=renamed))
        audits.append(
            {
                "id": new_uuid7(),
                "workspace_id": workspace_id,
                "actor_type": "system",
                "actor_id": None,
                "action": "agent.renamed",
                "target_type": "agent",
                "target_id": agent_id,
                "metadata_json": {
                    "from": name,
                    "to": renamed,
                    # Unchanged, as in every other rename: the slug is the
                    # stable handle and nobody's links may move.
                    "slug": slug,
                    "requested_by_user_id": None,
                    "requested_by_name": "",
                    "requested_via": "migration",
                    "via": "migration 0043",
                    "reason": (
                        "another agent in this workspace already had this name; "
                        "one name per workspace is now enforced by the database"
                    ),
                },
                "created_at": now,
            }
        )
    if audits:
        bind.execute(_AUDIT.insert(), audits)

    op.create_index(
        INDEX_NAME,
        "agent",
        ["workspace_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="agent")
