"""Request and response models for the OAuth surface
(``docs/architecture/oauth.md``).

One rule governs every model here and it is meant to be grep-able: **no
response model has a field that carries credential material.** Not an access
token, not a refresh token, not a client secret, not an authorization code,
not a device code. ``OAuthClientOut`` returns ``client_id``, which is public
by definition, and a ``client_secret_configured`` boolean instead of the
secret. ``OAuthDeviceStartOut`` returns the ``user_code`` a person types into
a provider's website — worthless without the device code, which stays
encrypted on the server and is never serialized.

Request models that *do* carry credential material (``OAuthStartIn``,
``OAuthClientCreate``) are read through the sensitive-body path the
connections router already uses, so they never pass through the generic
request logging and validation-error machinery.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jhin_api.connections.schemas import ConnectionOut

#: How the connect panel should proceed for one connector.
ConnectMethod = Literal[
    "oauth_discovery",
    "oauth_static",
    "device_code",
    "oauth_needs_client",
    "api_key",
]

RegistrationSource = Literal["dcr", "manual", "static"]
TokenEndpointAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]

#: Why one sign-in flow cannot start, from Jhin's own vocabulary. Empty when
#: it can. ``needs_client_secret`` means a registration exists but cannot do
#: the browser redirect: a confidential provider with no stored secret.
ProbeFlowReason = Literal[
    "",
    "needs_client_credentials",
    "needs_client_secret",
    "no_device_endpoint",
]


class ProbeFlow(BaseModel):
    """Whether one sign-in flow can start for this workspace, and if not, why.

    ``reason`` is a constant from Jhin's own vocabulary, never provider text.
    """

    available: bool
    reason: ProbeFlowReason = ""


def _unavailable_flow() -> ProbeFlow:
    return ProbeFlow(available=False)


class OAuthRedirectOut(BaseModel):
    """The redirect URI an operator pastes into a provider's app settings.

    Shown verbatim, with a copy button, on every bring-your-own-app screen.
    ``is_loopback`` exists so the UI can say the honest thing on a laptop
    install: a provider that redirects the browser can still send it back to
    this machine, but one-click GitHub App creation needs the origin
    allow-listed, which ``github_app_available`` reports.
    """

    redirect_uri: str
    github_app_redirect_uri: str
    is_https: bool
    is_loopback: bool
    configured_via: Literal["OAUTH_REDIRECT_BASE_URL", "APP_URL"]
    #: False when the outbound URL policy refuses this instance's own origin
    #: (loopback / plain http not allow-listed): the manifest cannot be built,
    #: and the web app offers the by-hand registration instead.
    github_app_available: bool
    #: ``app_permissions()`` — the permissions a manifest asks for — so a
    #: by-hand setup never drifts from what the manifest would have requested.
    github_app_permissions: dict[str, str]
    #: Which flow Connect offers first for a native provider that can do both.
    preferred_sign_in: Literal["redirect", "device_code"]


class OAuthProbeIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    connector_type: str = Field(min_length=1, max_length=50)
    #: Required when ``connector_type`` is ``mcp``: the server to ask.
    server_url: str | None = Field(default=None, max_length=2000)


class OAuthProbeOut(BaseModel):
    """What asking the server (never guessing from a catalog hint) established."""

    method: ConnectMethod
    supports_oauth: bool
    supports_dcr: bool
    issuer: str = ""
    #: Host only, for the "Jhin will ask <host> for permission" sentence. A
    #: full URL in that sentence is a phishing surface; a host is a fact.
    authorization_server_display: str = ""
    scopes: list[str] = []
    resource: str = ""
    #: True when this workspace already has a usable client at this issuer, so
    #: the panel can skip straight to consent.
    client_configured: bool = False
    requires_client_secret: bool = False
    #: A constant from the service's own vocabulary. Never provider text.
    reason: str = ""
    #: The browser redirect (authorization-code) flow. ``method`` names the
    #: preferred flow; this and ``device_flow`` say which ones can start.
    redirect_flow: ProbeFlow = Field(default_factory=_unavailable_flow)
    #: RFC 8628. Available when the provider has a device endpoint and a
    #: client is registered for this workspace.
    device_flow: ProbeFlow = Field(default_factory=_unavailable_flow)
    #: Where a person manages the apps they own at this provider — from the
    #: provider table, validated on the way out, https. Rendered as a link the
    #: person may open; the web app never navigates to it. Empty when unknown.
    app_settings_url: str = ""


class OAuthStartIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    connector_type: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    #: Non-secret manifest configuration only; the pending-authorization store
    #: refuses anything credential-shaped.
    config: dict[str, Any] = Field(default_factory=dict)
    #: Set to re-authorize an existing connection rather than create one.
    connection_id: UUID | None = None
    #: Names a static provider (``linear``, ``notion``, …) for native connectors.
    provider_key: str | None = Field(default=None, max_length=50)


class OAuthStartOut(BaseModel):
    """Everything the browser needs to leave, and nothing it should not have."""

    #: The provider URL to navigate to. Carries ``client_id``, ``state``, the
    #: PKCE challenge, scope, and resource — no secret of any kind.
    authorization_url: str
    state_expires_at: datetime
    issuer: str
    scopes: list[str]
    resource: str
    #: Whose provider account is about to be used. The consent step names this
    #: person, because every agent granted this connection acts as them.
    authorized_as_user_id: UUID
    client_source: RegistrationSource


class OAuthDeviceStartIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    connector_type: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class OAuthDeviceStartOut(BaseModel):
    """The device-flow display payload.

    ``handle`` is Jhin's opaque poll token, not the provider's ``device_code``
    — that stays encrypted server-side. ``user_code`` is meant to be read
    aloud and typed into a provider's website; on its own it authorizes
    nothing.
    """

    handle: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_at: datetime
    interval_seconds: int


class OAuthDevicePollIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    handle: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")


class OAuthDevicePollOut(BaseModel):
    status: Literal["pending", "slow_down", "connected", "denied", "expired"]
    interval_seconds: int | None = None
    #: Populated only on ``connected``.
    connection: ConnectionOut | None = None


class OAuthClientCreate(BaseModel):
    """An admin registering Jhin by hand at a server with no dynamic registration."""

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    issuer: str = Field(min_length=1, max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str | None = Field(default=None, max_length=4096)
    token_endpoint_auth_method: TokenEndpointAuthMethod = "none"
    scopes: str = Field(default="", max_length=2048)


class OAuthClientOut(BaseModel):
    """One stored registration. Never the secret — only whether there is one."""

    id: UUID
    issuer: str
    redirect_uri: str
    client_id: str
    client_secret_configured: bool
    token_endpoint_auth_method: str
    source: RegistrationSource
    scopes: str
    created_at: datetime
    last_used_at: datetime | None
    #: How many connections currently depend on this registration, so an admin
    #: about to delete one can see what it would strand.
    connection_count: int


class GitHubAppManifestIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    #: GitHub's own ceiling on an app name.
    app_name: str = Field(min_length=1, max_length=34)
    organization: str | None = Field(default=None, max_length=100)


class GitHubAppManifestOut(BaseModel):
    """The form the browser POSTs to GitHub to create this instance's own app."""

    post_url: str
    manifest: dict[str, Any]
    #: The raw pending-authorization handle; GitHub echoes it back to the
    #: callback, which is what binds the created app to this admin's session.
    state: str
    expires_at: datetime
