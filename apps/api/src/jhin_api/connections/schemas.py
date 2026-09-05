"""Request/response schemas for connectors and connections (plan 6.9, 11.1).

Credential fields are write-only: they appear in ``ConnectionCreate`` and
``CredentialsRotate`` and nowhere else. The per-connection webhook secret is
returned exactly once, inside ``ConnectionCreated``.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SecretFieldOut(BaseModel):
    name: str
    label: str
    placeholder: str = ""
    multiline: bool = False
    required: bool = True


class AuthSchemeOut(BaseModel):
    type: str
    label: str
    description: str = ""
    secret_fields: list[SecretFieldOut] = []


class ConfigFieldOut(BaseModel):
    name: str
    label: str
    required: bool = False
    placeholder: str = ""
    help: str = ""
    kind: Literal["text", "integer", "boolean", "string_list"] = "text"
    auth_types: list[str] = []
    default: Any | None = None
    minimum: int | None = None
    maximum: int | None = None


class ConnectorOut(BaseModel):
    """One installed connector's manifest, safe for any authenticated user."""

    connector_type: str
    display_name: str
    icon: str
    description: str = ""
    auth_schemes: list[AuthSchemeOut] = []
    config_fields: list[ConfigFieldOut] = []
    webhook_events: list[str] = []
    canonical_events: list[str] = []
    capabilities: list[str] = []
    supports_webhooks: bool = False
    webhook_secret_mode: Literal["none", "generated", "provider_supplied"] = "none"
    webhook_signature_algorithm: str = ""
    webhook_setup_help: str = ""
    docs_url: str = ""


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, strict=True, extra="forbid")

    connector_type: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    auth_type: str = Field(min_length=1, max_length=32)
    # Credential fields per the connector's auth scheme; encrypted at rest,
    # never returned.
    credentials: dict[str, str] = Field(default_factory=dict)
    # Public, non-secret configuration (e.g. base_url, allowed org).
    config: dict[str, Any] = Field(default_factory=dict)


class CredentialsRotate(BaseModel):
    """Re-entered credential fields; replaces the encrypted secret in place."""

    model_config = ConfigDict(strict=True, extra="forbid")

    credentials: dict[str, str] = Field(min_length=1)


class ConnectionAuthorizedByOut(BaseModel):
    """Whose provider account an OAuth connection acts as.

    Named on every connection because the consequence is real: every agent
    granted this connection acts with this person's permissions at the
    provider, and an admin should never have to ask around to find out whose.
    """

    user_id: UUID
    display_name: str


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    connector_type: str
    name: str
    auth_type: str
    status: str
    public_id: str
    config_json: dict[str, Any]
    created_by_user_id: UUID | None
    created_at: datetime
    last_verified_at: datetime | None
    last_error: str | None
    webhook_secret_configured: bool = False
    # --- OAuth (docs/architecture/oauth.md). Never a token, never a secret. ---
    authorized_by: ConnectionAuthorizedByOut | None = None
    oauth_expires_at: datetime | None = None
    # A convenience mirror of ``status``, so the web app's banner does not have
    # to know the status vocabulary to render the one case that needs a person.
    needs_reauth: bool = False


class ConnectionGrantSummaryOut(BaseModel):
    """One connection-scoped grant, safe to expose to workspace admins."""

    grant_id: UUID
    capability: str
    effect: Literal["allow", "deny"]
    scope: dict[str, str]
    eligible_tool_names: list[str]
    eligibility_reason: str | None


class ConnectionAgentAccessOut(BaseModel):
    """Effective connector access for one agent, independent of approval policy."""

    agent_id: UUID
    agent_name: str
    authorized: bool
    authorized_tool_names: list[str]
    grants: list[ConnectionGrantSummaryOut]


class ConnectionDeleteImpactOut(BaseModel):
    """What a delete would take with the connection (plan 17.9).

    Triggers and their invocation history cascade off the connection row, so
    the delete confirmation can name the cost before anyone accepts it."""

    trigger_count: int = 0
    trigger_invocation_count: int = 0
    # Grants pinned to this connection are revoked with it (each audited),
    # so the confirmation names how many, on how many agents.
    grant_count: int = 0
    agent_count: int = 0


class ConnectionConfigUpdate(BaseModel):
    """Change the manifest-declared settings of a connection: the fields
    given are laid over the ones it has, a field omitted keeps its value,
    and a field sent empty is cleared (never a credential; those go through
    rotate)."""

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] = Field(default_factory=dict)


class ConnectionAccessSummaryOut(BaseModel):
    """Workspace-local connection access diagnostics for administrators."""

    connection_id: UUID
    agents: list[ConnectionAgentAccessOut]
    delete_impact: ConnectionDeleteImpactOut = ConnectionDeleteImpactOut()


class WebhookSetupOut(BaseModel):
    """Shown once at creation: where to point the provider and the signing
    secret to paste there. The secret is not retrievable afterwards."""

    url_path: str
    secret: str | None
    secret_mode: Literal["generated", "provider_supplied"]
    signature_algorithm: str
    help: str = ""


class WebhookSecretWrite(BaseModel):
    """Write-only provider-supplied webhook signing secret."""

    model_config = ConfigDict(strict=True, extra="forbid")

    secret: str = Field(min_length=16, max_length=4096)


class ConnectionCreated(BaseModel):
    connection: ConnectionOut
    webhook: WebhookSetupOut | None = None


class VerifyOut(BaseModel):
    ok: bool
    message: str
    status: str
    details: dict[str, str] = {}


# --- Per-connection tools and the app catalog (docs/architecture/mcp.md) ---

RiskName = Literal["read", "write", "elevated", "destructive"]


class ConnectionToolOut(BaseModel):
    """One tool reachable through a connection, with its enforced risk."""

    name: str
    provider_name: str | None = None
    description: str = ""
    risk: str
    derived_risk: str | None = None
    risk_override: str | None = None
    annotations: dict[str, Any] = {}
    input_schema: dict[str, Any] = {}
    schema_truncated: bool = False
    supports_approval: bool = False
    scope_keys: list[str] = []


class ConnectionToolsOut(BaseModel):
    connection_id: UUID
    connector_type: str
    # True when tools are discovered per connection (MCP) rather than static.
    dynamic: bool
    capability_pattern: str | None = None
    discovered_at: str | None = None
    tools: list[ConnectionToolOut] = []


class ToolRiskOverridesWrite(BaseModel):
    """Admin risk overrides keyed by tool slug; null removes an override."""

    model_config = ConfigDict(strict=True, extra="forbid")

    tool_risk_overrides: dict[str, RiskName | None] = Field(max_length=200)


class CatalogAppOut(BaseModel):
    """One Apps-library entry (public identity only; no secrets, no state)."""

    slug: str
    name: str
    category: str
    icon: str
    description: str
    # Same-origin icon-proxy path when the entry ships a logo; never the
    # upstream URL.
    logo_url: str | None = None
    connector_type: str | None = None
    mcp_url: str | None = None
    url_unverified: bool = False
    transport: str = "unknown"
    auth_hint: str = "bearer"
    auth_note: str = ""
    docs_url: str = ""
    setup_note: str = ""
    stdio_only: bool = False
    connector_config: dict[str, str] = {}
