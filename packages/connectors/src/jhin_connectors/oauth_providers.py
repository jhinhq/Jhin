"""The authorization servers Jhin ships knowledge of, because they publish none.

An MCP server is asked how it signs in: one unauthenticated request returns
RFC 9728 resource metadata and RFC 8414 server metadata, and
:func:`jhin_oauth.discovery.parse_authorization_server_metadata` turns the
answer into a validated :class:`~jhin_oauth.types.AuthorizationServerMetadata`.
A native connector cannot be asked. GitHub publishes no discovery document at
all, so the endpoints have to come from somewhere, and the honest place is a
table this repository owns and reviews rather than a guess assembled at call
time.

That is the whole purpose of this module, and it draws the line it does for a
reason. An entry here is a **fact about a provider's protocol** — where its
authorize, token, and device endpoints live, whether it authenticates a
confidential client, what scopes to ask for by default. It is never a
credential: which client id this workspace registered, and any secret beside
it, live encrypted in ``OAuthClientStore`` and are looked up by ``issuer``.
The two are joined by that issuer string, which is why ``issuer`` here must
stay byte-identical to the value the rest of the system keys registrations by.

Every URL is put through :func:`jhin_oauth.urls.validate_oauth_url` on the way
out, not on the way in. A module-level constant that raised on import would
take the whole API process down for a policy the operator can legitimately
change at runtime (``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`` is read per call,
which is what lets the test suite point a provider at a loopback fake), so
:func:`provider_metadata` validates at the moment of use and raises
:class:`~jhin_connectors.endpoints.EndpointPolicyError` there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from jhin_connectors.github.oauth import (
    AUTHORIZE_URL as GITHUB_AUTHORIZE_URL,
)
from jhin_connectors.github.oauth import (
    DEVICE_CODE_URL as GITHUB_DEVICE_CODE_URL,
)
from jhin_connectors.github.oauth import (
    DEVICE_TOKEN_URL as GITHUB_TOKEN_URL,
)
from jhin_connectors.github.oauth import (
    GITHUB_ISSUER,
)
from jhin_oauth.types import AuthorizationServerMetadata
from jhin_oauth.urls import validate_oauth_url

_NO_PARAMS: Final[Mapping[str, str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class StaticOAuthProvider:
    """One provider's protocol facts, known ahead of time rather than discovered."""

    key: str
    """Stable handle a caller names this provider by (``OAuthStartIn.provider_key``)."""

    connector_type: str
    """The connector this provider signs in for, e.g. ``"github"``."""

    issuer: str
    """The identity client registrations are keyed by. Byte-identical everywhere."""

    authorization_endpoint: str
    token_endpoint: str

    device_authorization_endpoint: str = ""
    """Empty when the provider offers no device flow. Truthiness is the test:
    it is what routes a connector to the device path instead of a redirect."""

    revocation_endpoint: str = ""
    """RFC 7009 endpoint, empty when the provider publishes none.

    Empty is the common answer and a real one, not a gap: GitHub retires a
    token through an authenticated REST call on the app, not through a
    standard revocation endpoint, so there is nothing conformant to put here.
    A connection whose provider has no endpoint still has its stored copy
    destroyed on delete — see ``ConnectionTokenService.revoke_and_clear``,
    which treats revocation as best-effort and erasure as mandatory."""

    default_scopes: tuple[str, ...] = ()
    """What to ask for when nothing narrower is known. Empty is a real answer,
    not a missing one — a GitHub App's access comes from the permissions it was
    installed with, so a scope list would be noise the provider ignores."""

    requires_client_secret: bool = True
    """Whether the redirect flow needs a confidential client. Never consulted
    on the device path, which has no client secret at start, poll, or refresh."""

    supports_refresh: bool = True

    extra_authorize_params: Mapping[str, str] = _NO_PARAMS
    """Non-standard query parameters this provider's authorize URL needs."""


