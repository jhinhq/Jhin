"""API configuration loaded from environment variables (plan section 39)."""

from __future__ import annotations

from functools import cached_property, lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from jhin_observability import ObservabilitySettings

# Hosts for which plaintext HTTP is a legitimate production configuration:
# the single-machine quick start, reached only over the loopback interface.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


class InsecureDeploymentError(RuntimeError):
    """The process was asked to serve a production deployment insecurely.

    Raised at startup rather than degrading silently: a self-hosted Jhin holds
    the company's credentials, and a cookie served without ``Secure`` over a
    public origin is a session-theft waiting to happen.
    """


class Settings(ObservabilitySettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "Jhin"
    app_url: str = "http://localhost:3000"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://jhin:jhin@localhost:5432/jhin"
    nats_url: str = "nats://localhost:4222"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

    # --- Auth (plan 20.1) ---
    session_cookie_name: str = "jhin_session"
    csrf_cookie_name: str = "jhin_csrf"
    csrf_header_name: str = "x-csrf-token"
    # Absolute session lifetime: a session dies at this age no matter how
    # actively it is used, so a stolen cookie has a bounded blast radius.
    session_ttl_hours: int = 24 * 7
    # Idle lifetime: an unused session is revoked after this long. Shorter than
    # the absolute lifetime so abandoned browser tabs stop being credentials.
    session_idle_timeout_hours: int = 72
    # Revoke a session whose User-Agent changes. Catches cookie replay from a
    # different client without breaking mobile roaming (the address is not
    # part of the binding — only the client identity string).
    session_bind_user_agent: bool = True
    # Set COOKIE_SECURE=true behind HTTPS. Defaults to false so the
    # quick-start stack works over plain http://localhost; a production
    # deployment on a non-loopback origin is rejected at startup instead
    # (see ``_validate_transport_security``).
    cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

    # --- Login lockout (plan 20.1, 41) ---
    login_max_attempts: int = 10
    login_ip_max_attempts: int = 30
    # Half-life of the decaying failure score, and the backoff ladder.
    login_window_seconds: int = 300
    login_base_block_seconds: int = 30
    login_account_max_block_seconds: int = 900
    login_ip_max_block_seconds: int = 3600

    # --- Invitations and API keys (docs/architecture/rbac.md, api-keys.md) ---
    # How long a workspace invitation link stays usable. Single-use regardless.
    invitation_ttl_days: int = 7
    # Bad API-key presentations are locked out on the same decaying ladder as
    # logins, keyed by the key's public prefix instead of an email address.
    api_key_max_attempts: int = 20
    api_key_ip_max_attempts: int = 60
    # Usage rows older than this are pruned opportunistically; the usage
    # endpoint is paginated on top of that.
    api_key_usage_retention_days: int = 30

    # --- Request limits ---
    # Global ceiling on any request body, enforced by middleware. Set above the
    # largest legitimate upload (8 MiB media, 5 MiB skill bundles) so per-route
    # limits keep producing their own specific errors; this only catches the
    # endpoints that have no limit of their own.
    max_request_body_bytes: int = 16 * 1024 * 1024

    # --- Proxy awareness ---
    # Comma-separated CIDRs whose X-Forwarded-For header may be believed. The
    # API normally sits behind the Next.js rewrite proxy, so without this every
    # request appears to come from one address and per-IP lockout would either
    # be useless or lock out every user at once.
    trusted_proxy_cidrs: str = ""

    @cached_property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        networks: list[IPv4Network | IPv6Network] = []
        for raw in self.trusted_proxy_cidrs.split(","):
            candidate = raw.strip()
            if not candidate:
                continue
            try:
                networks.append(ip_network(candidate, strict=False))
            except ValueError:
                continue
        return tuple(networks)

    @property
    def is_production_like(self) -> bool:
        return self.app_env in {"staging", "production"}

    @property
    def emit_hsts(self) -> bool:
        return self.is_production_like and self.cookie_secure

    @property
    def expose_api_docs(self) -> bool:
        """Interactive docs map the whole API surface; keep them out of prod."""
        return not self.is_production_like

    @model_validator(mode="after")
    def _validate_transport_security(self) -> Settings:
        if self.session_cookie_samesite.lower() not in {"lax", "strict", "none"}:
            raise ValueError("SESSION_COOKIE_SAMESITE must be lax, strict, or none")
        if self.session_cookie_samesite.lower() == "none" and not self.cookie_secure:
            raise InsecureDeploymentError(
                "SESSION_COOKIE_SAMESITE=none requires COOKIE_SECURE=true; "
                "browsers reject SameSite=None cookies without the Secure flag."
            )
        if self.session_idle_timeout_hours > self.session_ttl_hours:
            # Not a security hole, but it means the idle timeout never fires;
            # surface it rather than let an operator think they configured one.
            raise ValueError("SESSION_IDLE_TIMEOUT_HOURS must not exceed SESSION_TTL_HOURS")
        if not self.is_production_like:
            return self

        scheme, host = _split_origin(self.app_url)
        if scheme == "https":
            if not self.cookie_secure:
                raise InsecureDeploymentError(
                    f"APP_URL is {self.app_url!r} but COOKIE_SECURE is false: session and "
                    "CSRF cookies would be sent without the Secure flag and could be "
                    "stolen over any plaintext request. Set COOKIE_SECURE=true."
                )
            return self
        if scheme == "http" and host not in _LOOPBACK_HOSTS and not host.endswith(".localhost"):
            raise InsecureDeploymentError(
                f"APP_ENV={self.app_env} with a plaintext APP_URL ({self.app_url!r}). "
                "Terminate TLS in front of Jhin and set APP_URL=https://... with "
                "COOKIE_SECURE=true, or set APP_ENV=dev for a local install."
            )
        return self


def _split_origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
