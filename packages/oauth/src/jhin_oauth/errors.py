"""Display-safe OAuth failures.

Every message crossing this boundary is a sentence Jhin wrote. Provider
``error_description`` text is attacker-influenced and never reaches an
exception, a log line at INFO or above, an API response, or a browser; the
provider's machine-readable ``error`` *code* is kept, but only after it has
been matched against :data:`KNOWN_ERROR_CODES` so an unrecognised code
degrades to ``"unknown"`` instead of flowing through as free text.
"""

from __future__ import annotations

# RFC 6749 §5.2, RFC 8628 §3.5, RFC 7591 §3.2.2, plus the five GitHub codes:
# ``device_flow_disabled`` and ``incorrect_device_code`` from its device flow,
# ``incorrect_client_credentials``, ``bad_verification_code`` and
# ``redirect_uri_mismatch`` from its token endpoint (which reports them with
# HTTP 200). Anything outside this set is reported as "unknown".
KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "bad_verification_code",
        "device_flow_disabled",
        "expired_token",
        "incorrect_client_credentials",
        "incorrect_device_code",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "invalid_target",
        "redirect_uri_mismatch",
        "server_error",
        "slow_down",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_response_type",
    }
)

UNKNOWN_ERROR_CODE = "unknown"


class OAuthError(Exception):
    """Base. The message is always a Jhin-authored constant sentence and
    never contains credential material or provider-supplied text."""


class DiscoveryError(OAuthError):
    """A metadata document could not be fetched, parsed, or trusted."""


class IssuerMismatchError(DiscoveryError):
    """A metadata document claims an issuer other than the one requested.

    This is an active attack signal (RFC 8414 §3.3), not a reason to try the
    next candidate URL: it aborts the whole flow.
    """


class PkceUnsupportedError(DiscoveryError):
    """An authorization server does not advertise ``S256``.

    Jhin refuses rather than downgrading to ``plain``; there is no setting
    that turns this off.
    """


class RegistrationError(OAuthError):
    """Dynamic client registration failed in a way retrying will not fix."""


class ClientForgottenError(RegistrationError):
    """The authorization server no longer recognises Jhin's client.

    Recoverable for a ``dcr`` registration by registering once more; terminal
    for credentials a human configured.
    """


class TokenError(OAuthError):
    """A token endpoint refused the request.

    ``error_code`` carries the provider's machine-readable code when it is one
    Jhin knows, so callers can branch on it without ever rendering provider
    prose.
    """

    def __init__(self, message: str, *, error_code: str = UNKNOWN_ERROR_CODE) -> None:
        super().__init__(message)
        self.error_code = error_code if error_code in KNOWN_ERROR_CODES else UNKNOWN_ERROR_CODE


class InvalidGrantError(TokenError):
    """The grant is spent, revoked, or expired: re-authorization is required."""


class TransientOAuthError(OAuthError):
    """A rate limit, a 5xx, or a transport failure. Worth retrying."""


class DeviceAuthorizationDenied(OAuthError):
    """The person declined the device-flow request at the provider."""


class DeviceCodeExpired(OAuthError):
    """The device code timed out before it was approved."""


def normalize_error_code(raw: object) -> str:
    """The provider's ``error`` value when Jhin knows it, else ``"unknown"``.

    Bounded before the membership test so a multi-megabyte ``error`` field
    cannot be used to make the comparison expensive.
    """
    if not isinstance(raw, str):
        return UNKNOWN_ERROR_CODE
    candidate = raw.strip()[:64]
    return candidate if candidate in KNOWN_ERROR_CODES else UNKNOWN_ERROR_CODE


__all__ = [
    "KNOWN_ERROR_CODES",
    "UNKNOWN_ERROR_CODE",
    "ClientForgottenError",
    "DeviceAuthorizationDenied",
    "DeviceCodeExpired",
    "DiscoveryError",
    "InvalidGrantError",
    "IssuerMismatchError",
    "OAuthError",
    "PkceUnsupportedError",
    "RegistrationError",
    "TokenError",
    "TransientOAuthError",
    "normalize_error_code",
]
