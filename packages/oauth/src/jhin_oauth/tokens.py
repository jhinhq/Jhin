"""Authorization requests, token exchange, refresh, revocation, device flow.

Two invariants hold across every function here.

**No token ever reaches a URL.** The authorization request carries only public
parameters — client id, redirect URI, state, challenge, scope, resource — and
every credential-bearing request is a form-encoded POST *body*. A token in a
query string is a token in an access log, a ``Referer``, and a proxy's history.

**No provider prose ever reaches a caller.** A token endpoint's
``error_description`` is attacker-influenced text; the machine-readable
``error`` code is kept only after being matched against a known vocabulary, and
the sentence a caller sees is one Jhin wrote.

``state``, ``pkce``, and ``resource`` are non-optional keyword arguments on
:func:`build_authorization_url` by design: there is no code path in Jhin that
starts an authorization without CSRF binding, without PKCE, or without an
audience.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, NoReturn
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from jhin_domain.endpoints import EndpointPolicyError
from jhin_oauth._http import (
    MAX_TOKEN_BYTES,
    BoundedHttpError,
    BoundedResponse,
    build_form_post,
    send_bounded,
)
from jhin_oauth.errors import (
    ClientForgottenError,
    DeviceAuthorizationDenied,
    DeviceCodeExpired,
    InvalidGrantError,
    TokenError,
    TransientOAuthError,
    normalize_error_code,
)
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    ClientCredentials,
    DeviceCodeGrant,
    DeviceTokenPending,
    PkcePair,
    TokenResponse,
)
from jhin_oauth.urls import validate_oauth_url
from jhin_observability import get_logger
from jhin_secrets.redaction import get_redactor

logger = get_logger(__name__)

DEVICE_CODE_GRANT_TYPE: Final[str] = "urn:ietf:params:oauth:grant-type:device_code"

MAX_SCOPE_LENGTH: Final[int] = 2_048
MAX_RESOURCE_LENGTH: Final[int] = 1_000
MAX_STATE_LENGTH: Final[int] = 256
MAX_TOKEN_LENGTH: Final[int] = 8_192
MAX_USER_CODE_LENGTH: Final[int] = 64

DEFAULT_DEVICE_INTERVAL_SECONDS: Final[int] = 5
SLOW_DOWN_INCREMENT_SECONDS: Final[int] = 5
DEFAULT_DEVICE_LIFETIME_SECONDS: Final[int] = 600
MAX_DEVICE_LIFETIME_SECONDS: Final[int] = 1_800

# Parameters the authorization request owns; extra_params may not shadow one.
_RESERVED_AUTHORIZE_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "redirect_uri",
        "resource",
        "response_type",
        "scope",
        "state",
    }
)

_REFUSAL_MESSAGES: Final[Mapping[str, str]] = {
    "invalid_client": "the authorization server no longer recognises this app's registration",
    "invalid_grant": "this connection's authorization is no longer valid",
    "invalid_request": "the authorization server rejected the shape of this request",
    "invalid_scope": "the authorization server refused one of the permissions Jhin asked for",
    "invalid_target": "this server's authorization service rejected the resource identifier",
    "unauthorized_client": "this app is not allowed to use this sign-in method",
    "unsupported_grant_type": "this authorization server does not support this sign-in method",
    "device_flow_disabled": "device sign-in is turned off for this app",
    "incorrect_client_credentials": "this app's credentials were refused",
    "bad_verification_code": "the authorization code was refused",
    "redirect_uri_mismatch": "the redirect URI is not one registered for this app",
}
_GENERIC_REFUSAL: Final[str] = "the authorization server refused the token request"


def _require_bounded(value: str, *, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"the OAuth {label} is missing or too long")
    return value


def build_authorization_url(
    metadata: AuthorizationServerMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    pkce: PkcePair,
    scope: str,
    resource: str,
    extra_params: Mapping[str, str] | None = None,
) -> str:
    """The URL the browser is sent to, with the audience and challenge bound in.

    ``resource`` (RFC 8707) is sent whether or not the server advertises
    support: a server that ignores it is unharmed, and a server that honours it
    issues a token that cannot be replayed against a different resource. The
    one case it is *omitted* is an empty string, which is not a resource Jhin
    failed to compute but a provider that has no resource concept at all — a
    statically-known authorization server like GitHub, whose tokens are scoped
    by the app installation rather than by audience. RFC 8707 requires an
    absolute URI, so sending ``resource=`` to such a server is not "harmless
    and ignored", it is a malformed parameter that a strict server answers
    with ``invalid_target``. The PKCE challenge is by contrast always sent,
    including to providers that do not implement it — an unknown parameter
    costs nothing there, and there is deliberately no branch that skips
    generating one.

    ``extra_params`` carries per-provider quirks. A key that collides with a
    parameter this function owns raises :class:`ValueError` rather than being
    silently dropped or silently winning.
    """
    endpoint = validate_oauth_url(metadata.authorization_endpoint, kind="authorization endpoint")
    _require_bounded(client_id, limit=500, label="client identifier")
    _require_bounded(redirect_uri, limit=500, label="redirect URI")
    _require_bounded(state, limit=MAX_STATE_LENGTH, label="state value")
    if resource:
        _require_bounded(resource, limit=MAX_RESOURCE_LENGTH, label="resource identifier")
    if not pkce.verifier or not pkce.challenge:
        raise ValueError("the OAuth PKCE pair is incomplete")
    if len(scope) > MAX_SCOPE_LENGTH:
        raise ValueError("the OAuth scope string is too long")

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if resource:
        params["resource"] = resource
    if scope:
        params["scope"] = scope
    for key, value in (extra_params or {}).items():
        if key in _RESERVED_AUTHORIZE_PARAMS:
            raise ValueError(f"the OAuth parameter {key!r} cannot be overridden")
        params[key] = value

    parsed = urlsplit(endpoint)
    existing = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in params
    ]
    query = urlencode(existing + list(params.items()))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _client_auth(credentials: ClientCredentials) -> tuple[dict[str, str], dict[str, str]]:
    """Form fields and headers for the stored authentication method.

    Never guessed: a public client that starts sending a secret, or a
    confidential client that stops, is a failed token request at best and a
    leaked secret at worst. ``client_id`` is always in the body, including for
    HTTP Basic, because several servers read it from there regardless.
    """
    fields = {"client_id": credentials.client_id}
    headers: dict[str, str] = {}
    method = credentials.token_endpoint_auth_method
    if method == "none":
        return fields, headers
    if method not in {"client_secret_post", "client_secret_basic"}:
        raise TokenError(
            "this app's client authentication method is not supported",
            error_code="invalid_client",
        )
    if not credentials.client_secret:
        raise TokenError(
            "this app is registered as confidential but stores no client secret",
            error_code="invalid_client",
        )
    if method == "client_secret_post":
        fields["client_secret"] = credentials.client_secret
        return fields, headers
    raw = f"{quote(credentials.client_id, safe='')}:{quote(credentials.client_secret, safe='')}"
    headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return fields, headers


async def _post_token_request(
    client: httpx.AsyncClient,
    token_endpoint: str,
    form: Mapping[str, str],
    headers: Mapping[str, str],
) -> BoundedResponse:
    """One token-endpoint POST, with the endpoint re-validated at use time.

    Re-validating a stored endpoint on every call is what lets an operator
    tighten the allow-list and have it take effect immediately, without a
    migration over stored rows — the same reasoning the MCP client already
    applies to a stored server URL.
    """
    endpoint = validate_oauth_url(token_endpoint, kind="token endpoint")
    try:
        return await send_bounded(
            client,
            build_form_post(client, endpoint, form, headers=headers),
            max_response_bytes=MAX_TOKEN_BYTES,
        )
    except BoundedHttpError as error:
        if error.transient:
            raise TransientOAuthError("the authorization server could not be reached") from None
        raise TokenError("the authorization server's token endpoint is not usable") from None


def _as_refusal(response: BoundedResponse) -> BoundedResponse:
    """The same body, restated as an HTTP 400.

    GitHub's device flow reports ``authorization_pending``, ``access_denied``,
    and the rest with HTTP 200 and the error in the body. Restating the status
    lets one classifier handle both shapes instead of two that can drift.
    """
    return BoundedResponse(
        status_code=400,
        payload=response.payload,
        content_type=response.content_type,
        headers=response.headers,
    )


def _raise_for_refusal(response: BoundedResponse) -> NoReturn:
    """Turn a refused token response into the right exception.

    The taxonomy matters more than the wording: ``invalid_grant`` is terminal
    and must never be retried (retrying trips provider abuse detection),
    ``invalid_client`` means the server forgot a registration and may be worth
    one re-registration, and a 429 or 5xx is worth backing off for.
    """
    status = response.status_code
    if status == 429 or status >= 500:
        raise TransientOAuthError("the authorization server is temporarily unavailable")
    code = normalize_error_code(
        response.payload.get("error") if isinstance(response.payload, dict) else None
    )
    logger.debug("oauth.token_request_refused", status_code=status, error_code=code)
    if code == "invalid_grant":
        raise InvalidGrantError(_REFUSAL_MESSAGES[code], error_code=code)
    if code == "invalid_client" or status == 401:
        raise ClientForgottenError(_REFUSAL_MESSAGES["invalid_client"])
    raise TokenError(_REFUSAL_MESSAGES.get(code, _GENERIC_REFUSAL), error_code=code)


def _positive_seconds(raw: object) -> int | None:
    """A lifetime in seconds, from a JSON number or a numeric string.

    Providers disagree about the type; RFC 6749 says number, several send a
    string. Anything else is treated as absent rather than guessed at.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        return None
    return value if 0 < value <= 10 * 365 * 24 * 3600 else None


