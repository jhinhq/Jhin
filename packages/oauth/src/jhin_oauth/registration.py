"""RFC 7591 dynamic client registration, and its RFC 7592 clean-up.

Registration is what makes "click Connect, land on the consent screen" possible
for a server Jhin has never seen: no operator copies a client id, because the
authorization server mints one on the spot. Two fields are sent explicitly
that RFC 7591 would otherwise default badly:

- ``grant_types`` — the default is ``["authorization_code"]`` alone, and a
  client registered that way silently cannot refresh;
- ``token_endpoint_auth_method`` — the default is ``client_secret_basic``,
  which means the server issues a secret Jhin then has to store. Asking for
  ``none`` and pairing it with PKCE keeps a secret from existing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_oauth._http import (
    MAX_METADATA_BYTES,
    BoundedHttpError,
    build_json_post,
    send_bounded,
)
from jhin_oauth.errors import RegistrationError, TransientOAuthError, normalize_error_code
from jhin_oauth.types import AuthorizationServerMetadata, ClientCredentials
from jhin_oauth.urls import validate_oauth_url
from jhin_observability import get_logger
from jhin_secrets.redaction import get_redactor

logger = get_logger(__name__)

MAX_CLIENT_ID_LENGTH: Final[int] = 500
MAX_CLIENT_SECRET_LENGTH: Final[int] = 4_096
MAX_REGISTRATION_TOKEN_LENGTH: Final[int] = 4_096

AUTH_METHODS: Final[frozenset[str]] = frozenset(
    {"none", "client_secret_post", "client_secret_basic"}
)
GRANT_TYPES: Final[tuple[str, ...]] = ("authorization_code", "refresh_token")
RESPONSE_TYPES: Final[tuple[str, ...]] = ("code",)

# RFC 7591 §3.2.2 codes, which do not overlap the token-endpoint vocabulary.
_REGISTRATION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_redirect_uri", "invalid_client_metadata", "invalid_software_statement"}
)


def _registration_document(
    *,
    redirect_uri: str,
    client_name: str,
    client_uri: str | None,
    scopes: str,
    application_type: str,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "redirect_uris": [redirect_uri],
        "client_name": client_name,
        "grant_types": list(GRANT_TYPES),
        "response_types": list(RESPONSE_TYPES),
        "token_endpoint_auth_method": "none",
        "application_type": application_type,
    }
    if client_uri:
        document["client_uri"] = client_uri
    if scopes:
        document["scope"] = scopes
    return document


def _parse_credentials(payload: object) -> ClientCredentials:
    if not isinstance(payload, dict):
        raise RegistrationError("the authorization server returned an unusable registration")
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > MAX_CLIENT_ID_LENGTH:
        raise RegistrationError("the authorization server returned no client identifier")

    secret = payload.get("client_secret")
    client_secret = (
        secret if isinstance(secret, str) and 0 < len(secret) <= MAX_CLIENT_SECRET_LENGTH else None
    )

    method = payload.get("token_endpoint_auth_method")
    if isinstance(method, str) and method in AUTH_METHODS:
        auth_method = method
    else:
        # RFC 7591 §2: a server that omits the field registered the default,
        # which is client_secret_basic. Believing our own request instead would
        # send an unauthenticated token request to a confidential client.
        auth_method = "client_secret_basic" if client_secret else "none"

    registration_token = payload.get("registration_access_token")
    access_token = (
        registration_token
        if isinstance(registration_token, str)
        and 0 < len(registration_token) <= MAX_REGISTRATION_TOKEN_LENGTH
        else None
    )

    registration_uri: str | None = None
    raw_uri = payload.get("registration_client_uri")
    if isinstance(raw_uri, str) and raw_uri:
        try:
            registration_uri = validate_oauth_url(raw_uri, kind="client configuration endpoint")
        except EndpointPolicyError:
            logger.debug("oauth.registration_client_uri_refused")

    redactor = get_redactor()
    for value in (client_secret, access_token):
        if value:
            redactor.register(value)

    return ClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint_auth_method=auth_method,
        registration_access_token=access_token,
        registration_client_uri=registration_uri,
        client_secret_expires_at=_secret_expiry(payload.get("client_secret_expires_at")),
    )


def _secret_expiry(raw: object) -> datetime | None:
    """RFC 7591 seconds-since-epoch, where ``0`` means "never expires"."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


