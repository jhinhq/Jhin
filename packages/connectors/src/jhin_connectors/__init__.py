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
)
from jhin_connectors.execution import (
    ConnectionResolutionError,
    ResolvedConnection,
    resolve_connection,
)
from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)
from jhin_connectors.registry import (
    DEFAULT_CONNECTORS,
    ConnectorRegistry,
    build_default_catalog,
    default_registry,
)

__all__ = [
    "DEFAULT_CONNECTORS",
    "AuthSchemeSpec",
    "ConfigFieldSpec",
    "ConnectionHealth",
    "ConnectionResolutionError",
    "Connector",
    "ConnectorError",
    "ConnectorManifest",
    "ConnectorRegistry",
    "NormalizedEvent",
    "RawWebhookEvent",
    "ResolvedConnection",
    "SecretFieldSpec",
    "VerifyContext",
    "build_default_catalog",
    "default_registry",
    "resolve_connection",
]
