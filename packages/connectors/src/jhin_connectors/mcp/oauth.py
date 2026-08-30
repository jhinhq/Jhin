"""OAuth 2.1 wiring for MCP connections (docs/architecture/oauth.md).

The protocol itself lives in :mod:`jhin_oauth` — discovery, PKCE, dynamic
client registration, the token endpoint, and the durable token lifecycle.
This module is the *connector* half: what an MCP connection stores, how a
stored access token becomes a request header, how a server's ``401``/``403``
challenge is read, and what the tool worker does when a token it believed
was good comes back rejected.

Three rules shape everything here.

**A token is bound to one resource.** Every MCP OAuth connection records the
RFC 8707 audience its tokens were issued for in ``config_json`` under
``oauth_resource``. :func:`oauth_auth_headers` recomputes that audience from
the URL the transport is about to dial and refuses when the two differ, so
editing a connection's ``server_url`` after authorization cannot send its
token somewhere new. That is the MCP token-passthrough prohibition made
mechanical rather than documentary.

**A challenge is untrusted text.** ``WWW-Authenticate`` is written by the
server. Its error code is matched against RFC 6750's closed set (anything
else becomes ``"unknown"``), its scope tokens are filtered to the RFC 6749
grammar and capped, and its ``error_description`` is never read at all.
Nothing a server writes reaches a person or a model through this module.

**A rejection is diagnosed, not retried blindly.** ``401`` means the token
was refused: force one refresh and try again, once. ``403
insufficient_scope`` means the grant is too narrow: no refresh can fix it,
so the wider scope is parked on the connection for the Reconnect button and
a person is asked — at most once per tool per day, because a server that
keeps asking is not going to stop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from jhin_db.models import Connection
from jhin_domain import ConnectionStatus
from jhin_oauth.errors import OAuthError
from jhin_oauth.lifecycle import ConnectionTokenService
from jhin_oauth.urls import canonical_resource_uri, parse_www_authenticate
from jhin_secrets.redaction import get_redactor
from jhin_tools.builtin import ToolExecutionContext

AUTH_OAUTH: Final[str] = "oauth"

#: This module deliberately imports nothing from the rest of the MCP package:
#: the manifest names the auth scheme declared here, so the dependency has to
#: run this way round. Its own timeout therefore stands alone rather than
#: borrowing the transport's.
OAUTH_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: The RFC 8707 audience the connection's tokens were issued for.
OAUTH_RESOURCE_KEY: Final[str] = "oauth_resource"
#: The authorization server that issued them, as validated during discovery.
OAUTH_ISSUER_KEY: Final[str] = "oauth_issuer"
#: The scope string the tokens actually carry.
OAUTH_SCOPE_KEY: Final[str] = "oauth_scope"
#: The wider scope a Reconnect should ask for after an insufficient_scope 403.
OAUTH_PENDING_SCOPE_KEY: Final[str] = "oauth_pending_scope"
#: tool name -> ISO-8601 UTC time of the last step-up asked for that tool.
OAUTH_STEP_UPS_KEY: Final[str] = "oauth_scope_step_ups"

#: Internal ``config_json`` keys this module owns. None of them is a manifest
#: config field, so none of them is visible through ``public_connection_config``.
OAUTH_CONFIG_KEYS: Final[tuple[str, ...]] = (
    OAUTH_RESOURCE_KEY,
    OAUTH_ISSUER_KEY,
    OAUTH_SCOPE_KEY,
    OAUTH_PENDING_SCOPE_KEY,
    OAUTH_STEP_UPS_KEY,
)

#: Credential-map fields written by ``jhin_oauth.lifecycle.token_map``.
ACCESS_TOKEN_FIELD: Final[str] = "access_token"
REFRESH_TOKEN_FIELD: Final[str] = "refresh_token"
TOKEN_TYPE_FIELD: Final[str] = "token_type"

MAX_RESOURCE_CHARS: Final[int] = 1_000
MAX_ISSUER_CHARS: Final[int] = 500
MAX_SCOPE_CHARS: Final[int] = 2_048
MAX_SCOPE_ENTRIES: Final[int] = 64
MAX_SCOPE_TOKEN_CHARS: Final[int] = 128
MAX_METADATA_URL_CHARS: Final[int] = 2_048
MAX_STEP_UP_ENTRIES: Final[int] = 64

#: A second insufficient_scope for the same connection and tool inside this
#: window is a permanent failure: the reconnect was already asked for.
SCOPE_STEP_UP_COOLDOWN_SECONDS: Final[int] = 86_400

#: RFC 6750 section 3.1 defines exactly these three. Anything else a server
#: writes is reported as "unknown" and never echoed.
CHALLENGE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_request", "invalid_token", "insufficient_scope"}
)
UNKNOWN_CHALLENGE_ERROR: Final[str] = "unknown"

#: Scopes that grant everything are never requested, never stored, and never
#: carried forward out of a challenge.
WILDCARD_SCOPES: Final[frozenset[str]] = frozenset({"*", "all", "full-access", "full_access"})

# RFC 6749 section 3.3: scope-token = 1*( %x21 / %x23-5B / %x5D-7E ).
_SCOPE_TOKEN_RE = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+")


class McpOAuthConfigError(ValueError):
    """A stored OAuth setting is unusable. The message names the field and
    never any part of a token, a URL, or a server's own words."""


