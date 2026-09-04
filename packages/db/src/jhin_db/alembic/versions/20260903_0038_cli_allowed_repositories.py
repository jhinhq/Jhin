"""Grandfather every existing CLI Sandbox connection onto the new allow-list.

``allowed_repositories`` is deny-by-default: a CLI connection that does not
list a repository cannot check it out and cannot push it. That is the right
failure direction for a new connection and the wrong one for a connection that
was cloning happily yesterday, so rows that predate the field are backfilled
to ``["*"]`` — the behaviour they already had — and the connection page shows
an "allows every repository" banner so an operator narrows it deliberately
rather than discovering the change through a broken run.

Data-only, no schema change. Conservative in both directions:

- upgrade writes only where the key is *absent*. A connection an operator has
  already narrowed (including to an empty list, which means "no repository
  work") is a decision this migration must not overwrite.
- downgrade removes the key only where it is exactly ``["*"]``. It cannot tell
  a backfilled value from an identical hand-typed one, and leaving a narrower
  list in place is the safer half of that trade.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-03
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_DICT = sa.JSON().with_variant(JSONB(), "postgresql")

_CONNECTION = sa.table(
    "connection",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("connector_type", sa.String),
    sa.column("config_json", _JSON_DICT),
)

GRANDFATHERED: list[str] = ["*"]


def _cli_configs() -> list[tuple[Any, dict[str, Any]]]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_CONNECTION.c.id, _CONNECTION.c.config_json).where(
            _CONNECTION.c.connector_type == "cli"
        )
    ).all()
    return [(row[0], dict(row[1] or {})) for row in rows]


def upgrade() -> None:
    bind = op.get_bind()
    for connection_id, config in _cli_configs():
        if "allowed_repositories" in config:
            continue
        config["allowed_repositories"] = list(GRANDFATHERED)
        bind.execute(
            _CONNECTION.update().where(_CONNECTION.c.id == connection_id).values(config_json=config)
        )


def downgrade() -> None:
    bind = op.get_bind()
    for connection_id, config in _cli_configs():
        if config.get("allowed_repositories") != GRANDFATHERED:
            continue
        config.pop("allowed_repositories", None)
        bind.execute(
            _CONNECTION.update().where(_CONNECTION.c.id == connection_id).values(config_json=config)
        )
