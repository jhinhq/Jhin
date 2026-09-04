"""OAuth 2.1 client core for Jhin (MCP authorization 2026-07-28).

Jhin is a leaf OAuth client. It exposes no authorization endpoint, no token
endpoint, and no registration endpoint to anyone, and there is exactly one
redirect URI per instance, computed from settings and never read out of a
request. Nothing in this package accepts a ``redirect_uri``, a ``client_id``,
or a return address from an untrusted caller, and nothing here forwards a user
agent anywhere a request asked it to.

See docs/architecture/oauth.md.
"""

from jhin_oauth.discovery import (
    discover_authorization_server,
    discover_protected_resource,
    parse_authorization_server_metadata,
    parse_protected_resource_metadata,
    probe_mcp_endpoint,
    select_scopes,
)
from jhin_oauth.errors import (
    ClientForgottenError,
    DeviceAuthorizationDenied,
    DeviceCodeExpired,
    DiscoveryError,
    InvalidGrantError,
    IssuerMismatchError,
    OAuthError,
    PkceUnsupportedError,
    RegistrationError,
    TokenError,
    TransientOAuthError,
)
from jhin_oauth.lifecycle import (
    REFRESH_MARGIN_SECONDS,
    ConnectionTokenService,
    RefreshOutcome,
    StoredTokens,
    needs_refresh,
    parse_token_map,
    refresh_due_connections,
    token_map,
)
from jhin_oauth.persistence import (
    ClaimRefusal,
    OAuthClientStore,
    PendingAuthorizationInvalid,
    PendingAuthorizationStore,
)
from jhin_oauth.pkce import generate_pkce, generate_state, state_hash
from jhin_oauth.registration import delete_registration, register_client
from jhin_oauth.tokens import (
    build_authorization_url,
    exchange_code,
    poll_device_token,
    refresh_access_token,
    revoke_token,
    start_device_authorization,
)
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    ClientCredentials,
    DeviceCodeGrant,
    DeviceTokenPending,
    McpAuthProbe,
    PkcePair,
    ProtectedResourceMetadata,
    TokenResponse,
)
from jhin_oauth.urls import (
    canonical_resource_uri,
    parse_www_authenticate,
    validate_oauth_url,
    well_known_as_candidates,
    well_known_prm_candidates,
)

__all__ = [
    "REFRESH_MARGIN_SECONDS",
    "AuthorizationServerMetadata",
    "ClaimRefusal",
    "ClientCredentials",
    "ClientForgottenError",
    "ConnectionTokenService",
    "DeviceAuthorizationDenied",
    "DeviceCodeExpired",
    "DeviceCodeGrant",
    "DeviceTokenPending",
    "DiscoveryError",
    "InvalidGrantError",
    "IssuerMismatchError",
    "McpAuthProbe",
    "OAuthClientStore",
    "OAuthError",
    "PendingAuthorizationInvalid",
    "PendingAuthorizationStore",
    "PkcePair",
    "PkceUnsupportedError",
    "ProtectedResourceMetadata",
    "RefreshOutcome",
    "RegistrationError",
    "StoredTokens",
    "TokenError",
    "TokenResponse",
    "TransientOAuthError",
    "build_authorization_url",
    "canonical_resource_uri",
    "delete_registration",
    "discover_authorization_server",
    "discover_protected_resource",
    "exchange_code",
    "generate_pkce",
    "generate_state",
    "needs_refresh",
    "parse_authorization_server_metadata",
    "parse_protected_resource_metadata",
    "parse_token_map",
    "parse_www_authenticate",
    "poll_device_token",
    "probe_mcp_endpoint",
    "refresh_access_token",
    "refresh_due_connections",
    "register_client",
    "revoke_token",
    "select_scopes",
    "start_device_authorization",
    "state_hash",
    "token_map",
    "validate_oauth_url",
    "well_known_as_candidates",
    "well_known_prm_candidates",
]
