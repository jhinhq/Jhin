"""Ghost Admin API: confirmed destinations, short-lived JWTs, bounded requests.

An Admin integration key is NOT a Content API key. Only this worker boundary
sees it. Never redirect an authenticated request or retry an uncertain write.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_public_http_url
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_secrets.redaction import get_redactor
from jhin_tools.errors import ToolExecutionError

_ADMIN_KEY = re.compile(r"^[a-fA-F0-9]{24}:[a-fA-F0-9]{64}$")
_POST_ID = re.compile(r"^[a-fA-F0-9]{24}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
# Published article pages include complete rich-text bodies. Keep their bound
# separate from the smaller shared provider default; never truncate a page.
MAX_GHOST_RESPONSE_BYTES = 16 * 1024 * 1024


class GhostApiError(ToolExecutionError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        mutation: bool = False,
        code: str = "ghost_request_failed",
    ) -> None:
        super().__init__(
            message,
            code=code,
            hint=message,
            # A successful status without a readable confirmation may already
            # have changed Ghost. Treat it like a lost response, never a safe retry.
            side_effect_possible=mutation
            and (status_code is None or 200 <= status_code < 300 or status_code >= 500),
        )
        self.status_code = status_code


def validate_admin_url(value: str) -> str:
    """Accept the actual supplied site/admin URL, never invent a subdomain.

    Ghost supports subdirectory installs. Normalize only documented suffixes
    and preserve the install prefix. Plain HTTP/LAN needs the operator's
    existing exact-origin allowlist, just like the other native connectors.
    """
    if not isinstance(value, str) or any(
        ord(char) <= 32 or ord(char) == 127 or char in "\\%?#" for char in value
    ):
        raise GhostApiError("Supply the actual Ghost Admin URL", code="ghost_url_required")
    try:
        safe = validate_public_http_url(value, kind="Ghost Admin URL")
    except (EndpointPolicyError, ValueError):
        raise GhostApiError(
            "Ghost Admin URL is invalid or outside the configured outbound policy",
            code="ghost_target_not_allowed",
        ) from None
    parsed = urlsplit(safe)
    path = parsed.path.rstrip("/")
    for suffix in ("/ghost/api/admin", "/ghost"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if any(not _PATH_SEGMENT.fullmatch(segment) for segment in path.split("/") if segment):
        raise GhostApiError("Ghost install path is invalid", code="ghost_target_not_allowed")
    if "//" in path:
        raise GhostApiError("Ghost install path is invalid", code="ghost_target_not_allowed")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def admin_origin(admin_url: str) -> str:
    parsed = urlsplit(validate_admin_url(admin_url))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def admin_token(admin_key: str, *, now: int | None = None) -> str:
    if not _ADMIN_KEY.fullmatch(admin_key):
        raise GhostApiError(
            "Ghost needs an Admin integration key, not a Content API key",
            code="ghost_admin_key_invalid",
        )
    get_redactor().register(admin_key)
    key_id, secret = admin_key.split(":", 1)
    get_redactor().register(secret)
    issued_at = int(time.time()) if now is None else now
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    claims = {"iat": issued_at, "exp": issued_at + 300, "aud": "/admin/"}
    unsigned = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    token = (
        unsigned
        + "."
        + _b64(
            hmac.new(
                bytes.fromhex(secret),
                unsigned.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
    )
    get_redactor().register(token)
    return token


def post_path(post_id: str) -> str:
    if not _POST_ID.fullmatch(post_id):
        raise GhostApiError("Ghost post ID is invalid", code="ghost_post_id_invalid")
    return f"posts/{post_id}/"


async def ghost_request(
    admin_url: str,
    admin_key: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send exactly one typed Admin request. Transport failures never retry."""
    base = validate_admin_url(admin_url)
    if method not in {"GET", "POST", "PUT"} or not (
        path in {"posts/", "site/"} or re.fullmatch(r"posts/[a-fA-F0-9]{24}/", path)
    ):
        raise GhostApiError("Ghost operation is invalid", code="ghost_operation_invalid")
    mutation = method != "GET"
    token = admin_token(admin_key)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            request = client.build_request(
                method,
                f"{base}/ghost/api/admin/{path}",
                headers={
                    "Authorization": f"Ghost {token}",
                    "Accept-Version": "v5.0",
                    "Content-Type": "application/json",
                    "User-Agent": "jhin-ghost",
                },
                json=body,
                params=params,
            )
            payload = await send_bounded_json(
                client, request, max_response_bytes=MAX_GHOST_RESPONSE_BYTES
            )
    except ProviderHTTPError as exc:
        status = exc.status_code
        code = f"ghost_http_{status}" if status else "ghost_request_failed"
        if status is not None and 200 <= status < 300:
            # These messages are stable, credential-free reasons produced by
            # our shared parser, not text copied from a provider response.
            if str(exc) == "Provider response is too large":
                message = "Ghost response exceeded the 16 MiB limit. " + (
                    "The write may have succeeded; inspect Ghost before retrying."
                    if mutation
                    else "Reduce the requested page size; no content was truncated."
                )
                code = "ghost_response_too_large"
            elif str(exc) == "Provider returned an invalid JSON response":
                message = "Ghost returned invalid JSON despite a successful HTTP status."
                code = "ghost_bad_response"
            else:
                message = "Ghost response could not be fully read and confirmed."
                code = "ghost_response_unconfirmed"
        elif status in {401, 403}:
            message = (
                "Ghost refused authentication. Confirm the Admin URL and replace the Admin key; "
                "do not repeat this request unchanged."
            )
        elif status == 429:
            message = (
                "Ghost rate limited the request. Wait before retrying; "
                "do not switch URLs or authentication schemes."
            )
        elif status == 409:
            message = "Ghost post changed. Read the latest revision and obtain a new review."
        else:
            message = (
                f"Ghost request failed (HTTP {status})"
                if status
                else "Ghost request could not be confirmed"
            )
        raise GhostApiError(
            message,
            status_code=status,
            mutation=mutation,
            code=code,
        ) from None
    if not isinstance(payload, dict):
        raise GhostApiError(
            "Ghost returned an invalid response", mutation=mutation, code="ghost_bad_response"
        )
    return payload


