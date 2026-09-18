"""Pin variable consumers to a full Ghost installation URL.

Revision ID: 0051
Revises: 0050
"""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _canonical(value: object) -> tuple[str, str] | None:
    # Migration-local normalization is immutable across future app upgrades.
    if not isinstance(value, str) or any(
        ord(char) <= 32 or ord(char) == 127 or char in "\\%?#" for char in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
    except ValueError:
        return None
    origin = f"{parsed.scheme}://{host}"
    if port and port != (443 if parsed.scheme == "https" else 80):
        origin += f":{port}"
    path = parsed.path.rstrip("/")
    for suffix in ("/ghost/api/admin", "/ghost"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if "//" in path or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in path.split("/") if p):
        return None
    return origin, origin + path


def upgrade() -> None:
    op.add_column(
        "variable_connection_binding",
        sa.Column("approved_admin_url", sa.String(2048), nullable=False, server_default=""),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT b.id, b.approved_origin, c.config_json FROM variable_connection_binding b "
            "JOIN connection c ON c.id=b.connection_id AND c.workspace_id=b.workspace_id"
        )
    ).mappings()
    for row in rows:
        config = row["config_json"]
        normalized = _canonical(config.get("admin_url")) if isinstance(config, dict) else None
        if normalized and normalized[0] == row["approved_origin"]:
            connection.execute(
                sa.text(
                    "UPDATE variable_connection_binding SET approved_admin_url=:target WHERE id=:id"
                ),
                {"target": normalized[1], "id": row["id"]},
            )
    # Invalid/unmatched legacy rows keep an empty pin and fail closed until an
    # administrator deliberately recreates the connection binding.


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM variable_connection_binding)")
    )
    if populated:
        raise RuntimeError(
            "Remove variable app bindings before downgrading full URL pin protection"
        )
    op.drop_column("variable_connection_binding", "approved_admin_url")
