"""Immutable value objects for one OAuth authorization.

Every one of these is frozen and slotted: a metadata document, a client
registration, or a token set is a fact about a moment, and nothing downstream
may edit one in place. A :class:`AuthorizationServerMetadata` in hand is a
document whose URLs have already passed Jhin's SSRF policy — it is only
constructible through :func:`jhin_oauth.discovery.parse_authorization_server_metadata`
or :func:`jhin_connectors.oauth_providers.provider_metadata`, both of which
validate before they build.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProtectedResourceMetadata:
    """RFC 9728 protected-resource metadata for one MCP server."""

    resource: str
    authorization_servers: tuple[str, ...]
    scopes_supported: tuple[str, ...] = ()
    source_url: str = ""
    """Which well-known or ``WWW-Authenticate`` URL produced this document."""


@dataclass(frozen=True, slots=True)
class AuthorizationServerMetadata:
    """RFC 8414 / OpenID Discovery metadata, already bounded and validated."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    device_authorization_endpoint: str | None = None
    scopes_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    grant_types_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    authorization_response_iss_parameter_supported: bool = False
    client_id_metadata_document_supported: bool = False

    def supports_dcr(self) -> bool:
        """Whether RFC 7591 dynamic client registration is on offer."""
        return bool(self.registration_endpoint)

    def supports_refresh(self) -> bool:
        """Whether refresh tokens are on offer.

        An empty ``grant_types_supported`` is not a denial: RFC 8414 makes the
        field optional and its default (``authorization_code`` and
        ``implicit``) predates refresh being ubiquitous. Assuming refresh works
        and finding out at the token endpoint costs one failed refresh;
        assuming it does not costs the user a re-authorization every hour.
        """
        return not self.grant_types_supported or "refresh_token" in self.grant_types_supported


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """How Jhin identifies itself to one authorization server.

    ``client_secret`` is ``None`` for the public + PKCE clients Jhin asks for
    by default; fewer secrets at rest is strictly better.
    """

    client_id: str
    client_secret: str | None = None
    token_endpoint_auth_method: str = "none"
    registration_access_token: str | None = None
    registration_client_uri: str | None = None
    client_secret_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PkcePair:
    """RFC 7636 code verifier and its ``S256`` challenge.

    ``method`` is typed to the single value Jhin will ever send: ``plain``
    cannot be produced by any function in this package.
    """

    verifier: str
    challenge: str
    method: Literal["S256"] = "S256"


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """One token endpoint success, with lifetimes resolved to instants.

    Expiries arrive from providers as relative seconds; they are turned into
    absolute UTC instants here so nothing downstream has to remember when the
    request was made.
    """

    access_token: str
    token_type: str = "Bearer"
    refresh_token: str | None = None
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    scope: str = ""
    issuer: str = ""


@dataclass(frozen=True, slots=True)
class DeviceCodeGrant:
    """RFC 8628 device authorization, as shown to the person approving it."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: datetime
    interval_seconds: int = 5


@dataclass(frozen=True, slots=True)
class DeviceTokenPending:
    """The device grant is not approved yet; poll again after the interval."""

    reason: Literal["authorization_pending", "slow_down"]
    interval_seconds: int


@dataclass(frozen=True, slots=True)
class McpAuthProbe:
    """What one unauthenticated probe of an MCP endpoint learned."""

    server_url: str
    requires_auth: bool
    resource_metadata_url: str | None
    challenge_scope: str | None
    protected_resource: ProtectedResourceMetadata | None
    authorization_server: AuthorizationServerMetadata | None
    supports_oauth: bool
    supports_dcr: bool
    failure_reason: str | None
    """A constant from :data:`jhin_oauth.discovery.PROBE_FAILURE_REASONS`,
    never text the probed server chose."""


__all__ = [
    "AuthorizationServerMetadata",
    "ClientCredentials",
    "DeviceCodeGrant",
    "DeviceTokenPending",
    "McpAuthProbe",
    "PkcePair",
    "ProtectedResourceMetadata",
    "TokenResponse",
]