def _instant(seconds: int | None, *, now: datetime) -> datetime | None:
    return None if seconds is None else now + timedelta(seconds=seconds)


def _token_response(
    payload: object,
    *,
    issuer: str,
    previous_refresh_token: str | None = None,
    now: datetime | None = None,
) -> TokenResponse:
    """One successful token payload, with every token registered for redaction.

    Registration happens at the moment of first possession — before the value
    is returned, let alone stored — so anything that later renders it through
    the logging pipeline finds it already known.
    """
    if not isinstance(payload, dict):
        raise TokenError("the authorization server returned an unusable token response")
    access_token = payload.get("access_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or len(access_token) > MAX_TOKEN_LENGTH
    ):
        raise TokenError("the authorization server returned no access token")

    raw_refresh = payload.get("refresh_token")
    refresh_token = (
        raw_refresh
        if isinstance(raw_refresh, str) and 0 < len(raw_refresh) <= MAX_TOKEN_LENGTH
        else previous_refresh_token
    )

    redactor = get_redactor()
    redactor.register(access_token)
    if refresh_token:
        redactor.register(refresh_token)

    token_type = payload.get("token_type")
    scope = payload.get("scope")
    moment = now if now is not None else datetime.now(UTC)
    refresh_lifetime = _positive_seconds(
        payload.get("refresh_token_expires_in", payload.get("refresh_expires_in"))
    )
    return TokenResponse(
        access_token=access_token,
        token_type=token_type if isinstance(token_type, str) and token_type else "Bearer",
        refresh_token=refresh_token,
        expires_at=_instant(_positive_seconds(payload.get("expires_in")), now=moment),
        refresh_expires_at=_instant(refresh_lifetime, now=moment),
        scope=scope if isinstance(scope, str) and len(scope) <= MAX_SCOPE_LENGTH else "",
        issuer=issuer,
    )


