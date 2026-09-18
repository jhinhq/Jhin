"""Reserve each managed app namespace for one connection per workspace.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-08

Disabled connections retain their names. If a development install already
contains duplicates, the oldest keeps its name and later rows gain a suffix.
Original distinct names are reserved before any suffix is chosen. No row,
credential, or grant is deleted; downgrade leaves repaired names in place.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_connection_composio_server_slug"
_CONNECTION = sa.table(
    "connection",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("connector_type", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("config_json", sa.JSON().with_variant(JSONB(), "postgresql")),
)


def _deduplicate_server_slugs(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.select(_CONNECTION.c.id, _CONNECTION.c.workspace_id, _CONNECTION.c.config_json)
        .where(_CONNECTION.c.connector_type == "composio")
        .order_by(_CONNECTION.c.created_at, _CONNECTION.c.id)
    ).all()
    taken: dict[object, set[str]] = {}
    for _row_id, workspace_id, config in rows:
        slug = config.get("server_slug") if isinstance(config, dict) else None
        if isinstance(slug, str) and slug:
            taken.setdefault(workspace_id, set()).add(slug)
    owners: set[tuple[object, str]] = set()
    for row_id, workspace_id, config in rows:
        slug = config.get("server_slug") if isinstance(config, dict) else None
        if not isinstance(slug, str) or not slug:
            continue
        key = (workspace_id, slug)
        if key not in owners:
            owners.add(key)
            continue
        reserved = taken[workspace_id]
        attempt = 2
        while True:
            suffix = f"_{attempt}"
            replacement = f"{slug[: 32 - len(suffix)]}{suffix}"
            if replacement not in reserved:
                break
            attempt += 1
        reserved.add(replacement)
        bind.execute(
            _CONNECTION.update()
            .where(_CONNECTION.c.id == row_id)
            .values(config_json={**config, "server_slug": replacement})
        )


def upgrade() -> None:
    bind = op.get_bind()
    _deduplicate_server_slugs(bind)
    if bind.dialect.name == "postgresql":
        expression = "(config_json ->> 'server_slug')"
    elif bind.dialect.name == "sqlite":
        expression = "json_extract(config_json, '$.server_slug')"
    else:
        raise RuntimeError("Managed app namespace index requires PostgreSQL or SQLite")
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON connection (workspace_id, {expression}) "
            f"WHERE connector_type = 'composio' AND {expression} IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
