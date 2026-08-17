"""Connector manifests (plan 11.1): everything the platform and UI need to
know about a connector without importing its implementation details —
display name, icon, auth schemes with their secret fields, public config
fields, webhook capabilities, and the permission scopes it registers.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SecretFieldSpec(BaseModel):
    """One credential field collected at connection creation. The value is
    write-only: it goes into the encrypted secret store and is never
    returned by any API."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    placeholder: str = ""
    multiline: bool = False
    required: bool = True


class AuthSchemeSpec(BaseModel):
    """One selectable auth method (plan 11): e.g. GitHub PAT vs GitHub App."""

    model_config = ConfigDict(frozen=True)

    type: str
    label: str
    description: str = ""
    secret_fields: tuple[SecretFieldSpec, ...] = ()

    def required_field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.secret_fields if field.required)


class ConfigFieldSpec(BaseModel):
    """One public (non-secret) configuration field stored in
    ``connection.config_json`` — safe to display and edit."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    required: bool = False
    placeholder: str = ""
    help: str = ""


class ConnectorManifest(BaseModel):
    """Static declaration of one connector (plan 11.1)."""

    model_config = ConfigDict(frozen=True)

    connector_type: str
    display_name: str
    icon: str
    description: str = ""
    auth_schemes: tuple[AuthSchemeSpec, ...] = ()
    config_fields: tuple[ConfigFieldSpec, ...] = ()
    # Provider webhook event names this connector accepts (empty = no webhooks).
    webhook_events: tuple[str, ...] = ()
    # Capability names (plan 12.3) whose tools this connector registers.
    capabilities: tuple[str, ...] = ()
    docs_url: str = ""

    supports_webhooks: bool = Field(default=False)

    def auth_scheme(self, auth_type: str) -> AuthSchemeSpec | None:
        for scheme in self.auth_schemes:
            if scheme.type == auth_type:
                return scheme
        return None
