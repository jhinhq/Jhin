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