def one_post(payload: dict[str, Any], *, mutation: bool = False) -> dict[str, Any]:
    posts = payload.get("posts")
    if not isinstance(posts, list) or len(posts) != 1 or not isinstance(posts[0], dict):
        raise GhostApiError(
            "Ghost did not confirm the post", mutation=mutation, code="ghost_bad_response"
        )
    post = posts[0]
    if not _POST_ID.fullmatch(str(post.get("id", ""))) or not post.get("updated_at"):
        raise GhostApiError(
            "Ghost did not return a versioned post", mutation=mutation, code="ghost_bad_response"
        )
    return post


def post_revision(post: dict[str, Any]) -> str:
    """Bind review to all returned writable content, not just the timestamp.

    Volatile computed/API fields are excluded; all content and routing fields
    relevant to what is published remain. updated_at catches out-of-band edits.
    """
    fields = (
        "id",
        "updated_at",
        "title",
        "slug",
        "status",
        "lexical",
        "html",
        "feature_image",
        "feature_image_alt",
        "feature_image_caption",
        "custom_excerpt",
        "excerpt",
        "visibility",
        "tags",
        "authors",
        "meta_title",
        "meta_description",
        "og_image",
        "og_title",
        "og_description",
        "twitter_image",
        "twitter_title",
        "twitter_description",
        "codeinjection_head",
        "codeinjection_foot",
        "canonical_url",
        "custom_template",
        "featured",
        "email_subject",
        "email_only",
        "newsletter",
    )
    content = {key: post[key] for key in fields if key in post}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            # A published post can carry an unpaired surrogate -- an emoji split
            # by whatever wrote it -- and plain UTF-8 refuses those, which
            # crashed a whole archive scan at the one post that had one.
            # ``surrogatepass`` is the encoding that keeps every other post's
            # revision byte-identical, so no existing review is invalidated,
            # and it stays injective, so two posts cannot collide here.
            errors="surrogatepass"
        )
    ).hexdigest()
