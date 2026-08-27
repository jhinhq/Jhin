"""One MCP short name per workspace.

Discovered MCP tools are named ``mcp.<short name>.<tool>``, and so are the
capability family and the per-tool risk overrides an admin reviews. When two
connections in one workspace share a short name only one of them is ever
resolved, so the other's approved risk levels stop being enforced — a
destructive tool can run without the approval its own connection demands.

The unique index is partial twice over: only MCP rows, and only rows that
actually carry a short name. It is an expression index over ``config_json``,
so the DDL is written per dialect.

Existing duplicates are renamed rather than deleted: the *oldest* row keeps
the short name (it is the one the tool catalog already resolves, so nothing
an agent can reach today changes) and every newer row is suffixed. The rename
is deliberately not undone by ``downgrade``, which only drops the index —
the original state was the ambiguity this migration removes, and there is no
record of which row a name "should" belong to.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_connection_mcp_server_slug"
MCP_CONNECTOR_TYPE = "mcp"
# jhin_connectors.mcp.discovery.is_valid_server_slug: 1-32 lowercase letters,
# digits, or underscores. Every generated replacement has to stay inside it.
_MAX_SLUG_LENGTH = 32

_CONNECTION = sa.table(
    "connection",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("connector_type", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("config_json", sa.JSON().with_variant(JSONB(), "postgresql")),
)

# An expression index over JSONB, so it is written out rather than built with
# op.create_index. Migrations run against Postgres only (earlier revisions in
# this chain already emit JSONB DDL), so there is no second dialect to carry.
_CREATE_INDEX = (
    f"CREATE UNIQUE INDEX {INDEX_NAME} ON connection "
    "(workspace_id, (config_json ->> 'server_slug')) "
    f"WHERE connector_type = '{MCP_CONNECTOR_TYPE}' "
    "AND config_json ->> 'server_slug' IS NOT NULL"
)


def free_slug(slug: str, taken: set[str], row_id: object) -> str:
    """A valid short name near ``slug`` that nothing in the workspace uses.

    Counting suffixes read the way an operator expects (``notion_2``); the
    row id is the fallback so the search always terminates."""
    for attempt in range(2, 100):
        suffix = f"_{attempt}"
        candidate = f"{slug[: _MAX_SLUG_LENGTH - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
    unique = f"_{getattr(row_id, 'hex', str(row_id))[-8:]}"
    return f"{slug[: _MAX_SLUG_LENGTH - len(unique)]}{unique}"


def _deduplicate_server_slugs(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.select(
            _CONNECTION.c.id,
            _CONNECTION.c.workspace_id,
            _CONNECTION.c.config_json,
        )
        .where(_CONNECTION.c.connector_type == MCP_CONNECTOR_TYPE)
        .order_by(_CONNECTION.c.created_at, _CONNECTION.c.id)
    ).all()

    taken: dict[object, set[str]] = {}
    for row_id, workspace_id, config in rows:
        if not isinstance(config, dict):
            continue
        slug = config.get("server_slug")
        if not isinstance(slug, str) or not slug:
            continue
        workspace_slugs = taken.setdefault(workspace_id, set())
        if slug not in workspace_slugs:
            workspace_slugs.add(slug)
            continue
        replacement = free_slug(slug, workspace_slugs, row_id)
        workspace_slugs.add(replacement)
        bind.execute(
            _CONNECTION.update()
            .where(_CONNECTION.c.id == row_id)
            .values(config_json={**config, "server_slug": replacement})
        )


def upgrade() -> None:
    bind = op.get_bind()
    _deduplicate_server_slugs(bind)
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_CREATE_INDEX))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
