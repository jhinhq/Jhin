"""Give the agents that already exist the new default grants.

New agents get ``jhin_policy.default_agent_grant_specs()`` at creation. The
ones already in a workspace were created before memory and asking were part
of that set, and an agent that cannot remember what it is told is a colleague
with amnesia — so they are backfilled here rather than left needing a hand
edit per agent.

Data-only, and deliberately conservative in two ways:

- a row is written only where the ``(workspace_id, agent_id, capability)``
  triple has *no* grant at all, in either effect. An existing ``deny`` is a
  decision somebody made and this migration must not paper over it; an
  existing ``allow`` may carry a scope this must not duplicate. The filter is
  a ``NOT EXISTS`` in the query, so it reads the same state it inserts against.
- ``downgrade`` removes only unscoped ``allow`` rows for these capabilities.
  It cannot tell a backfilled grant from an identical hand-made one, and that
  is the correct trade: the alternative is a downgrade that strips nothing.

Ids are UUIDv7 from the application, like every other row in this schema
(0014 and 0015 do the same), rather than a database-side v4.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-27
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors jhin_policy.memory_grant_specs() + ask_person_grant_specs(). Spelled
# out rather than imported: a migration has to keep meaning what it meant on
# the day it ran, even after the policy default changes again.
BACKFILLED_CAPABILITIES = ("memory.read", "memory.propose", "organization.ask_person")

_JSON_DICT = sa.JSON().with_variant(JSONB(), "postgresql")

_AGENT = sa.table(
    "agent",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
)
_GRANT = sa.table(
    "agent_capability_grant",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("agent_id", sa.Uuid(as_uuid=True)),
    sa.column("capability", sa.String),
    sa.column("scope_json", _JSON_DICT),
    sa.column("effect", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _agents_without(capability: str) -> sa.Select[tuple[object, object]]:
    already_granted = (
        sa.select(_GRANT.c.id)
        .where(
            _GRANT.c.workspace_id == _AGENT.c.workspace_id,
            _GRANT.c.agent_id == _AGENT.c.id,
            _GRANT.c.capability == capability,
        )
        .exists()
    )
    return sa.select(_AGENT.c.workspace_id, _AGENT.c.id).where(~already_granted)


def upgrade() -> None:
    bind = op.get_bind()
    # A Python timestamp, not sa.func.now(): these rows go in as one
    # executemany, and a SQL function cannot ride in a bound parameter.
    now = datetime.now(UTC)
    for capability in BACKFILLED_CAPABILITIES:
        rows = [
            {
                "id": new_uuid7(),
                "workspace_id": workspace_id,
                "agent_id": agent_id,
                "capability": capability,
                "scope_json": {},
                "effect": "allow",
                "created_at": now,
                "updated_at": now,
            }
            for workspace_id, agent_id in bind.execute(_agents_without(capability)).all()
        ]
        if rows:
            bind.execute(_GRANT.insert(), rows)


def downgrade() -> None:
    bind = op.get_bind()
    scope_is_empty = (
        sa.cast(_GRANT.c.scope_json, sa.Text) == "{}"
        if bind.dialect.name == "postgresql"
        else _GRANT.c.scope_json == {}
    )
    bind.execute(
        _GRANT.delete().where(
            _GRANT.c.capability.in_(BACKFILLED_CAPABILITIES),
            _GRANT.c.effect == "allow",
            scope_is_empty,
        )
    )