async def exchange_code(
    client: httpx.AsyncClient,
    metadata: AuthorizationServerMetadata,
    *,
    credentials: ClientCredentials,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str,
) -> TokenResponse:
    """Trade one authorization code for a token set.

    ``resource`` is sent again here, exactly as it was on the authorization
    request: RFC 8707 requires the two to agree, and a server that honours it
    binds the audience of the token it issues. Omitted when empty, for the
    same reason :func:`build_authorization_url` omits it — and omitting it in
    both places is what keeps the two requests agreeing.

    Raises :class:`~jhin_oauth.errors.TokenError`,
    :class:`~jhin_oauth.errors.InvalidGrantError`,
    :class:`~jhin_oauth.errors.ClientForgottenError`,
    :class:`~jhin_oauth.errors.TransientOAuthError`, and
    :class:`jhin_domain.endpoints.EndpointPolicyError`.
    """
    get_redactor().register(code)
    fields, headers = _client_auth(credentials)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        **({"resource": resource} if resource else {}),
        **fields,
    }
    response = await _post_token_request(client, metadata.token_endpoint, form, headers)
    if not response.is_success:
        _raise_for_refusal(response)
    if isinstance(response.payload, dict) and isinstance(response.payload.get("error"), str):
        # GitHub reports a refused exchange — ``incorrect_client_credentials``,
        # ``redirect_uri_mismatch``, ``bad_verification_code`` — with HTTP 200
        # and the error in the body, exactly as its device flow does. Restated
        # as a refusal so the one classifier names it, instead of the shape
        # check below reporting "no access token" and losing the reason.
        _raise_for_refusal(_as_refusal(response))
    return _token_response(response.payload, issuer=metadata.issuer)


