"""Fail-closed outbound target policy for everything Jhin dials out to.

HTTP providers are selected by exact normalized origin. PostgreSQL targets
are selected by a credential-free host/port/database tuple, while the
original validated DSN is returned unchanged for the database driver.

The policy lives in the dependency-light domain package, not beside the
connectors that were its first callers, because the OAuth core needs the very
same validator for the attacker-influenced URLs an authorization server hands
it. Reaching it through ``jhin_connectors`` would put the executable connector
catalog on the agent worker, which depends on the OAuth core for background
token refresh and must structurally be unable to execute a connector. Only
the stdlib may be imported here.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# Labels that the resolver will happily reinterpret as an IPv4 literal even
# though ``ipaddress`` refuses them: bare decimals (``2130706433``), short form
# (``127.1``), octal (``0300.0250.0.1``) and hex (``0x7f000001``) all resolve to
# loopback or RFC1918 space via ``getaddrinfo``/``inet_aton``. Treating them as
# ordinary hostnames is how a private-range block gets walked straight past.
_ALL_DIGITS_RE = re.compile(r"^[0-9]+$")
_HEX_LITERAL_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
_PROJECT_REF_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_HOSTED_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
_DEFAULT_POSTGRES_PORT = 5432


class EndpointPolicyError(ValueError):
    """An outbound connector target violates the configured endpoint policy."""


def _require_unambiguous_url(value: str, *, kind: str) -> None:
    if not value or value != value.strip() or "\\" in value:
        raise EndpointPolicyError(f"{kind} is invalid")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
        raise EndpointPolicyError(f"{kind} is invalid")


def _normalize_host(raw_host: str) -> str:
    if not raw_host or raw_host.endswith("."):
        raise EndpointPolicyError("Endpoint host is invalid")
    try:
        return ipaddress.ip_address(raw_host).compressed.lower()
    except ValueError:
        pass
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise EndpointPolicyError("Endpoint host is invalid") from None
    labels = host.split(".")
    if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise EndpointPolicyError("Endpoint host is invalid")
    if _is_ambiguous_ip_literal(labels):
        raise EndpointPolicyError("Endpoint host is invalid")
    return host


def _is_ambiguous_ip_literal(labels: list[str]) -> bool:
    """True for names the resolver would parse as a packed IPv4 address.

    ``ipaddress.ip_address`` rejects these forms, so without this check they
    fall through to the hostname branch, pass the label regex, and are
    classified as public — while ``getaddrinfo`` still resolves them to
    loopback or private space. A real hostname's last label is never numeric
    (RFC 1123 forbids an all-numeric TLD), so rejecting them costs nothing.
    """
    if any(_HEX_LITERAL_RE.fullmatch(label) for label in labels):
        return True
    return bool(labels) and bool(_ALL_DIGITS_RE.fullmatch(labels[-1]))


def _render_origin(scheme: str, host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def _normalize_origin(raw: str) -> tuple[str, str, int, str]:
    _require_unambiguous_url(raw, kind="HTTP origin")
    if "?" in raw or "#" in raw:
        raise EndpointPolicyError("HTTP origin must not contain a query or fragment")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host_value = parsed.hostname
        port_value = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        raise EndpointPolicyError("HTTP origin is invalid") from None
    if scheme not in {"http", "https"} or host_value is None:
        raise EndpointPolicyError("HTTP origin is invalid")
    if username is not None or password is not None:
        raise EndpointPolicyError("HTTP origin must not contain credentials")
    if parsed.path not in {"", "/"}:
        raise EndpointPolicyError("HTTP origin must not contain a path")

    host = _normalize_host(host_value)
    port = port_value if port_value is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65_535:
        raise EndpointPolicyError("HTTP origin port is invalid")
    return scheme, host, port, _render_origin(scheme, host, port)


def _address_is_reachable_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is somewhere a connector may legitimately talk to.

    ``is_global`` alone is not enough: it says nothing about multicast,
    reserved, or unspecified space, and IPv6 has three ways to smuggle an IPv4
    address (``::ffff:``, 6to4, Teredo) that must be judged on the address they
    actually carry.
    """
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour or address.teredo
        if isinstance(embedded, tuple):  # Teredo yields (server, client).
            embedded = embedded[1]
        if embedded is not None:
            return _address_is_reachable_public(embedded)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def _host_is_local_or_private(host: str) -> bool:
    try:
        return not _address_is_reachable_public(ipaddress.ip_address(host))
    except ValueError:
        return host == "localhost" or host.endswith(".localhost") or host.endswith(".local")