#: Keyed by :attr:`StaticOAuthProvider.key`. GitHub is the only entry because
#: it is the only native connector Jhin can sign a person into today; every
#: other connector authenticates with a key the operator supplies, and
#: answering "OAuth" for one of those would be a promise the code cannot keep.
#: A connector with no entry is not an error — it is how a caller learns the
#: connector has no OAuth, and it is the answer ``probe`` reports as
#: ``connector_has_no_oauth``.
STATIC_PROVIDERS: Final[Mapping[str, StaticOAuthProvider]] = MappingProxyType(
    {
        "github": StaticOAuthProvider(
            key="github",
            connector_type="github",
            issuer=GITHUB_ISSUER,
            authorization_endpoint=GITHUB_AUTHORIZE_URL,
            token_endpoint=GITHUB_TOKEN_URL,
            device_authorization_endpoint=GITHUB_DEVICE_CODE_URL,
            # A GitHub App carries its own permissions; user-to-server tokens
            # are bounded by the installation, not by a scope string.
            default_scopes=(),
            # Only the redirect flow needs it. The app-manifest provisioning
            # path hands Jhin a client secret precisely so this can be true.
            requires_client_secret=True,
        )
    }
)


def provider_metadata(provider: StaticOAuthProvider) -> AuthorizationServerMetadata:
    """The metadata document this provider would have published, had it one.

    Built here rather than parsed, and validated on the way out so that the
    result carries the same guarantee a discovered document does: every URL in
    an :class:`~jhin_oauth.types.AuthorizationServerMetadata` has already
    passed Jhin's outbound policy. Callers downstream — the authorize-URL
    builder, the token exchange, the refresher — may therefore treat a
    constructed document and a discovered one identically, which is the point.

    ``registration_endpoint`` is left ``None`` on purpose: a provider is in
    this table *because* it does not do RFC 7591 dynamic registration, so
    :meth:`~jhin_oauth.types.AuthorizationServerMetadata.supports_dcr` must say
    no and the caller must fall back to a client the workspace registered.

    Raises :class:`~jhin_connectors.endpoints.EndpointPolicyError` when the
    outbound policy refuses one of the provider's URLs.
    """
    grant_types = ["authorization_code"]
    if provider.device_authorization_endpoint:
        grant_types.append("urn:ietf:params:oauth:grant-type:device_code")
    if provider.supports_refresh:
        grant_types.append("refresh_token")

    return AuthorizationServerMetadata(
        issuer=validate_oauth_url(provider.issuer, kind=f"{provider.key} issuer"),
        authorization_endpoint=validate_oauth_url(
            provider.authorization_endpoint, kind=f"{provider.key} authorization endpoint"
        ),
        token_endpoint=validate_oauth_url(
            provider.token_endpoint, kind=f"{provider.key} token endpoint"
        ),
        registration_endpoint=None,
        revocation_endpoint=(
            validate_oauth_url(
                provider.revocation_endpoint, kind=f"{provider.key} revocation endpoint"
            )
            if provider.revocation_endpoint
            else None
        ),
        device_authorization_endpoint=(
            validate_oauth_url(
                provider.device_authorization_endpoint,
                kind=f"{provider.key} device authorization endpoint",
            )
            if provider.device_authorization_endpoint
            else None
        ),
        scopes_supported=provider.default_scopes,
        # PKCE is sent to every provider regardless of what it advertises:
        # a provider that does not implement it ignores two extra parameters,
        # and one that does gets the protection. Naming S256 here keeps a
        # constructed document honest about what Jhin will actually send.
        code_challenge_methods_supported=("S256",),
        grant_types_supported=tuple(grant_types),
        token_endpoint_auth_methods_supported=(
            ("client_secret_post",) if provider.requires_client_secret else ("none",)
        ),
        # RFC 9207 is a discovery-era feature; a provider old enough to need a
        # hand-written entry does not return `iss` on the callback, and
        # claiming otherwise would make the callback reject its own redirect.
        authorization_response_iss_parameter_supported=False,
    )


def provider_for_connector(connector_type: str) -> StaticOAuthProvider | None:
    """The entry for one connector type, or ``None`` when it has no OAuth."""
    for provider in STATIC_PROVIDERS.values():
        if provider.connector_type == connector_type:
            return provider
    return None


__all__ = [
    "STATIC_PROVIDERS",
    "StaticOAuthProvider",
    "provider_for_connector",
    "provider_metadata",
]