async def register_client(
    client: httpx.AsyncClient,
    metadata: AuthorizationServerMetadata,
    *,
    redirect_uri: str,
    client_name: str,
    client_uri: str | None = None,
    scopes: str = "",
    application_type: Literal["web", "native"] = "web",
) -> ClientCredentials:
    """Register Jhin with one authorization server.

    Accepts HTTP 200 as well as the 201 RFC 7591 specifies, because several
    servers answer 200. ``registration_access_token`` and
    ``registration_client_uri`` are RFC 7592 extensions, not RFC 7591 fields:
    they are captured when offered and a registration is never failed for
    their absence.

    A server that rejects the redirect URI for a ``web`` client is retried
    exactly once as ``native`` — some servers classify by application type
    rather than by the URI itself — and then given up on.

    Raises :class:`~jhin_oauth.errors.RegistrationError`,
    :class:`~jhin_oauth.errors.TransientOAuthError`, and
    :class:`jhin_connectors.endpoints.EndpointPolicyError`.
    """
    if not metadata.registration_endpoint:
        raise RegistrationError(
            "this authorization server does not accept new client registrations"
        )
    endpoint = validate_oauth_url(
        metadata.registration_endpoint, kind="client registration endpoint"
    )

    attempted: list[str] = [application_type]
    while True:
        document = _registration_document(
            redirect_uri=redirect_uri,
            client_name=client_name,
            client_uri=client_uri,
            scopes=scopes,
            application_type=attempted[-1],
        )
        try:
            response = await send_bounded(
                client,
                build_json_post(client, endpoint, document),
                max_response_bytes=MAX_METADATA_BYTES,
            )
        except BoundedHttpError as error:
            if error.transient:
                raise TransientOAuthError(
                    "the authorization server could not be reached to register this app"
                ) from None
            raise RegistrationError(
                "the authorization server's registration endpoint is not usable"
            ) from None

        if response.is_success:
            return _parse_credentials(response.payload)

        if response.status_code == 429 or response.status_code >= 500:
            raise TransientOAuthError(
                "the authorization server is temporarily unable to register this app"
            )

        error_code = _registration_error_code(response.payload)
        if (
            error_code == "invalid_redirect_uri"
            and attempted[-1] == "web"
            and "native" not in attempted
        ):
            logger.debug("oauth.registration_retry_native")
            attempted.append("native")
            continue
        raise RegistrationError("the authorization server rejected Jhin's registration")


def _registration_error_code(payload: object) -> str:
    """RFC 7591 §3.2.2 registration error codes.

    Kept separate from the token vocabulary: ``invalid_redirect_uri`` and
    ``invalid_client_metadata`` exist only here, and the retry decision turns
    on the first of them.
    """
    if not isinstance(payload, dict):
        return "unknown"
    raw = payload.get("error")
    if not isinstance(raw, str):
        return "unknown"
    candidate = raw.strip()[:64]
    if candidate in _REGISTRATION_ERROR_CODES:
        return candidate
    return normalize_error_code(candidate)


async def delete_registration(client: httpx.AsyncClient, credentials: ClientCredentials) -> None:
    """Best-effort RFC 7592 ``DELETE`` of a registration Jhin created.

    Returns silently when the server offered no configuration endpoint or no
    registration access token, when it refuses, and when it cannot be reached.
    Tidiness is worth one request and not one failed disconnect: the local row
    is what actually stops Jhin using the client.
    """
    if not credentials.registration_client_uri or not credentials.registration_access_token:
        return
    try:
        endpoint = validate_oauth_url(
            credentials.registration_client_uri, kind="client configuration endpoint"
        )
        request = client.build_request(
            "DELETE",
            endpoint,
            headers={"Authorization": f"Bearer {credentials.registration_access_token}"},
            timeout=httpx.Timeout(30.0),
        )
        await send_bounded(client, request, max_response_bytes=MAX_METADATA_BYTES)
    except Exception:
        logger.debug("oauth.registration_delete_failed")


__all__ = [
    "AUTH_METHODS",
    "GRANT_TYPES",
    "MAX_CLIENT_ID_LENGTH",
    "MAX_CLIENT_SECRET_LENGTH",
    "RESPONSE_TYPES",
    "delete_registration",
    "register_client",
]
