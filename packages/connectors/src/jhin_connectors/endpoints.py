"""Fail-closed outbound target policy shared by connector clients.

HTTP providers are selected by exact normalized origin. PostgreSQL targets
are selected by a credential-free host/port/database tuple, while the
original validated DSN is returned unchanged for the database driver.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
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
    return host


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


def _host_is_local_or_private(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return host == "localhost" or host.endswith(".localhost") or host.endswith(".local")


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


__all__ = ["EndpointPolicyError", "validate_http_origin", "validate_postgres_target"]
