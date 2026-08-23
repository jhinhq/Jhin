"""The connector interface (plan 11).

Every connector implements :class:`Connector`:

- ``manifest`` — static declaration (plan 11.1);
- ``verify_connection(ctx)`` — live credential/health check;
- ``tools()`` — tool definitions plus executors, registered into the same
  catalog and gateway the Phase 4 built-ins use (plan 12);
- ``tool_definitions()`` — the definition-only view safe for API discovery;
- ``normalize_event(raw)`` — maps raw provider webhook payloads to canonical
  ``connector.<type>.<entity>.<event>`` domain events (plan 9.2).

Credentials always arrive *decrypted for this call only* through
:class:`VerifyContext` (verification) or via ``ToolExecutionContext.crypto``
(execution) — never through prompts, never persisted in results (plan 13.5).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from jhin_connectors.manifest import ConnectorManifest
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor

_EVENT_TYPE_RE = re.compile(r"^connector(\.[a-z0-9_]+)+$")


class ConnectorError(Exception):
    """Raised for connector misconfiguration or invalid registration."""


class WebhookVerificationError(Exception):
    """A webhook delivery failed signature or shape verification. The API
    rejects it with 401 *before* any payload processing (plan 48.5).
    Messages must never include the secret or the raw body."""


class ConnectionHealth(BaseModel):
    """Result of one verification attempt (plan 6.9: status/last_error)."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    message: str
    # Small display-safe facts (authenticated login, app slug, ...). Never
    # credentials.
    details: dict[str, str] = {}


@dataclass(frozen=True)
class VerifyContext:
    """Everything ``verify_connection`` may use. ``credentials`` holds the
    decrypted secret fields for the duration of the call only."""

    auth_type: str
    credentials: dict[str, str]
    config: dict[str, Any] = field(default_factory=dict)


class RawWebhookEvent(BaseModel):
    """One accepted webhook delivery as published to the INGRESS stream."""

    model_config = ConfigDict(frozen=True)

    event: str
    delivery_id: str
    payload: dict[str, Any]


class NormalizedEvent(BaseModel):
    """One canonical domain event produced from a raw webhook payload.

    ``event_type`` must live under the ``connector.`` domain so it lands on
    the EVENTS stream (plan 9.2), e.g. ``connector.github.pull_request.opened``.
    """

    model_config = ConfigDict(frozen=True)

    event_type: str
    data: dict[str, Any]

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        if not _EVENT_TYPE_RE.match(value):
            raise ValueError(f"not a canonical connector event type: {value!r}")
        return value


class Connector(ABC):
    """Base class for all connectors (plan 11). Implementations must be
    stateless: per-connection state lives in the database and per-call state
    in the execution context."""

    manifest: ConnectorManifest

    @property
    def type(self) -> str:
        return self.manifest.connector_type

    @abstractmethod
    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        """Check the credentials against the live provider. Must never raise
        with credential material in the message; return ok=False instead."""

    @abstractmethod
    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        """Tool definitions and executors to register into the catalog."""

    @abstractmethod
    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Tool definitions without executor callables."""

    def connection_tool_definitions(self, config: Mapping[str, Any]) -> tuple[ToolDefinition, ...]:
        """Tool definitions as seen through one stored connection.

        Static connectors return ``tool_definitions()``. Connectors whose
        tools are discovered per connection (MCP) derive them from the
        connection's durable ``config_json`` instead. Never performs I/O."""
        return self.tool_definitions()

    async def refresh_discovery(self, ctx: VerifyContext) -> dict[str, Any] | None:
        """Re-discover per-connection state to persist into ``config_json``
        after a successful verification (e.g. an MCP server's tool list).
        Returns the keys to merge, or None when the connector has nothing to
        discover. Values must be display-safe and bounded — never secrets."""
        return None

    def webhook_events(self) -> tuple[str, ...]:
        """Provider event names accepted at the webhook endpoint."""
        return self.manifest.webhook_events

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Apply optional connector-specific checks after generic normalization."""
        return dict(config)

    async def fetch_metadata(self, ctx: VerifyContext) -> dict[str, Any]:
        """Display-safe provider metadata for UI pickers (e.g. Linear teams
        and workflow states for the trigger builder). Optional; connectors
        without discoverable metadata return {}. Never credentials."""
        return {}

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> RawWebhookEvent:
        """Verify one webhook delivery (signature first — plan 48.5) and
        extract its event name, delivery id, and payload. Raise
        :class:`WebhookVerificationError` on any verification failure."""
        raise WebhookVerificationError("connector does not accept webhooks")

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        """Map one raw webhook payload to canonical domain events. Unknown
        events return [] — never raise for content the provider controls."""
        return []


__all__ = [
    "ConnectionHealth",
    "Connector",
    "ConnectorError",
    "NormalizedEvent",
    "RawWebhookEvent",
    "VerifyContext",
    "WebhookVerificationError",
]