# DNS checking can be turned off for air-gapped installs whose internal names
# never resolve; the lexical policy above still applies.
def _dns_checks_enabled() -> bool:
    return os.getenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def _resolves_to_private_address(host: str) -> bool:
    """True when DNS maps ``host`` onto space a connector must not reach.

    Closes the hole where an attacker registers a perfectly public *name*
    (``metadata.attacker.example``) whose A record points at ``169.254.169.254``
    or an RFC1918 host. Resolution failures deliberately do **not** block: a
    name that cannot be resolved here cannot be connected to either, and
    failing closed would break air-gapped installs whose resolver is offline
    at validation time.

    NOTE: this is a check at validation time, not a pinned connection. A
    hostile resolver can still answer differently for the connection that
    follows (DNS rebinding); pinning the resolved address into the transport
    is tracked as residual risk in docs/security-assessment.md.
    """
    if not _dns_checks_enabled():
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not _address_is_reachable_public(address):
            return True
    return False


def _official_origin_set(official_origins: tuple[str, ...]) -> set[str]:
    normalized: set[str] = set()
    for official in official_origins:
        try:
            scheme, host, _port, rendered = _normalize_origin(official)
        except EndpointPolicyError:
            raise EndpointPolicyError("Official HTTP origin configuration is invalid") from None
        if scheme != "https" or _host_is_local_or_private(host):
            raise EndpointPolicyError("Official HTTP origin configuration is invalid")
        normalized.add(rendered)
    return normalized


def _operator_origin_set(allowlist_env: str) -> set[str]:
    normalized: set[str] = set()
    for entry in os.getenv(allowlist_env, "").split(","):
        if not entry.strip():
            continue
        try:
            normalized.add(_normalize_origin(entry.strip())[3])
        except EndpointPolicyError:
            raise EndpointPolicyError("HTTP origin allowlist configuration is invalid") from None
    return normalized


def validate_http_origin(
    raw: str,
    *,
    official_origins: tuple[str, ...],
    allowlist_env: str = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
) -> str:
    """Return a normalized origin only when policy allows its exact origin."""
    _scheme, _host, _port, normalized = _normalize_origin(raw)
    if normalized in _official_origin_set(official_origins):
        return normalized
    if normalized in _operator_origin_set(allowlist_env):
        return normalized
    raise EndpointPolicyError("HTTP origin is not allowed")


def validate_public_http_url(
    raw: str,
    *,
    kind: str = "HTTP URL",
    allowlist_env: str = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
) -> str:
    """Return a normalized absolute URL when policy allows its origin.

    Public ``https`` origins are accepted as-is. Any other origin (plain
    ``http``, localhost, private/link-local hosts, IP literals that are not
    global) needs an exact operator allow-list entry. Userinfo and fragments
    are rejected; the path and query are preserved (unlike
    :func:`validate_http_origin`, which forbids paths). Shared by the MCP
    and generic HTTP connectors.
    """
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise EndpointPolicyError(f"{kind} is invalid")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise EndpointPolicyError(f"{kind} is invalid") from None
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError(f"{kind} must not contain credentials or a fragment")
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise EndpointPolicyError(f"{kind} is invalid")
    host_for_origin = parsed.hostname
    if ":" in host_for_origin:
        host_for_origin = f"[{host_for_origin}]"
    try:
        port = parsed.port
    except ValueError:
        raise EndpointPolicyError(f"{kind} is invalid") from None
    origin_candidate = f"{parsed.scheme}://{host_for_origin}" + (f":{port}" if port else "")
    try:
        scheme, host, _port, origin = _normalize_origin(origin_candidate)
    except EndpointPolicyError:
        raise EndpointPolicyError(f"{kind} is invalid") from None
    if origin not in _operator_origin_set(allowlist_env):
        # Not operator-authorized: it must be a public https origin, and the
        # name must not resolve into private space.
        if scheme != "https" or _host_is_local_or_private(host):
            raise EndpointPolicyError(f"{kind} is not allowed")
        is_ip_literal = True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            is_ip_literal = False
        if not is_ip_literal and _resolves_to_private_address(host):
            raise EndpointPolicyError(f"{kind} is not allowed")
    path = parsed.path or "/"
    return urlunsplit((scheme, origin.split("://", 1)[1], path, parsed.query, ""))


@dataclass(frozen=True)
class _PostgresTarget:
    host: str
    port: int
    database: str
    username: str
    sslmode: str | None

    @property
    def identity(self) -> tuple[str, int, str]:
        return self.host, self.port, self.database


