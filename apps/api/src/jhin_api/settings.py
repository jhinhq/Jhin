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

    # --- OAuth (docs/architecture/oauth.md) ---
    # Origin the provider redirects the browser back to. Empty means "use
    # APP_URL", which is right for every deployment where the web app and the
    # API share an origin through the Next.js proxy. Set it only when the
    # browser reaches Jhin at a different origin than APP_URL.
    oauth_redirect_base_url: str = ""
    # How long a pending authorization stays claimable. Thirty minutes, not
    # ten: a single round trip can contain an SSO login at the edge (a Jhin
    # behind Cloudflare Access adds one, and it can include an emailed
    # one-time code), a provider sign-in with a second factor, a consent
    # screen somebody actually reads, and — for a GitHub App — an
    # installation picker where they choose an org and repositories. Ten
    # minutes is a tight budget for that sequence, not a generous one.
    #
    # This is the fourth control in front of the callback, never the first:
    # the handle is 256 bits, only sha256(handle) is stored, the row is
    # single-use, and it is bound to the initiating user's session. Widening
    # this widens the window only for somebody who already holds both the
    # handle and the victim's live session — and holding both means the flow
    # is already lost. It is also not a new maximum:
    # DEVICE_FLOW_MAX_TTL_SECONDS is already 1800, and the manifest flow's
    # 3600 stays the outlier, dictated by GitHub's conversion-code lifetime.
    #
    # It buys diagnosability too. GitHub's authorization *code* is valid ten
    # minutes. Under a ten-minute state a twelve-minute round trip dies at
    # the claim as an indistinguishable state_expired/state_unknown; under
    # thirty it reaches the exchange and fails as invalid_grant, which
    # oauth.code_exchange_failed names outright. The longer window does not
    # only let slow round trips succeed — it makes the ones that still fail
    # legible.
    oauth_state_ttl_seconds: int = 1800
    # How long a *consumed* authorization remembers what it produced, so a
    # refresh, a back-button, or a link prefetch that spent the single-use
    # state does not cost somebody the connection they actually made. A
    # receipt holds no secret, is readable only by the session that could
    # have completed the flow, and produces only a redirect to a page that
    # session already reaches. Clamped to an hour in code; 0 disables
    # receipts entirely and every repeat gets the uniform refusal.
    oauth_callback_receipt_ttl_seconds: int = 600
    # client_name sent during dynamic client registration; what the user sees
    # on the provider's consent screen.
    oauth_client_name: str = "Jhin"
    # Proactive refresh sweep cadence, in seconds.
    oauth_refresh_interval_seconds: int = 300
    # Offer the sign-in code before the browser sign-in for a native provider
    # that can do both. Never removes either flow and never affects MCP
    # servers: it is for an operator whose browsers cannot load this
    # instance's redirect URI.
    oauth_prefer_device_code: bool = False

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

    @model_validator(mode="after")
    def _validate_oauth_redirect(self) -> Settings:
        """Refuse at startup to compute a redirect URI nobody can use safely.

        The redirect URI is one constant for the whole instance, recomputed
        from these settings at every call site and registered with every
        provider. A base URL carrying a path, a query, a fragment, or
        userinfo produces a URI that either will not match what was
        registered or hands part of the destination to whoever can influence
        it — so the shape is checked once, here, rather than discovered when
        a provider rejects the tenth authorization of the day.

        The TLS rule is the same posture ``COOKIE_SECURE`` already takes: an
        authorization code arriving over plaintext on a public origin is an
        account takeover, so a production-like install must terminate TLS or
        stay on loopback.
        """
        base = self.oauth_redirect_base_url.strip()
        source = "OAUTH_REDIRECT_BASE_URL"
        if not base:
            base, source = self.app_url.strip(), "APP_URL"
        if not base:
            raise InsecureDeploymentError(
                "APP_URL is empty, so Jhin cannot compute the OAuth redirect URI that "
                "providers must be told to send people back to. Set APP_URL."
            )
        parsed = urlsplit(base)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"} or not host:
            raise InsecureDeploymentError(
                f"{source} ({base!r}) must be an absolute http:// or https:// URL with a host."
            )
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise InsecureDeploymentError(
                f"{source} ({base!r}) must be a bare origin: no query string, no fragment, "
                "and no username or password."
            )
        if parsed.path not in {"", "/"}:
            raise InsecureDeploymentError(
                f"{source} ({base!r}) must be a bare origin with no path. Jhin appends its "
                "own callback path, and a provider matches the registered redirect URI "
                "exactly."
            )
        if (
            self.is_production_like
            and scheme != "https"
            and host not in _LOOPBACK_HOSTS
            and not host.endswith(".localhost")
        ):
            raise InsecureDeploymentError(
                f"{source} ({base!r}) is plaintext HTTP on a public host. An OAuth "
                "authorization code sent back over plaintext can be stolen in transit; "
                "serve Jhin over HTTPS or set APP_ENV=dev for a local install."
            )
        return self

    @model_validator(mode="after")
    def _validate_oauth_state_ttl(self) -> Settings:
        """Refuse a state or receipt lifetime that cannot mean what it says.

        The state TTL is a defence-in-depth bound, not the control that stops
        a forged callback — the 256-bit handle, its ``sha256``-only storage,
        the single-use claim, and the session binding are. So the range is
        wide, and both ends are real: below a minute no round trip through a
        provider's consent screen can finish, and above an hour a pending
        authorization outlives the GitHub App conversion code, which is the
        longest-lived credential this subsystem holds.
        """
        if not 60 <= self.oauth_state_ttl_seconds <= 3600:
            raise InsecureDeploymentError(
                f"OAUTH_STATE_TTL_SECONDS is {self.oauth_state_ttl_seconds}. It must be "
                "between 60 and 3600 seconds: below a minute no real round trip through a "
                "provider's consent screen can finish, and above an hour a pending "
                "authorization outlives the GitHub App conversion code, which is the "
                "longest-lived credential this subsystem holds."
            )
        if not 0 <= self.oauth_callback_receipt_ttl_seconds <= 3600:
            raise InsecureDeploymentError(
                f"OAUTH_CALLBACK_RECEIPT_TTL_SECONDS is "
                f"{self.oauth_callback_receipt_ttl_seconds}. It must be between 0 "
                "(receipts disabled) and 3600 seconds."
            )
        return self


def _split_origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
