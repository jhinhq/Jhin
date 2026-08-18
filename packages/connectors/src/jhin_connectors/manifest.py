"""Connector manifests (plan 11.1): everything the platform and UI need to
know about a connector without importing its implementation details —
display name, icon, auth schemes with their secret fields, public config
fields, webhook capabilities, and the permission scopes it registers.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConfigFieldKind = Literal["text", "integer", "boolean", "string_list"]
WebhookSecretMode = Literal["none", "generated", "provider_supplied"]


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

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    required: bool = False
    placeholder: str = ""
    help: str = ""
    kind: ConfigFieldKind = "text"
    auth_types: tuple[str, ...] = ()
    default: Any | None = None
    minimum: int | None = None
    maximum: int | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if (self.minimum is not None or self.maximum is not None) and self.kind != "integer":
            raise ValueError("minimum and maximum apply only to integer config fields")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot be greater than maximum")
        return self


class ConnectorManifest(BaseModel):
    """Static declaration of one connector (plan 11.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_type: str
    display_name: str
    icon: str
    description: str = ""
    auth_schemes: tuple[AuthSchemeSpec, ...] = ()
    config_fields: tuple[ConfigFieldSpec, ...] = ()
    # Provider webhook event names this connector accepts (empty = no webhooks).
    webhook_events: tuple[str, ...] = ()
    # Canonical ``connector.<type>.*`` event types normalization can produce —
    # the trigger builder's WHEN dropdown (plan 17.10). Free-text event types
    # remain allowed for connectors with dynamic suffixes.
    canonical_events: tuple[str, ...] = ()
    # Capability names (plan 12.3) whose tools this connector registers.
    capabilities: tuple[str, ...] = ()
    docs_url: str = ""
    webhook_secret_mode: WebhookSecretMode = "none"
    webhook_signature_algorithm: str = ""
    webhook_setup_help: str = ""
    supports_webhooks: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def _derive_legacy_webhook_fields(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        if "webhook_secret_mode" not in values:
            values["webhook_secret_mode"] = (
                "generated" if values.get("supports_webhooks") is True else "none"
            )
        if "supports_webhooks" not in values:
            values["supports_webhooks"] = values["webhook_secret_mode"] != "none"
        return values

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        auth_types = [scheme.type for scheme in self.auth_schemes]
        if len(auth_types) != len(set(auth_types)):
            raise ValueError("auth scheme types must be unique")

        config_names = [field.name for field in self.config_fields]
        if len(config_names) != len(set(config_names)):
            raise ValueError("config field names must be unique")
        known_auth_types = set(auth_types)
        for config_field in self.config_fields:
            unknown_auth_types = set(config_field.auth_types) - known_auth_types
            if unknown_auth_types:
                raise ValueError(
                    f"config field {config_field.name!r} references an unknown auth type"
                )

        derived_supports_webhooks = self.webhook_secret_mode != "none"
        if self.supports_webhooks is not derived_supports_webhooks:
            raise ValueError("supports_webhooks must match webhook_secret_mode")
        if self.webhook_events and not derived_supports_webhooks:
            raise ValueError("connectors with webhook events require a webhook_secret_mode")
        if derived_supports_webhooks and not self.webhook_signature_algorithm.strip():
            raise ValueError("webhook connectors require a webhook_signature_algorithm")
        return self

    def auth_scheme(self, auth_type: str) -> AuthSchemeSpec | None:
        for scheme in self.auth_schemes:
            if scheme.type == auth_type:
                return scheme
        return None


def normalize_config(
    manifest: ConnectorManifest,
    auth_type: str,
    submitted: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize public connector settings for one selected auth scheme.

    Only fields declared for the selected auth scheme are accepted.  Error
    messages name the public field but never interpolate submitted values.
    """
    if manifest.auth_scheme(auth_type) is None:
        raise ValueError(f"unknown connector auth type: {auth_type!r}")

    applicable = {
        config_field.name: config_field
        for config_field in manifest.config_fields
        if not config_field.auth_types or auth_type in config_field.auth_types
    }
    submitted_names = set(submitted)
    unknown_names = submitted_names - set(applicable)
    if unknown_names:
        rendered_names = ", ".join(sorted(str(name) for name in unknown_names))
        raise ValueError(
            f"config fields are not allowed for auth type {auth_type!r}: {rendered_names}"
        )

    normalized: dict[str, Any] = {}
    for name, config_field in applicable.items():
        if name in submitted:
            raw_value = submitted[name]
        elif config_field.default is not None:
            raw_value = deepcopy(config_field.default)
        elif config_field.required:
            raise ValueError(f"config field {name!r} is required")
        else:
            continue

        normalized_value = _normalize_field_value(config_field, raw_value)
        if config_field.required and _is_empty(normalized_value):
            raise ValueError(f"config field {name!r} is required")
        normalized[name] = normalized_value
    return normalized


def _normalize_field_value(config_field: ConfigFieldSpec, raw_value: Any) -> Any:
    name = config_field.name
    if config_field.kind == "text":
        if not isinstance(raw_value, str):
            raise ValueError(f"config field {name!r} must be text")
        return raw_value

    if config_field.kind == "integer":
        if isinstance(raw_value, bool):
            raise ValueError(f"config field {name!r} must be an integer")
        if isinstance(raw_value, int):
            value = raw_value
        elif isinstance(raw_value, str) and _is_base_ten_integer(raw_value):
            value = int(raw_value, 10)
        else:
            raise ValueError(f"config field {name!r} must be an integer")
        if config_field.minimum is not None and value < config_field.minimum:
            raise ValueError(f"config field {name!r} is below its minimum")
        if config_field.maximum is not None and value > config_field.maximum:
            raise ValueError(f"config field {name!r} is above its maximum")
        return value

    if config_field.kind == "boolean":
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value == "true":
            return True
        if raw_value == "false":
            return False
        raise ValueError(f"config field {name!r} must be a boolean")

    if config_field.kind == "string_list":
        if not isinstance(raw_value, list | tuple) or any(
            not isinstance(item, str) or not item for item in raw_value
        ):
            raise ValueError(f"config field {name!r} must be a list of nonempty strings")
        return list(raw_value)

    raise ValueError(f"config field {name!r} has an unsupported kind")


def _is_base_ten_integer(value: str) -> bool:
    unsigned = value[1:] if value[:1] in {"+", "-"} else value
    return bool(unsigned) and unsigned.isascii() and unsigned.isdecimal()


def _is_empty(value: Any) -> bool:
    return (isinstance(value, str) and not value) or (isinstance(value, list) and not value)