def _parse_postgres_url(dsn: str) -> _PostgresTarget:
    _require_unambiguous_url(dsn, kind="PostgreSQL target")
    if "#" in dsn:
        raise EndpointPolicyError("PostgreSQL target is invalid")
    try:
        parsed = urlsplit(dsn)
        scheme = parsed.scheme.lower()
        host_value = parsed.hostname
        port_value = parsed.port
        username_value = parsed.username
        password_value = parsed.password
    except ValueError:
        raise EndpointPolicyError("PostgreSQL target is invalid") from None
    if scheme not in {"postgresql", "postgresql+asyncpg"} or host_value is None:
        raise EndpointPolicyError("PostgreSQL target is invalid")
    if username_value is None or password_value is None or not password_value:
        raise EndpointPolicyError("PostgreSQL target is invalid")

    database = unquote(parsed.path.removeprefix("/"))
    if not database or "/" in database:
        raise EndpointPolicyError("PostgreSQL target is invalid")
    host = _normalize_host(host_value)
    port = port_value if port_value is not None else _DEFAULT_POSTGRES_PORT
    if not 1 <= port <= 65_535:
        raise EndpointPolicyError("PostgreSQL target is invalid")

    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise EndpointPolicyError("PostgreSQL target is invalid") from None
    normalized_keys = [key.casefold() for key, _value in query_pairs]
    if len(query_pairs) > 1 or any(key != "sslmode" for key in normalized_keys):
        raise EndpointPolicyError("PostgreSQL target query is not allowed")
    sslmode_values = [
        value.casefold()
        for (key, value), normalized_key in zip(query_pairs, normalized_keys, strict=True)
        if normalized_key == "sslmode"
    ]
    sslmode = sslmode_values[0] if sslmode_values else None
    return _PostgresTarget(
        host=host,
        port=port,
        database=database,
        username=unquote(username_value),
        sslmode=sslmode,
    )


def _database_allowlist(allowlist_env: str) -> set[tuple[str, int]]:
    allowed: set[tuple[str, int]] = set()
    for entry in os.getenv(allowlist_env, "").split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            parsed = urlsplit(f"//{candidate}")
            if (
                parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            host = _normalize_host(parsed.hostname)
            port = parsed.port if parsed.port is not None else _DEFAULT_POSTGRES_PORT
            if not 1 <= port <= 65_535:
                raise ValueError
        except (EndpointPolicyError, ValueError):
            raise EndpointPolicyError("Database host allowlist configuration is invalid") from None
        allowed.add((host, port))
    return allowed


def _is_official_direct_target(target: _PostgresTarget, project_ref: str) -> bool:
    return target.host == f"db.{project_ref}.supabase.co" and target.port == _DEFAULT_POSTGRES_PORT


def _is_official_pooler_target(target: _PostgresTarget, project_ref: str) -> bool:
    suffix = ".pooler.supabase.com"
    return (
        target.host.endswith(suffix)
        and target.host != suffix.removeprefix(".")
        and target.port == _DEFAULT_POSTGRES_PORT
        and target.username.endswith(f".{project_ref}")
    )


def _uses_official_supabase_namespace(target: _PostgresTarget) -> bool:
    return (
        target.host.startswith("db.") and target.host.endswith(".supabase.co")
    ) or target.host.endswith(".pooler.supabase.com")


def validate_postgres_target(
    dsn: str,
    *,
    project_ref: str,
    app_database_url: str | None,
    allowlist_env: str = "JHIN_CONNECTOR_ALLOWED_DB_HOSTS",
) -> str:
    """Validate a PostgreSQL target and return its original DSN unchanged.

    Credentials are parsed only to require an explicit password and validate
    the pooler username suffix. No exception includes the DSN, username,
    password, host, or database name.
    """
    normalized_project_ref = project_ref.casefold()
    if not _PROJECT_REF_RE.fullmatch(normalized_project_ref):
        raise EndpointPolicyError("Supabase project reference is invalid")

    try:
        target = _parse_postgres_url(dsn)
        app_target = _parse_postgres_url(app_database_url) if app_database_url else None
    except EndpointPolicyError:
        raise EndpointPolicyError("PostgreSQL target is invalid") from None

    if app_target is not None and target.identity == app_target.identity:
        raise EndpointPolicyError("PostgreSQL target cannot be Jhin's application database")

    hosted = _is_official_direct_target(
        target, normalized_project_ref
    ) or _is_official_pooler_target(target, normalized_project_ref)
    if _uses_official_supabase_namespace(target):
        if not hosted:
            raise EndpointPolicyError("PostgreSQL target is not allowed")
        if target.sslmode not in _HOSTED_TLS_MODES:
            raise EndpointPolicyError("Hosted PostgreSQL targets require TLS")
        return dsn

    if (target.host, target.port) in _database_allowlist(allowlist_env):
        return dsn
    raise EndpointPolicyError("PostgreSQL target is not allowed")


__all__ = [
    "EndpointPolicyError",
    "validate_http_origin",
    "validate_postgres_target",
    "validate_public_http_url",
]
