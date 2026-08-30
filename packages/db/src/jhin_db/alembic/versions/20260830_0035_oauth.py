"""OAuth: client registrations, pending authorizations, and the token
lifecycle columns on ``connection`` (``docs/architecture/oauth.md``).

Three changes, one feature: connecting an app without anybody pasting a key.

*``oauth_client_registration``.* Who Jhin *is* at one authorization server,
per workspace. Keyed by ``(workspace_id, issuer, redirect_uri)``: the issuer
because MCP 2026-07-28 requires credentials to be keyed by the server that
issued them, the redirect URI because a registration is only valid for the URI
it was registered with — changing ``OAUTH_REDIRECT_BASE_URL`` should force a
fresh registration, not silently present a stale one.

*``oauth_authorization``.* One row per in-flight authorization, single-use and
short-lived. It stores ``sha256(state)`` rather than ``state``, so reading the
table grants nobody the ability to finish somebody else's flow.

*Nine columns on ``connection``.* The non-secret half of an OAuth grant:
issuer, audience, scopes, and the expiry timestamps the refresher sorts on.
The tokens stay where connection credentials already live — encrypted, behind
``encrypted_secret_id``. The partial index is the refresher's only query, and
is partial so it stays small on an install whose connections are mostly API
keys.

No DDL is needed for the new ``needs_reauth`` connection status:
``connection.status`` is a plain ``VARCHAR(16)`` with no check constraint, and
``needs_reauth`` is twelve characters.

``downgrade`` drops what ``upgrade`` created and nothing else. It deliberately
does *not* reclassify ``needs_reauth`` rows back to ``active``: a connection
whose refresh token is dead is not active, and quietly saying it is would send
agents at a provider that will reject them. Operators downgrading past this
revision should expect those rows to keep a status the older code does not
recognise, which the older code treats as "not active" — the safe reading.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on Postgres, plain JSON elsewhere — the same variant the model's own
# ``JsonDict`` column type uses, so the DDL and the mapping agree exactly.
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OAUTH_CONNECTION_COLUMNS = (
    "oauth_client_registration_id",
    "oauth_issuer",
    "oauth_resource",
    "oauth_scope",
    "oauth_expires_at",
    "oauth_refresh_expires_at",
    "oauth_last_refresh_at",
    "oauth_refresh_failures",
    "oauth_authorized_by_user_id",
)


def upgrade() -> None:
    op.create_table(
        "oauth_client_registration",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("client_id", sa.String(500), nullable=False),
        sa.Column("client_secret_id", sa.Uuid(), nullable=True),
        sa.Column("registration_access_token_id", sa.Uuid(), nullable=True),
        sa.Column("registration_client_uri", sa.String(1000), nullable=True),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'dcr'")),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("client_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_client_registration"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_oauth_client_registration_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_secret_id"],
            ["secret.id"],
            name="fk_oauth_client_registration_client_secret",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["registration_access_token_id"],
            ["secret.id"],
            name="fk_oauth_client_registration_registration_token",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_oauth_client_registration_created_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "workspace_id", "issuer", "redirect_uri", name="uq_oauth_client_registration"
        ),
        sa.CheckConstraint(
            "source IN ('dcr', 'manual', 'static')",
            name="ck_oauth_client_registration_source",
        ),
        sa.CheckConstraint(
            "token_endpoint_auth_method IN ('none', 'client_secret_post', 'client_secret_basic')",
            name="ck_oauth_client_registration_auth_method",
        ),
    )
    op.create_index(
        "ix_oauth_client_registration_workspace",
        "oauth_client_registration",
        ["workspace_id"],
    )

    op.create_table(
        "oauth_authorization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # sha256 hex of the opaque handle. For authorization_code flows the
        # handle IS the OAuth ``state`` parameter; the raw value is never
        # stored, so a database read cannot complete a pending authorization.
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("flow", sa.String(24), nullable=False),
        sa.Column("connector_type", sa.String(50), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("client_registration_id", sa.Uuid(), nullable=True),
        sa.Column("issuer", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "authorization_endpoint", sa.String(1000), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("token_endpoint", sa.String(1000), nullable=False, server_default=sa.text("''")),
        sa.Column("revocation_endpoint", sa.String(1000), nullable=True),
        sa.Column("resource", sa.String(1000), nullable=False, server_default=sa.text("''")),
        sa.Column("scope", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("redirect_uri", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "iss_parameter_supported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # The PKCE verifier (or device code), encrypted through SecretStore.
        sa.Column("verifier_secret_id", sa.Uuid(), nullable=True),
        # The pending non-secret connection payload: name, auth type, config.
        sa.Column(
            "draft_json",
            _JSON,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "poll_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("5")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_authorization"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_oauth_authorization_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_oauth_authorization_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection.id"],
            name="fk_oauth_authorization_connection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_registration_id"],
            ["oauth_client_registration.id"],
            name="fk_oauth_authorization_client_registration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verifier_secret_id"],
            ["secret.id"],
            name="fk_oauth_authorization_verifier_secret",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("state_hash", name="uq_oauth_authorization_state"),
        sa.CheckConstraint(
            "flow IN ('authorization_code', 'device_code', 'github_app_manifest')",
            name="ck_oauth_authorization_flow",
        ),
    )
    op.create_index("ix_oauth_authorization_expires_at", "oauth_authorization", ["expires_at"])
    op.create_index("ix_oauth_authorization_workspace", "oauth_authorization", ["workspace_id"])

    op.add_column("connection", sa.Column("oauth_client_registration_id", sa.Uuid(), nullable=True))
    op.add_column("connection", sa.Column("oauth_issuer", sa.String(500), nullable=True))
    op.add_column("connection", sa.Column("oauth_resource", sa.String(1000), nullable=True))
    op.add_column("connection", sa.Column("oauth_scope", sa.Text(), nullable=True))
    op.add_column(
        "connection", sa.Column("oauth_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "connection",
        sa.Column("oauth_refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connection", sa.Column("oauth_last_refresh_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "connection",
        sa.Column(
            "oauth_refresh_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column("connection", sa.Column("oauth_authorized_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_connection_oauth_client_registration",
        "connection",
        "oauth_client_registration",
        ["oauth_client_registration_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_connection_oauth_authorized_by",
        "connection",
        "user",
        ["oauth_authorized_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # The proactive refresher's only query. Partial so it stays small on
    # installs whose connections are overwhelmingly API keys.
    op.create_index(
        "ix_connection_oauth_refresh_due",
        "connection",
        ["oauth_expires_at"],
        postgresql_where=sa.text("oauth_expires_at IS NOT NULL AND status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_connection_oauth_refresh_due", table_name="connection")
    op.drop_constraint("fk_connection_oauth_authorized_by", "connection", type_="foreignkey")
    op.drop_constraint("fk_connection_oauth_client_registration", "connection", type_="foreignkey")
    for column in _OAUTH_CONNECTION_COLUMNS:
        op.drop_column("connection", column)
    op.drop_index("ix_oauth_authorization_workspace", table_name="oauth_authorization")
    op.drop_index("ix_oauth_authorization_expires_at", table_name="oauth_authorization")
    op.drop_table("oauth_authorization")
    op.drop_index("ix_oauth_client_registration_workspace", table_name="oauth_client_registration")
    op.drop_table("oauth_client_registration")
