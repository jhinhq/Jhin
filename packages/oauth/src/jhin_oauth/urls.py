"""URL policy for OAuth: SSRF validation, canonical audiences, well-known
candidates, and ``WWW-Authenticate`` parsing.

Every URL an authorization server or a protected resource hands Jhin is
attacker-influenced, including the ones Jhin then *fetches*. They all go
through :func:`validate_oauth_url`, which is a thin wrapper over the shared
policy in :mod:`jhin_domain.endpoints` — the same validator the MCP and
generic HTTP connectors use. This module only ever tightens that policy; it
never relaxes one of its rules.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from jhin_domain.endpoints import (
    EndpointPolicyError,
    validate_http_origin,
    validate_public_http_url,
)

MAX_URL_LENGTH = 2_000
MAX_CHALLENGE_HEADER_LENGTH = 8_192
MAX_CHALLENGE_PARAMS = 16
MAX_CHALLENGE_NAME_LENGTH = 64
MAX_CHALLENGE_VALUE_LENGTH = 2_048

WELL_KNOWN_PROTECTED_RESOURCE = ".well-known/oauth-protected-resource"
WELL_KNOWN_AUTHORIZATION_SERVER = ".well-known/oauth-authorization-server"
WELL_KNOWN_OPENID_CONFIGURATION = ".well-known/openid-configuration"

# RFC 9110 token characters, which is what an auth-param name may contain.
_AUTH_PARAM_RE = re.compile(
    r"""(?P<name>[A-Za-z0-9!#$%&'*+\-.^_`|~]+)\s*=\s*"""
    r"""(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<token>[^\s,]*))"""
)
_BEARER_RE = re.compile(r"(?:^|[\s,])bearer(?=[\s,]|$)", re.IGNORECASE)
_QUOTED_PAIR_RE = re.compile(r"\\(.)")


def validate_oauth_url(raw: str, *, kind: str) -> str:
    """The normalized URL when Jhin's outbound policy allows it.

    Delegates to :func:`jhin_domain.endpoints.validate_public_http_url`
    (public ``https`` origins, or an exact operator allow-list entry in
    ``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`` for anything else) and then
    re-asserts the two rules OAuth depends on: no userinfo and no fragment,
    and ``https`` unless the operator has explicitly allow-listed that exact
    origin. The re-assertion is deliberate duplication — this module's
    guarantee must hold even if the shared validator's own scheme rule is
    ever loosened for another connector's sake.

    Only the *authority* is normalized. The shared validator rewrites an empty
    path to ``"/"``, which is harmless for a connector call and fatal here: an
    issuer is compared byte for byte against the metadata document that claims
    it, and RFC 8414 issuers routinely have no path at all. Inventing a
    trailing slash would turn every origin-root authorization server into an
    issuer mismatch.

    Raises :class:`jhin_domain.endpoints.EndpointPolicyError`.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_LENGTH:
        raise EndpointPolicyError(f"{kind} is invalid")
    normalized = validate_public_http_url(raw, kind=kind)
    parsed = urlsplit(normalized)
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError(f"{kind} must not contain credentials or a fragment")
    if parsed.scheme != "https":
        # Reachable only for an origin the operator allow-listed; asking the
        # shared validator again is how that is proven rather than assumed.
        validate_http_origin(f"{parsed.scheme}://{parsed.netloc}", official_origins=())
    original = urlsplit(raw)
    return urlunsplit((parsed.scheme, parsed.netloc, original.path, original.query, ""))


def canonical_resource_uri(server_url: str) -> str:
    """The RFC 8707 §2 canonical form of a resource identifier.

    Lowercases the scheme and host, elides a default port, drops a bare ``/``
    path entirely and any other trailing slash, and keeps the query. This is
    the value that becomes the ``resource`` parameter and, once stored, the
    audience an access token may be presented to.

    Raises :class:`ValueError` for a relative URL or one carrying a fragment.
    """
    if not isinstance(server_url, str) or not server_url.strip():
        raise ValueError("a resource identifier must be an absolute URL")
    parsed = urlsplit(server_url.strip())
    if parsed.fragment:
        raise ValueError("a resource identifier must not contain a fragment")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("a resource identifier must be an absolute http or https URL")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("a resource identifier carries an invalid port") from None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    path = "" if parsed.path in {"", "/"} else parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _rebuild(parsed_scheme: str, netloc: str, path: str, *, kind: str) -> str:
    return validate_oauth_url(urlunsplit((parsed_scheme, netloc, path, "", "")), kind=kind)


def well_known_prm_candidates(server_url: str) -> tuple[str, ...]:
    """RFC 9728 §3 lookup order for one MCP endpoint.

    Path-insertion first — ``https://host/.well-known/oauth-protected-resource/mcp``
    for ``https://host/mcp`` — then the origin root. The path-inserted form is
    omitted when the endpoint has no path, because it would be identical to
    the root form.
    """
    normalized = validate_oauth_url(server_url, kind="MCP server URL")
    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/")
    candidates: list[str] = []
    if path:
        candidates.append(
            _rebuild(
                parsed.scheme,
                parsed.netloc,
                f"/{WELL_KNOWN_PROTECTED_RESOURCE}{path}",
                kind="protected resource metadata URL",
            )
        )
    candidates.append(
        _rebuild(
            parsed.scheme,
            parsed.netloc,
            f"/{WELL_KNOWN_PROTECTED_RESOURCE}",
            kind="protected resource metadata URL",
        )
    )
    return tuple(candidates)


def well_known_as_candidates(issuer: str) -> tuple[str, ...]:
    """RFC 8414 §3.1 and OpenID Discovery lookup order for one issuer.

    An issuer *with* a path yields the three candidates MCP mandates, in
    order: OAuth path-insertion, OpenID path-insertion, then OpenID
    path-appending. An issuer without a path yields two. An issuer carrying a
    query or a fragment is refused outright — RFC 8414 §2 forbids both, and a
    candidate built from one would silently drop the part that made it
    distinct.
    """
    normalized = validate_oauth_url(issuer, kind="authorization server issuer")
    parsed = urlsplit(normalized)
    if parsed.query:
        raise EndpointPolicyError("authorization server issuer must not contain a query")
    path = parsed.path.rstrip("/")
    paths: tuple[str, ...]
    if path:
        paths = (
            f"/{WELL_KNOWN_AUTHORIZATION_SERVER}{path}",
            f"/{WELL_KNOWN_OPENID_CONFIGURATION}{path}",
            f"{path}/{WELL_KNOWN_OPENID_CONFIGURATION}",
        )
    else:
        paths = (
            f"/{WELL_KNOWN_AUTHORIZATION_SERVER}",
            f"/{WELL_KNOWN_OPENID_CONFIGURATION}",
        )
    return tuple(
        _rebuild(
            parsed.scheme,
            parsed.netloc,
            candidate_path,
            kind="authorization server metadata URL",
        )
        for candidate_path in paths
    )


def same_origin(left: str, right: str) -> bool:
    """Whether two absolute URLs share a scheme, host, and effective port."""
    try:
        first = urlsplit(canonical_resource_uri(left))
        second = urlsplit(canonical_resource_uri(right))
    except ValueError:
        return False
    return (first.scheme, first.netloc) == (second.scheme, second.netloc)


def parse_www_authenticate(header: str) -> dict[str, str]:
    """The Bearer challenge's auth-params, lowercased and bounded.

    Quoted and unquoted values are both accepted: RFC 9110 permits either and
    at least one large provider emits unquoted ones. A header that is
    malformed, oversized, or carries no Bearer challenge yields an empty
    mapping — this function never raises, because it runs on whatever an
    unauthenticated probe happened to receive.
    """
    if not isinstance(header, str) or not header or len(header) > MAX_CHALLENGE_HEADER_LENGTH:
        return {}
    match = _BEARER_RE.search(header)
    if match is None:
        return {}
    params: dict[str, str] = {}
    for param in _AUTH_PARAM_RE.finditer(header, match.end()):
        if len(params) >= MAX_CHALLENGE_PARAMS:
            break
        name = param.group("name").lower()
        quoted = param.group("quoted")
        value = _QUOTED_PAIR_RE.sub(r"\1", quoted) if quoted is not None else param.group("token")
        if len(name) > MAX_CHALLENGE_NAME_LENGTH or len(value) > MAX_CHALLENGE_VALUE_LENGTH:
            continue
        params.setdefault(name, value)
    return params


__all__ = [
    "MAX_CHALLENGE_HEADER_LENGTH",
    "MAX_CHALLENGE_NAME_LENGTH",
    "MAX_CHALLENGE_PARAMS",
    "MAX_CHALLENGE_VALUE_LENGTH",
    "MAX_URL_LENGTH",
    "WELL_KNOWN_AUTHORIZATION_SERVER",
    "WELL_KNOWN_OPENID_CONFIGURATION",
    "WELL_KNOWN_PROTECTED_RESOURCE",
    "canonical_resource_uri",
    "parse_www_authenticate",
    "same_origin",
    "validate_oauth_url",
    "well_known_as_candidates",
    "well_known_prm_candidates",
]