async def refresh_access_token(
    client: httpx.AsyncClient,
    metadata: AuthorizationServerMetadata,
    *,
    credentials: ClientCredentials,
    refresh_token: str,
    resource: str,
    scope: str | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a fresh token set.

    The old refresh token is carried forward when the provider does not rotate
    — most do not, and treating a missing ``refresh_token`` as "we no longer
    have one" would turn every non-rotating provider into a re-authorization
    an hour later.

    Same exceptions as :func:`exchange_code`.
    """
    fields, headers = _client_auth(credentials)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        **({"resource": resource} if resource else {}),
        **fields,
    }
    if scope:
        form["scope"] = scope
    response = await _post_token_request(client, metadata.token_endpoint, form, headers)
    if not response.is_success:
        _raise_for_refusal(response)
    return _token_response(
        response.payload, issuer=metadata.issuer, previous_refresh_token=refresh_token
    )


async def revoke_token(
    client: httpx.AsyncClient,
    metadata: AuthorizationServerMetadata,
    *,
    credentials: ClientCredentials,
    token: str,
    token_type_hint: Literal["access_token", "refresh_token"],
) -> None:
    """Best-effort RFC 7009 revocation. Never raises.

    A server with no revocation endpoint, a server that refuses, and a server
    that cannot be reached are all the same outcome: the local credential is
    deleted either way, and a disconnect that fails because the provider was
    briefly down would be a worse product than one that quietly leaves a token
    to expire.
    """
    if not metadata.revocation_endpoint:
        return
    try:
        endpoint = validate_oauth_url(
            metadata.revocation_endpoint, kind="token revocation endpoint"
        )
        fields, headers = _client_auth(credentials)
        await send_bounded(
            client,
            build_form_post(
                client,
                endpoint,
                {"token": token, "token_type_hint": token_type_hint, **fields},
                headers=headers,
            ),
            max_response_bytes=MAX_TOKEN_BYTES,
        )
    except Exception:
        logger.debug("oauth.revocation_failed")


async def start_device_authorization(
    client: httpx.AsyncClient,
    *,
    device_authorization_endpoint: str,
    client_id: str,
    scope: str = "",
) -> DeviceCodeGrant:
    """Begin RFC 8628 device authorization.

    The verification URI is put through the same outbound policy as every other
    provider-supplied URL, because it is a URL Jhin puts in front of a person
    and invites them to open.

    Raises :class:`~jhin_oauth.errors.TokenError`,
    :class:`~jhin_oauth.errors.TransientOAuthError`, and
    :class:`jhin_domain.endpoints.EndpointPolicyError`.
    """
    endpoint = validate_oauth_url(
        device_authorization_endpoint, kind="device authorization endpoint"
    )
    form = {"client_id": client_id}
    if scope:
        form["scope"] = scope
    try:
        response = await send_bounded(
            client,
            build_form_post(client, endpoint, form),
            max_response_bytes=MAX_TOKEN_BYTES,
        )
    except BoundedHttpError as error:
        if error.transient:
            raise TransientOAuthError("the authorization server could not be reached") from None
        raise TokenError("the device authorization endpoint is not usable") from None

    if not response.is_success:
        _raise_for_refusal(response)
    payload = response.payload
    if not isinstance(payload, dict):
        raise TokenError("the authorization server returned an unusable device authorization")
    if isinstance(payload.get("error"), str):
        _raise_for_refusal(_as_refusal(response))

    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    if (
        not isinstance(device_code, str)
        or not device_code
        or len(device_code) > MAX_TOKEN_LENGTH
        or not isinstance(user_code, str)
        or not user_code
        or len(user_code) > MAX_USER_CODE_LENGTH
    ):
        raise TokenError("the authorization server returned an unusable device authorization")
    get_redactor().register(device_code)

    raw_verification_uri = payload.get("verification_uri", payload.get("verification_url"))
    if not isinstance(raw_verification_uri, str) or not raw_verification_uri:
        raise TokenError("the authorization server returned no device verification address")
    verification_uri = validate_oauth_url(raw_verification_uri, kind="device verification URL")

    verification_uri_complete: str | None = None
    raw_complete = payload.get("verification_uri_complete")
    if isinstance(raw_complete, str) and raw_complete:
        try:
            verification_uri_complete = validate_oauth_url(
                raw_complete, kind="device verification URL"
            )
        except EndpointPolicyError:
            logger.debug("oauth.device_verification_complete_refused")

    lifetime = _positive_seconds(payload.get("expires_in")) or DEFAULT_DEVICE_LIFETIME_SECONDS
    interval = _positive_seconds(payload.get("interval")) or DEFAULT_DEVICE_INTERVAL_SECONDS
    return DeviceCodeGrant(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        expires_at=datetime.now(UTC)
        + timedelta(seconds=min(lifetime, MAX_DEVICE_LIFETIME_SECONDS)),
        interval_seconds=interval,
    )


async def poll_device_token(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    client_id: str,
    device_code: str,
    client_secret: str | None = None,
) -> TokenResponse | DeviceTokenPending:
    """Ask once whether the device grant has been approved.

    GitHub — the reason this flow exists in Jhin at all, because it needs no
    redirect URI and works from an instance nobody can reach — returns
    ``authorization_pending`` and friends with HTTP 200 and the error in the
    body, so both shapes are handled. ``slow_down`` is the server asking to be
    polled less often; the returned interval must never be lowered again for
    this grant.

    Raises :class:`~jhin_oauth.errors.DeviceAuthorizationDenied`,
    :class:`~jhin_oauth.errors.DeviceCodeExpired`,
    :class:`~jhin_oauth.errors.TokenError`, and
    :class:`~jhin_oauth.errors.TransientOAuthError`.
    """
    form = {
        "grant_type": DEVICE_CODE_GRANT_TYPE,
        "device_code": device_code,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    response = await _post_token_request(client, token_endpoint, form, {})

    payload = response.payload
    reported = payload.get("error") if isinstance(payload, dict) else None
    error_code = normalize_error_code(reported)
    if error_code in {"authorization_pending", "slow_down"}:
        advertised = _positive_seconds(
            payload.get("interval") if isinstance(payload, dict) else None
        )
        interval = advertised or DEFAULT_DEVICE_INTERVAL_SECONDS
        if error_code == "slow_down":
            return DeviceTokenPending(
                reason="slow_down",
                interval_seconds=max(
                    interval, DEFAULT_DEVICE_INTERVAL_SECONDS + SLOW_DOWN_INCREMENT_SECONDS
                ),
            )
        return DeviceTokenPending(reason="authorization_pending", interval_seconds=interval)
    if error_code == "access_denied":
        raise DeviceAuthorizationDenied("the request was declined at the provider")
    if error_code in {"expired_token", "incorrect_device_code"}:
        raise DeviceCodeExpired("that sign-in code expired before it was approved")
    if not response.is_success:
        _raise_for_refusal(response)
    if isinstance(reported, str):
        _raise_for_refusal(_as_refusal(response))
    return _token_response(payload, issuer="")


def next_poll_interval(current_seconds: int, pending: DeviceTokenPending) -> int:
    """The interval to poll with next, which never goes down.

    RFC 8628 §3.5 makes ``slow_down`` a permanent five-second increase, not a
    one-off pause: a client that returns to its old cadence gets throttled
    again, and eventually rejected. Callers persist the result.
    """
    if pending.reason == "slow_down":
        return max(current_seconds + SLOW_DOWN_INCREMENT_SECONDS, pending.interval_seconds)
    return max(current_seconds, pending.interval_seconds)


__all__ = [
    "DEFAULT_DEVICE_INTERVAL_SECONDS",
    "DEFAULT_DEVICE_LIFETIME_SECONDS",
    "DEVICE_CODE_GRANT_TYPE",
    "MAX_DEVICE_LIFETIME_SECONDS",
    "MAX_RESOURCE_LENGTH",
    "MAX_SCOPE_LENGTH",
    "SLOW_DOWN_INCREMENT_SECONDS",
    "build_authorization_url",
    "exchange_code",
    "next_poll_interval",
    "poll_device_token",
    "refresh_access_token",
    "revoke_token",
    "start_device_authorization",
]
