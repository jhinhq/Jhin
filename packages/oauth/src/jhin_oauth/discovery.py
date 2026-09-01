"""Discovery: what an MCP endpoint needs, and who is allowed to authorize it.

Three untrusted documents in sequence — a ``WWW-Authenticate`` challenge, an
RFC 9728 protected-resource document, an RFC 8414 authorization-server
document — each one telling Jhin where to look next. Every field is bounded
before it is read and every URL is put through
:func:`jhin_oauth.urls.validate_oauth_url` *at parse time*, so an
:class:`~jhin_oauth.types.AuthorizationServerMetadata` in hand is a document
whose endpoints have already passed policy. Nothing downstream re-checks,
because nothing downstream has to.

Two refusals here are absolute:

- an ``issuer`` that does not byte-match the one requested aborts the flow
  instead of advancing to the next candidate — a mismatched document is an
  attack signal, not a stale mirror;
- an authorization server that does not advertise ``S256`` is refused, absent
  or not. There is no setting that turns that off.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

import httpx

from jhin_domain.endpoints import EndpointPolicyError
from jhin_oauth._http import (
    MAX_METADATA_BYTES as _MAX_METADATA_BYTES,
)
from jhin_oauth._http import (
    BoundedHttpError,
    BoundedResponse,
    build_json_get,
    send_bounded,
)
from jhin_oauth.errors import DiscoveryError, IssuerMismatchError, PkceUnsupportedError
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    McpAuthProbe,
    ProtectedResourceMetadata,
)
from jhin_oauth.urls import (
    canonical_resource_uri,
    parse_www_authenticate,
    same_origin,
    validate_oauth_url,
    well_known_as_candidates,
    well_known_prm_candidates,
)
from jhin_observability import get_logger

logger = get_logger(__name__)

MAX_METADATA_BYTES: int = _MAX_METADATA_BYTES
MAX_SCOPE_ENTRIES: int = 128
MAX_AUTHORIZATION_SERVERS: int = 16

MAX_STRING_ENTRY_LENGTH: Final[int] = 128
MAX_RESOURCE_LENGTH: Final[int] = 1_000
MAX_SCOPE_STRING_LENGTH: Final[int] = 2_048
MAX_SELECTED_SCOPES: Final[int] = 64

# Jhin never asks for these, whatever a server advertises: a wildcard grant is
# the opposite of the scope minimisation the whole design turns on.
FORBIDDEN_SCOPES: Final[frozenset[str]] = frozenset({"*", "all", "full-access"})
OFFLINE_ACCESS_SCOPE: Final[str] = "offline_access"

# RFC 6749 §3.3 scope-token characters, minus the space that separates them.
_SCOPE_TOKEN_ALLOWED = frozenset(
    chr(code) for code in range(0x21, 0x7F) if chr(code) not in {'"', "\\"}
)

# Constant vocabulary for McpAuthProbe.failure_reason. A probed server never
# gets to choose text that Jhin will later render.
PROBE_FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "unreachable",
        "no_authentication_required",
        "no_protected_resource_metadata",
        "no_authorization_server",
        "issuer_mismatch",
        "pkce_unsupported",
        "resource_mismatch",
    }
)

# One unauthenticated JSON-RPC initialize. The version is pinned here rather
# than imported from the MCP SDK so this package stays a pure OAuth core; a
# server that dislikes the version still answers 401 if it needs auth, which
# is the only part of the response this probe reads.
MCP_PROTOCOL_VERSION: Final[str] = "2025-11-25"
_INITIALIZE_BODY: Final[dict[str, Any]] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "Jhin", "version": "1"},
    },
}


def _bounded_strings(
    value: object,
    *,
    max_entries: int = MAX_SCOPE_ENTRIES,
    max_length: int = MAX_STRING_ENTRY_LENGTH,
) -> tuple[str, ...]:
    """A JSON string array, truncated and filtered rather than trusted.

    Non-string entries are dropped, over-long entries are dropped, and the
    array is cut at ``max_entries``. A non-array yields an empty tuple.
    """
    if not isinstance(value, list):
        return ()
    entries: list[str] = []
    for entry in value[:max_entries]:
        if isinstance(entry, str) and 0 < len(entry) <= max_length:
            entries.append(entry)
    return tuple(entries)


def _json_true(value: object) -> bool:
    """Only a real JSON ``true``. ``"true"``, ``1``, and ``[]`` are not."""
    return value is True


def _optional_url(document: dict[str, Any], field: str, *, kind: str) -> str | None:
    """An optional endpoint, or ``None`` when it is absent or unusable.

    A registration endpoint Jhin may not fetch should degrade the connection
    to "no dynamic registration", not kill it — so a policy failure here is a
    dropped field and a DEBUG line, never an exception.
    """
    raw = document.get(field)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return validate_oauth_url(raw, kind=kind)
    except EndpointPolicyError:
        logger.debug("oauth.metadata_field_refused", field=field)
        return None


def _required_url(document: dict[str, Any], field: str, *, kind: str) -> str:
    raw = document.get(field)
    if not isinstance(raw, str) or not raw:
        raise DiscoveryError(f"the authorization server metadata document has no {kind}")
    try:
        return validate_oauth_url(raw, kind=kind)
    except EndpointPolicyError:
        raise DiscoveryError(
            f"the authorization server's {kind} is not an allowed target"
        ) from None


def parse_protected_resource_metadata(
    document: object, *, source_url: str
) -> ProtectedResourceMetadata:
    """One RFC 9728 document, bounded and validated.

    ``resource`` is stored in its canonical RFC 8707 form, which is what makes
    it comparable to the audience check the MCP executor performs before every
    authorized call.

    Raises :class:`~jhin_oauth.errors.DiscoveryError`.
    """
    if not isinstance(document, dict):
        raise DiscoveryError("the protected resource metadata document is not a JSON object")
    raw_resource = document.get("resource")
    if (
        not isinstance(raw_resource, str)
        or not raw_resource
        or len(raw_resource) > MAX_RESOURCE_LENGTH
    ):
        raise DiscoveryError("the protected resource metadata document names no resource")
    try:
        resource = canonical_resource_uri(raw_resource)
    except ValueError:
        raise DiscoveryError(
            "the protected resource metadata document names an unusable resource"
        ) from None

    servers: list[str] = []
    raw_servers = document.get("authorization_servers")
    if isinstance(raw_servers, list):
        for entry in raw_servers[:MAX_AUTHORIZATION_SERVERS]:
            if not isinstance(entry, str) or not entry:
                continue
            try:
                servers.append(validate_oauth_url(entry, kind="authorization server issuer"))
            except EndpointPolicyError:
                logger.debug("oauth.authorization_server_refused")

    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=tuple(dict.fromkeys(servers)),
        scopes_supported=_bounded_strings(document.get("scopes_supported")),
        source_url=source_url,
    )


def parse_authorization_server_metadata(
    document: object, *, expected_issuer: str
) -> AuthorizationServerMetadata:
    """One RFC 8414 / OpenID Discovery document, bounded and validated.

    The ``issuer`` comparison is byte-identical with no normalization of any
    kind — no case folding, no default-port elision, no trailing-slash or
    percent-encoding fixups. RFC 8414 §3.3 makes that a MUST, and every
    "helpful" normalization is a way for two different servers to look like
    one.

    Raises :class:`~jhin_oauth.errors.DiscoveryError` and
    :class:`~jhin_oauth.errors.IssuerMismatchError`.
    """
    if not isinstance(document, dict):
        raise DiscoveryError("the authorization server metadata document is not a JSON object")
    issuer = document.get("issuer")
    if not isinstance(issuer, str) or issuer != expected_issuer:
        raise IssuerMismatchError(
            "the authorization server metadata document claims a different issuer"
        )

    return AuthorizationServerMetadata(
        issuer=issuer,
        authorization_endpoint=_required_url(
            document, "authorization_endpoint", kind="authorization endpoint"
        ),
        token_endpoint=_required_url(document, "token_endpoint", kind="token endpoint"),
        registration_endpoint=_optional_url(
            document, "registration_endpoint", kind="client registration endpoint"
        ),
        revocation_endpoint=_optional_url(
            document, "revocation_endpoint", kind="token revocation endpoint"
        ),
        device_authorization_endpoint=_optional_url(
            document, "device_authorization_endpoint", kind="device authorization endpoint"
        ),
        scopes_supported=_bounded_strings(document.get("scopes_supported")),
        code_challenge_methods_supported=_bounded_strings(
            document.get("code_challenge_methods_supported")
        ),
        grant_types_supported=_bounded_strings(document.get("grant_types_supported")),
        token_endpoint_auth_methods_supported=_bounded_strings(
            document.get("token_endpoint_auth_methods_supported")
        ),
        authorization_response_iss_parameter_supported=_json_true(
            document.get("authorization_response_iss_parameter_supported")
        ),
        client_id_metadata_document_supported=_json_true(
            document.get("client_id_metadata_document_supported")
        ),
    )


async def _fetch_metadata(client: httpx.AsyncClient, url: str) -> Any:
    """One metadata GET, or ``None`` when this candidate is not usable.

    A non-2xx, a non-JSON content type, an unparseable body, an oversized body,
    a redirect, and a transport failure are all the same answer here: try the
    next candidate. Nothing about *which* of them happened is worth telling a
    caller, and telling one would leak the shape of the probed network.
    """
    try:
        response: BoundedResponse = await send_bounded(
            client, build_json_get(client, url), max_response_bytes=MAX_METADATA_BYTES
        )
    except BoundedHttpError:
        logger.debug("oauth.metadata_fetch_failed")
        return None
    if not response.is_success or not response.is_json or response.payload is None:
        logger.debug("oauth.metadata_candidate_skipped", status_code=response.status_code)
        return None
    return response.payload


async def discover_protected_resource(
    client: httpx.AsyncClient,
    server_url: str,
    *,
    resource_metadata_url: str | None = None,
) -> ProtectedResourceMetadata:
    """The RFC 9728 document for one MCP endpoint.

    The URL from a ``WWW-Authenticate`` challenge is tried first when the
    server offered one, then the well-known candidates in RFC 9728 §3 order.
    A document whose ``resource`` does not cover the endpoint being probed —
    a different origin, or a path the endpoint does not sit under — is
    discarded rather than believed: it is how a server would hand Jhin an
    audience belonging to somebody else.

    Raises :class:`~jhin_oauth.errors.DiscoveryError` when none resolve.
    """
    endpoint = validate_oauth_url(server_url, kind="MCP server URL")
    candidates: list[str] = []
    if resource_metadata_url:
        try:
            candidates.append(
                validate_oauth_url(resource_metadata_url, kind="protected resource metadata URL")
            )
        except EndpointPolicyError:
            logger.debug("oauth.challenge_metadata_url_refused")
    candidates.extend(well_known_prm_candidates(endpoint))

    for candidate in dict.fromkeys(candidates):
        document = await _fetch_metadata(client, candidate)
        if document is None:
            continue
        try:
            metadata = parse_protected_resource_metadata(document, source_url=candidate)
        except DiscoveryError:
            logger.debug("oauth.protected_resource_document_rejected")
            continue
        if not _resource_covers(metadata.resource, endpoint):
            logger.debug("oauth.protected_resource_scope_mismatch")
            continue
        return metadata
    raise DiscoveryError("this server publishes no usable protected resource metadata")


def _resource_covers(resource: str, server_url: str) -> bool:
    """Whether a PRM ``resource`` legitimately covers the probed endpoint.

    RFC 9728 lets one document cover a resource and the paths beneath it, so
    ``https://host`` covers ``https://host/mcp``. It never covers another
    origin.
    """
    if not same_origin(resource, server_url):
        return False
    try:
        resource_path = canonical_resource_uri(resource).split("://", 1)[1]
        server_path = canonical_resource_uri(server_url).split("://", 1)[1]
    except (ValueError, IndexError):
        return False
    return server_path == resource_path or server_path.startswith(f"{resource_path}/")


async def discover_authorization_server(
    client: httpx.AsyncClient, issuer: str
) -> AuthorizationServerMetadata:
    """The RFC 8414 / OpenID document for one issuer.

    Walks the candidate ladder, advancing on any non-2xx, non-JSON body, parse
    failure, or missing required field. An issuer mismatch aborts instead of
    advancing. A document that does not advertise ``S256`` is refused, whether
    ``code_challenge_methods_supported`` is absent or merely lists ``plain``:
    OAuth 2.1 makes ``S256`` mandatory-to-implement on the server, so a server
    without it is either non-conformant or deliberately weakened, and ``plain``
    hands the exchange to anyone who can read the authorization request.

    Raises :class:`~jhin_oauth.errors.DiscoveryError`,
    :class:`~jhin_oauth.errors.IssuerMismatchError`, and
    :class:`~jhin_oauth.errors.PkceUnsupportedError`.
    """
    for candidate in well_known_as_candidates(issuer):
        document = await _fetch_metadata(client, candidate)
        if document is None:
            continue
        try:
            metadata = parse_authorization_server_metadata(document, expected_issuer=issuer)
        except IssuerMismatchError:
            raise
        except DiscoveryError:
            logger.debug("oauth.authorization_server_document_rejected")
            continue
        if "S256" not in metadata.code_challenge_methods_supported:
            raise PkceUnsupportedError(
                "this authorization server does not support the PKCE method Jhin requires"
            )
        return metadata
    raise DiscoveryError("this authorization server publishes no usable metadata")


def select_scopes(
    *,
    challenge_scope: str | None,
    resource_scopes: Sequence[str],
    server_scopes: Sequence[str],
    want_offline_access: bool,
) -> str:
    """The narrowest scope string that can satisfy this operation.

    Priority, per the MCP authorization spec: the ``scope`` the resource named
    in its own 401 challenge wins, because it is the server saying exactly what
    this call needs; then the protected resource's ``scopes_supported``; then
    nothing at all, which is a valid request and lets the server apply its
    default. ``offline_access`` is appended only when a refresh token is wanted
    *and* the authorization server advertises it — asking for a scope a server
    does not know is how a whole authorization gets rejected.

    Wildcards are dropped wherever they appear.
    """
    if challenge_scope and challenge_scope.strip():
        requested: Iterable[str] = challenge_scope.split()
    else:
        requested = resource_scopes

    selected: list[str] = []
    for scope in requested:
        if not _is_usable_scope(scope) or scope in selected:
            continue
        selected.append(scope)
        if len(selected) >= MAX_SELECTED_SCOPES:
            break

    if (
        want_offline_access
        and OFFLINE_ACCESS_SCOPE in server_scopes
        and OFFLINE_ACCESS_SCOPE not in selected
        and len(selected) < MAX_SELECTED_SCOPES
    ):
        selected.append(OFFLINE_ACCESS_SCOPE)

    rendered = ""
    for scope in selected:
        candidate = f"{rendered} {scope}" if rendered else scope
        if len(candidate) > MAX_SCOPE_STRING_LENGTH:
            break
        rendered = candidate
    return rendered


def _is_usable_scope(scope: object) -> bool:
    if not isinstance(scope, str) or not scope or len(scope) > MAX_STRING_ENTRY_LENGTH:
        return False
    if scope in FORBIDDEN_SCOPES:
        return False
    return all(character in _SCOPE_TOKEN_ALLOWED for character in scope)


def _origin_of(url: str) -> str:
    scheme, _, remainder = url.partition("://")
    return f"{scheme}://{remainder.split('/', 1)[0]}"


def _probe(
    server_url: str,
    *,
    requires_auth: bool = False,
    resource_metadata_url: str | None = None,
    challenge_scope: str | None = None,
    protected_resource: ProtectedResourceMetadata | None = None,
    authorization_server: AuthorizationServerMetadata | None = None,
    failure_reason: str | None = None,
) -> McpAuthProbe:
    return McpAuthProbe(
        server_url=server_url,
        requires_auth=requires_auth,
        resource_metadata_url=resource_metadata_url,
        challenge_scope=challenge_scope,
        protected_resource=protected_resource,
        authorization_server=authorization_server,
        supports_oauth=authorization_server is not None,
        supports_dcr=authorization_server is not None and authorization_server.supports_dcr(),
        failure_reason=failure_reason,
    )


async def probe_mcp_endpoint(client: httpx.AsyncClient, server_url: str) -> McpAuthProbe:
    """What one unauthenticated ``initialize`` and the discovery chain learned.

    This runs against a URL an admin typed, which means it runs against
    whatever that URL turns out to be. It never raises for a hostile server:
    every failure lands in :attr:`McpAuthProbe.failure_reason` as one of
    :data:`PROBE_FAILURE_REASONS`, so the answer to "can we OAuth this?" is
    always a value the UI can render and never provider prose.

    Raises :class:`jhin_domain.endpoints.EndpointPolicyError` only, and
    only when ``server_url`` itself is not an allowed target.
    """
    endpoint = validate_oauth_url(server_url, kind="MCP server URL")

    try:
        response = await send_bounded(
            client,
            client.build_request(
                "POST",
                endpoint,
                json=_INITIALIZE_BODY,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                },
                timeout=httpx.Timeout(30.0),
            ),
            max_response_bytes=MAX_METADATA_BYTES,
        )
    except BoundedHttpError:
        return _probe(endpoint, failure_reason="unreachable")

    if response.is_success:
        # Nothing to authorize: this server answers unauthenticated calls.
        return _probe(endpoint, failure_reason="no_authentication_required")

    challenge = parse_www_authenticate(response.headers.get("www-authenticate", ""))
    requires_auth = response.status_code in {401, 403}
    resource_metadata_url = challenge.get("resource_metadata") or None
    challenge_scope = challenge.get("scope") or None

    protected_resource: ProtectedResourceMetadata | None = None
    try:
        protected_resource = await discover_protected_resource(
            client, endpoint, resource_metadata_url=resource_metadata_url
        )
        issuers = protected_resource.authorization_servers
    except (DiscoveryError, EndpointPolicyError):
        # No RFC 9728 document. Several live servers are like this and still
        # run their own authorization server at the origin root, so the origin
        # is tried as an issuer of last resort before giving up. The audience
        # then comes from the endpoint itself rather than from a document.
        issuers = (_origin_of(endpoint),)

    failure_reason = (
        "no_authorization_server" if protected_resource else "no_protected_resource_metadata"
    )
    for issuer in issuers:
        try:
            metadata = await discover_authorization_server(client, issuer)
        except IssuerMismatchError:
            # An active mix-up signal aborts the whole chain: the next issuer
            # in a document this one already lied in is not worth trying.
            return _probe(
                endpoint,
                requires_auth=requires_auth,
                resource_metadata_url=resource_metadata_url,
                challenge_scope=challenge_scope,
                protected_resource=protected_resource,
                failure_reason="issuer_mismatch",
            )
        except PkceUnsupportedError:
            failure_reason = "pkce_unsupported"
            continue
        except (DiscoveryError, EndpointPolicyError):
            continue
        return _probe(
            endpoint,
            requires_auth=requires_auth,
            resource_metadata_url=resource_metadata_url,
            challenge_scope=challenge_scope,
            protected_resource=protected_resource,
            authorization_server=metadata,
        )

    return _probe(
        endpoint,
        requires_auth=requires_auth,
        resource_metadata_url=resource_metadata_url,
        challenge_scope=challenge_scope,
        protected_resource=protected_resource,
        failure_reason=failure_reason,
    )


__all__ = [
    "FORBIDDEN_SCOPES",
    "MAX_AUTHORIZATION_SERVERS",
    "MAX_METADATA_BYTES",
    "MAX_SCOPE_ENTRIES",
    "MCP_PROTOCOL_VERSION",
    "OFFLINE_ACCESS_SCOPE",
    "PROBE_FAILURE_REASONS",
    "discover_authorization_server",
    "discover_protected_resource",
    "parse_authorization_server_metadata",
    "parse_protected_resource_metadata",
    "probe_mcp_endpoint",
    "select_scopes",
]