@dataclass(frozen=True, slots=True)
class McpChallenge:
    """One parsed ``WWW-Authenticate`` Bearer challenge.

    Every field is either a Jhin constant or a value filtered to a closed
    grammar. ``error_description`` and ``error_uri`` are deliberately absent:
    they are free text a hostile server controls, and nothing in Jhin has a
    use for them.
    """

    status_code: int
    error: str
    resource_metadata_url: str | None
    scope: tuple[str, ...]

    @property
    def token_rejected(self) -> bool:
        """True when the credential itself was refused, so one forced refresh
        is worth trying. A bare ``401`` with no readable challenge still
        counts — that is the whole meaning of the status code."""
        return self.status_code == 401 and self.error != "insufficient_scope"

    @property
    def needs_more_scope(self) -> bool:
        """True when the token is valid but too narrow. No refresh can widen
        a grant; only a person re-authorizing can."""
        return self.error == "insufficient_scope"


def reconnect_message(connection_name: str) -> str:
    """The one sentence a person or an agent is told when a connection's
    OAuth access cannot be renewed without somebody signing in again."""
    return (
        f"This app needs to be reconnected. Ask an admin to reconnect "
        f"{_safe_name(connection_name)} in Apps."
    )


def scope_step_up_message(new_scope_count: int) -> str:
    """Asked once, when a server says the grant is too narrow and names what
    it wants. Only the count crosses the boundary — the scope names are the
    server's text."""
    return (
        f"This app needs additional permission ({new_scope_count} new scopes). "
        "Reconnect it in Apps to grant them."
    )


def scope_exhausted_message(connection_name: str) -> str:
    """Asked at most once a day: the reconnect was already requested and the
    server still refuses, so re-asking would only loop."""
    return (
        f"{_safe_name(connection_name)} still refuses this tool after a reconnect was "
        "already requested today. An admin needs to widen this app's permissions where "
        "the app itself is administered, then reconnect it in Apps."
    )


def scope_unnameable_message(connection_name: str) -> str:
    """The server refused for want of scope but named nothing new to ask
    for. Reconnecting would request exactly what it already refused."""
    return (
        f"{_safe_name(connection_name)} refuses this tool and does not say which extra "
        "permission it needs. An admin has to grant it where the app itself is "
        "administered."
    )


def resource_binding(server_url: str) -> str:
    """The RFC 8707 audience an MCP connection's tokens are bound to.

    Stored on the connection at authorization time and recomputed from the
    dialled URL on every call; the two must match exactly.
    """
    return canonical_resource_uri(server_url)


