"""OAuth client registrations and pending authorizations
(``docs/architecture/oauth.md``).

Two tables, one rule between them: nothing here holds credential material.
The dynamic-registration client secret, the PKCE code verifier, and the device
code are all encrypted through ``jhin_secrets`` and referenced by ``secret``
row id, exactly as connection credentials already are. What stays in columns
is the non-secret shape of a flow — which issuer, which redirect URI, which
scopes, when it expires — so the callback can decide whether to proceed
without decrypting anything.

``oauth_authorization`` never stores the raw ``state`` handed to the browser.
It stores ``sha256(state)``, so a database read grants nobody the ability to
complete somebody else's pending authorization.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import (
    CreatedAtMixin,
    JsonDict,
    StdUuid,
    TimestampMixin,
    UtcDateTime,
    UuidPkMixin,
)


class OAuthClientRegistration(Base, UuidPkMixin, TimestampMixin):
    """One workspace's OAuth client identity at one authorization server.

    Keyed by ``(workspace_id, issuer, redirect_uri)`` because both halves are
    load-bearing. The issuer is the MCP 2026-07-28 rule — credentials are
    keyed by the authorization server that issued them and are never presented
    to a different one. The redirect URI is part of the key because a
    registration is only valid for the URI it was registered with: changing
    ``OAUTH_REDIRECT_BASE_URL`` therefore forces a fresh registration instead
    of silently presenting a stale one.

    Registrations are never shared between workspaces. Workspaces are Jhin's
    tenancy boundary everywhere else, and a client secret reaching across one
    would let a compromise in one workspace reach another's provider account.
    """

    __tablename__ = "oauth_client_registration"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "issuer", "redirect_uri", name="uq_oauth_client_registration"
        ),
        CheckConstraint(
            "source IN ('dcr', 'manual', 'static')",
            name="ck_oauth_client_registration_source",
        ),
        CheckConstraint(
            "token_endpoint_auth_method IN ('none', 'client_secret_post', 'client_secret_basic')",
            name="ck_oauth_client_registration_auth_method",
        ),
        Index("ix_oauth_client_registration_workspace", "workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    #: The validated issuer identifier — the value that byte-matched the
    #: ``issuer`` field of the authorization server's own metadata document.
    issuer: Mapped[str] = mapped_column(String(500))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    #: Public by definition: a client id is sent in a URL the user can read.
    client_id: Mapped[str] = mapped_column(String(500))
    client_secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    #: RFC 7592 management credential. Optional everywhere: a registration is
    #: never refused for lacking one, and it is used only to delete the
    #: registration again on disconnect, best-effort.
    registration_access_token_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    registration_client_uri: Mapped[str | None] = mapped_column(String(1000), default=None)
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(32), default="none")
    source: Mapped[str] = mapped_column(String(16), default="dcr")
    scopes: Mapped[str] = mapped_column(Text, default="")
    client_secret_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class OAuthAuthorization(Base, UuidPkMixin, CreatedAtMixin):
    """One pending authorization: single-use, short-lived, bound to a user.

    The row exists between "the admin clicked Connect" and "the provider sent
    the browser back", which is the window an attacker would like to walk
    through. Four properties close it: the lookup key is a hash of a 256-bit
    handle the attacker does not have, ``expires_at`` bounds the window,
    ``consumed_at`` makes a replay a no-op, and ``user_id`` means a stolen
    handle still needs the initiating user's browser session.
    """

    __tablename__ = "oauth_authorization"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_oauth_authorization_state"),
        CheckConstraint(
            "flow IN ('authorization_code', 'device_code', 'github_app_manifest')",
            name="ck_oauth_authorization_flow",
        ),
        Index("ix_oauth_authorization_expires_at", "expires_at"),
        Index("ix_oauth_authorization_workspace", "workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    #: The user whose browser started this. The callback compares the live
    #: session against it; that comparison is the load-bearing CSRF defense.
    user_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("user.id", ondelete="CASCADE"))
    #: Lowercase hex ``sha256`` of the opaque handle. For authorization-code
    #: flows the handle *is* the OAuth ``state`` parameter. The raw value is
    #: returned to the caller once and never persisted.
    state_hash: Mapped[str] = mapped_column(String(64))
    flow: Mapped[str] = mapped_column(String(24))
    connector_type: Mapped[str] = mapped_column(String(50))
    #: Set when re-authorizing an existing connection rather than creating one.
    connection_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE"), default=None
    )
    client_registration_id: Mapped[UUID | None] = mapped_column(
        StdUuid,
        ForeignKey("oauth_client_registration.id", ondelete="CASCADE"),
        default=None,
    )
    #: Captured from the validated metadata document *before* redirecting, so
    #: the ``iss`` the provider returns can be compared against what we
    #: actually talked to (RFC 9207, mandated by MCP 2026-07-28).
    issuer: Mapped[str] = mapped_column(String(500), default="")
    authorization_endpoint: Mapped[str] = mapped_column(String(1000), default="")
    token_endpoint: Mapped[str] = mapped_column(String(1000), default="")
    revocation_endpoint: Mapped[str | None] = mapped_column(String(1000), default=None)
    resource: Mapped[str] = mapped_column(String(1000), default="")
    scope: Mapped[str] = mapped_column(Text, default="")
    #: Recomputed from settings at start time and compared byte-for-byte at
    #: callback time, so an operator who changes the base URL mid-flow gets a
    #: refusal rather than a token bound to the wrong URI.
    redirect_uri: Mapped[str] = mapped_column(String(500), default="")
    iss_parameter_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The encrypted PKCE verifier, or the device code for RFC 8628 flows.
    verifier_secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    #: The pending, non-secret connection payload (name, auth type, config).
    #: The store refuses any key that looks like credential material.
    draft_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=5)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
