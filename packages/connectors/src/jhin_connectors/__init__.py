"""Connector SDK and built-in connectors (plan section 11).

Public surface:

- :class:`Connector`, :class:`ConnectorManifest` — the contract every
  connector implements (plan 11, 11.1);
- :class:`ConnectorRegistry`, :func:`default_registry` — installed
  connectors;
- :func:`build_default_catalog` — built-in system tools plus all connector
  tools, ready for the tool gateway;
- :func:`resolve_connection` — execution-time credential resolution for
  executors (plan 13.5).
"""

from jhin_connectors.base import (
    ConnectionHealth,
    Connector,
    ConnectorError,
    NormalizedEvent,
    RawWebhookEvent,
    VerifyContext,
    WebhookVerificationError,
)
from jhin_connectors.endpoints import (
    EndpointPolicyError,
    validate_http_origin,
    validate_postgres_target,
)
from jhin_connectors.execution import (
    ConnectionResolutionError,
    ResolvedConnection,
    resolve_connection,
)
from jhin_connectors.http_client import (
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderHTTPError,
    send_bounded_json,
)
from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldKind,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
    WebhookSecretMode,
    normalize_config,
)
from jhin_connectors.registry import (
    DEFAULT_CONNECTORS,
    ConnectorRegistry,
    build_default_catalog,
    default_registry,
)

__all__ = [
    "DEFAULT_CONNECTORS",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "AuthSchemeSpec",
    "ConfigFieldKind",
    "ConfigFieldSpec",
    "ConnectionHealth",
    "ConnectionResolutionError",
    "Connector",
    "ConnectorError",
    "ConnectorManifest",
    "ConnectorRegistry",
    "EndpointPolicyError",
    "NormalizedEvent",
    "ProviderHTTPError",
    "RawWebhookEvent",
    "ResolvedConnection",
    "SecretFieldSpec",
    "VerifyContext",
    "WebhookSecretMode",
    "WebhookVerificationError",
    "build_default_catalog",
    "default_registry",
    "normalize_config",
    "resolve_connection",
    "send_bounded_json",
    "validate_http_origin",
    "validate_postgres_target",
]