def oauth_auth_headers(
    credentials: Mapping[str, str],
    config: Mapping[str, Any],
    *,
    validated_server_url: str,
) -> dict[str, str]:
    """``Authorization: Bearer <access_token>`` for one OAuth connection.

    Refuses when the token is missing or contains CR/LF/NUL (the same guard
    as :func:`jhin_connectors.mcp.client.auth_headers`), when the stored
    token type is not Bearer (MCP allows no other), and when the connection's
    recorded audience is not the canonical URI of the server about to be
    dialled — the token-passthrough prohibition, enforced rather than
    documented. Raises :class:`McpOAuthConfigError` (a ``ValueError``) with a
    credential-free message.
    """
    token = credentials.get(ACCESS_TOKEN_FIELD, "")
    if not token:
        raise McpOAuthConfigError("this MCP connection stores no OAuth access token")
    if any(character in token for character in "\r\n\0"):
        raise McpOAuthConfigError("this MCP connection's OAuth access token is malformed")
    token_type = credentials.get(TOKEN_TYPE_FIELD, "Bearer").strip()
    if token_type and token_type.lower() != "bearer":
        raise McpOAuthConfigError("this MCP connection's token is not a bearer token")

    expected = config.get(OAUTH_RESOURCE_KEY)
    if not isinstance(expected, str) or not expected:
        raise McpOAuthConfigError("this MCP connection records no authorized resource")
    try:
        actual = canonical_resource_uri(validated_server_url)
    except ValueError:
        raise McpOAuthConfigError(
            "this MCP connection's server URL cannot be an OAuth resource"
        ) from None
    if actual != expected:
        raise McpOAuthConfigError(
            "this MCP connection's server no longer matches the account it was authorized for"
        )

    get_redactor().register(token)
    return {"Authorization": f"Bearer {token}"}


def challenge_from_response(status_code: int, headers: Mapping[str, str]) -> McpChallenge | None:
    """Parse a ``401``/``403`` Bearer challenge. ``None`` for anything else.

    A missing or malformed ``WWW-Authenticate`` still yields a challenge for
    a ``401``/``403``: the status code alone is the server's answer, and the
    caller needs it. Every parsed value is filtered — the error code against
    RFC 6750's closed set, the scope against the RFC 6749 grammar, the
    metadata URL by length only (its policy check happens where it is
    fetched, in :mod:`jhin_oauth.discovery`).
    """
    if status_code not in (401, 403):
        return None
    raw = _header(headers, "www-authenticate")
    params = parse_www_authenticate(raw) if raw else {}
    raw_error = params.get("error", "").strip()
    if not raw_error:
        error = ""
    elif raw_error in CHALLENGE_ERROR_CODES:
        error = raw_error
    else:
        error = UNKNOWN_CHALLENGE_ERROR
    metadata_url = params.get("resource_metadata", "").strip()
    return McpChallenge(
        status_code=status_code,
        error=error,
        resource_metadata_url=(
            metadata_url if metadata_url and len(metadata_url) <= MAX_METADATA_URL_CHARS else None
        ),
        scope=parse_scope(params.get("scope")),
    )


def parse_scope(raw: str | None) -> tuple[str, ...]:
    """Space-delimited scope text to a bounded tuple of RFC 6749 scope tokens.

    Duplicates collapse, order is preserved, wildcards are dropped, and
    anything outside the grammar never existed.
    """
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for candidate in raw.split():
        if len(candidate) > MAX_SCOPE_TOKEN_CHARS or candidate.lower() in WILDCARD_SCOPES:
            continue
        if not _SCOPE_TOKEN_RE.fullmatch(candidate):
            continue
        seen.setdefault(candidate, None)
        if len(seen) >= MAX_SCOPE_ENTRIES:
            break
    return tuple(seen)


def merge_scope(stored: str, additional: Sequence[str]) -> str:
    """The union of a connection's scope and a challenge's, stored order
    first, bounded the same way :func:`jhin_oauth.discovery.select_scopes`
    bounds a fresh request."""
    merged: dict[str, None] = {}
    for token in (*parse_scope(stored), *parse_scope(" ".join(additional))):
        merged.setdefault(token, None)
        if len(merged) >= MAX_SCOPE_ENTRIES:
            break
    rendered = " ".join(merged)
    if len(rendered) <= MAX_SCOPE_CHARS:
        return rendered
    kept: list[str] = []
    for token in merged:
        candidate = " ".join((*kept, token))
        if len(candidate) > MAX_SCOPE_CHARS:
            break
        kept.append(token)
    return " ".join(kept)


def validate_oauth_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the OAuth settings of a stored config, checked and bounded.

    These keys are written by the authorization flow, not by an admin typing
    into a form, so this is a shape check rather than a policy one: strings,
    within their column widths, scopes inside the grammar. A value that fails
    is a bug or a tampered row, and either way the connection must not be
    used.
    """
    checked: dict[str, Any] = {}
    for key, limit in (
        (OAUTH_RESOURCE_KEY, MAX_RESOURCE_CHARS),
        (OAUTH_ISSUER_KEY, MAX_ISSUER_CHARS),
    ):
        if key in config:
            value = config[key]
            if not isinstance(value, str) or len(value) > limit:
                raise McpOAuthConfigError(f"config field {key!r} must be text")
            checked[key] = value
    for key in (OAUTH_SCOPE_KEY, OAUTH_PENDING_SCOPE_KEY):
        if key in config:
            value = config[key]
            if not isinstance(value, str) or len(value) > MAX_SCOPE_CHARS:
                raise McpOAuthConfigError(f"config field {key!r} must be text")
            checked[key] = " ".join(parse_scope(value))
    if OAUTH_STEP_UPS_KEY in config:
        checked[OAUTH_STEP_UPS_KEY] = _bounded_step_ups(config[OAUTH_STEP_UPS_KEY])
    return checked


async def reauthorized_headers(
    ctx: ToolExecutionContext,
    connection: Connection,
    config: Mapping[str, Any],
    *,
    credentials: Mapping[str, str],
    validated_server_url: str,
    timeout_seconds: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Force one refresh after a ``401`` and return the retry headers.

    The proactive refresher and the on-use check in ``resolve_connection``
    both work from ``expires_at``; a ``401`` on a token that is not yet
    expired means the provider disagreed — clock skew, an early revocation,
    a refresh token rotated by another process. One forced exchange is the
    documented cure and is attempted exactly once per call.

    ``None`` means a person has to reconnect the app. A connection that holds
    no refresh token is that case immediately: there is nothing to exchange,
    so the authorization server is never troubled for an answer already known.
    Raises :class:`jhin_oauth.errors.TransientOAuthError` when the
    authorization server was momentarily unreachable, which is worth retrying
    later but not here.
    """
    if ctx.crypto is None or not credentials.get(REFRESH_TOKEN_FIELD):
        return None
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(timeout_seconds)
    ) as http_client:
        service = ConnectionTokenService(ctx.session, ctx.crypto, http_client)
        try:
            outcome = await service.refresh(connection)
        except OAuthError:
            raise
        except Exception:
            # A refresh that failed for a reason the OAuth core does not
            # classify leaves the connection exactly as it was; the caller
            # asks for a reconnect rather than guessing.
            return None
        if not outcome.refreshed:
            return None
        token = await service.access_token(connection)
    return oauth_auth_headers(
        {ACCESS_TOKEN_FIELD: token}, config, validated_server_url=validated_server_url
    )


def record_scope_step_up(
    connection: Connection,
    *,
    tool_name: str,
    challenge: McpChallenge,
    now: datetime | None = None,
) -> str:
    """Park the wider scope on the connection and ask for one reconnect.

    Returns the sentence the agent sees. Three outcomes, and only the first
    changes the row:

    * the challenge names scopes the connection does not hold — the union is
      stored under ``oauth_pending_scope`` so the Reconnect button asks for
      it, the connection goes to ``needs_reauth``, and the ask is dated;
    * the same tool already asked inside the cooldown — permanent failure,
      no row change, because re-asking is the loop the spec forbids;
    * the challenge names nothing new — reconnecting would request exactly
      what was just refused, so a person is pointed at the provider instead.
    """
    moment = now if now is not None else datetime.now(UTC)
    config = dict(connection.config_json)
    history = _bounded_step_ups(config.get(OAUTH_STEP_UPS_KEY), now=moment)
    if _within_cooldown(history.get(tool_name), now=moment):
        return scope_exhausted_message(connection.name)

    stored_scope = config.get(OAUTH_SCOPE_KEY)
    stored = stored_scope if isinstance(stored_scope, str) else ""
    merged = merge_scope(stored, challenge.scope)
    held = set(parse_scope(stored))
    new_scopes = [token for token in parse_scope(merged) if token not in held]
    if not new_scopes:
        return scope_unnameable_message(connection.name)

    history[tool_name] = moment.isoformat()
    config[OAUTH_PENDING_SCOPE_KEY] = merged
    config[OAUTH_STEP_UPS_KEY] = _bounded_step_ups(history, now=moment)
    connection.config_json = config
    message = scope_step_up_message(len(new_scopes))
    # Not routed through ``ConnectionTokenService.mark_needs_reauth``: nothing
    # about a token failed here, so zeroing the refresh-failure counter would
    # be a lie. The two columns that describe "a person must act" are written
    # directly, and the caller's session commits them with the rest of the
    # tool call.
    connection.status = ConnectionStatus.NEEDS_REAUTH.value
    connection.last_error = message[:2000]
    return message


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive lookup that works for a plain dict as well as
    ``httpx.Headers``."""
    direct = headers.get(name)
    if isinstance(direct, str):
        return direct
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name and isinstance(value, str):
            return value
    return ""


def _safe_name(connection_name: str) -> str:
    """A connection name is admin-authored, but it still travels into a model
    observation, so it is bounded and stripped of anything that could break
    the sentence around it."""
    collapsed = " ".join(str(connection_name).split())
    return collapsed[:80] if collapsed else "this app"


def _within_cooldown(recorded: str | None, *, now: datetime) -> bool:
    moment = _parse_timestamp(recorded)
    if moment is None:
        return False
    return now - moment < timedelta(seconds=SCOPE_STEP_UP_COOLDOWN_SECONDS)


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _bounded_step_ups(raw: object, *, now: datetime | None = None) -> dict[str, str]:
    """Keep only well-formed, still-relevant entries, newest first."""
    if not isinstance(raw, Mapping):
        return {}
    moment = now if now is not None else datetime.now(UTC)
    horizon = timedelta(seconds=SCOPE_STEP_UP_COOLDOWN_SECONDS)
    dated: list[tuple[datetime, str]] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        parsed = _parse_timestamp(value)
        if parsed is None or moment - parsed >= horizon:
            continue
        dated.append((parsed, key))
    dated.sort(key=lambda item: item[0], reverse=True)
    return {name: moment_at.isoformat() for moment_at, name in dated[:MAX_STEP_UP_ENTRIES]}


def describe_scopes(scopes: Iterable[str]) -> str:
    """A bounded, grammar-checked rendering of a scope list for a log line.
    Never used in a message that reaches a person or a model."""
    return " ".join(parse_scope(" ".join(scopes)))


__all__ = [
    "ACCESS_TOKEN_FIELD",
    "AUTH_OAUTH",
    "CHALLENGE_ERROR_CODES",
    "MAX_SCOPE_ENTRIES",
    "OAUTH_CONFIG_KEYS",
    "OAUTH_HTTP_TIMEOUT_SECONDS",
    "OAUTH_ISSUER_KEY",
    "OAUTH_PENDING_SCOPE_KEY",
    "OAUTH_RESOURCE_KEY",
    "OAUTH_SCOPE_KEY",
    "OAUTH_STEP_UPS_KEY",
    "REFRESH_TOKEN_FIELD",
    "SCOPE_STEP_UP_COOLDOWN_SECONDS",
    "TOKEN_TYPE_FIELD",
    "UNKNOWN_CHALLENGE_ERROR",
    "WILDCARD_SCOPES",
    "McpChallenge",
    "McpOAuthConfigError",
    "challenge_from_response",
    "describe_scopes",
    "merge_scope",
    "oauth_auth_headers",
    "parse_scope",
    "reauthorized_headers",
    "reconnect_message",
    "record_scope_step_up",
    "resource_binding",
    "scope_exhausted_message",
    "scope_step_up_message",
    "scope_unnameable_message",
    "validate_oauth_config",
]
