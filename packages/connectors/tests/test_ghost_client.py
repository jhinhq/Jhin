"""Protocol, credential containment, and uncertain effect regression cases."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from jhin_connectors.ghost.client import (
    GhostApiError,
    admin_token,
    ghost_request,
    post_revision,
    validate_admin_url,
)
from jhin_connectors.ghost.schemas import DraftCreateInput, DraftUpdateInput
from jhin_connectors.http_client import MAX_PROVIDER_RESPONSE_BYTES

KEY = "a" * 24 + ":" + "b" * 64


def decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_admin_jwt_uses_hex_decoded_secret_and_short_lifetime():
    header, claims, signature = admin_token(KEY, now=100).split(".")
    assert json.loads(decode(header)) == {"alg": "HS256", "typ": "JWT", "kid": "a" * 24}
    assert json.loads(decode(claims)) == {"iat": 100, "exp": 400, "aud": "/admin/"}
    assert hmac.compare_digest(
        decode(signature),
        hmac.new(bytes.fromhex("b" * 64), f"{header}.{claims}".encode(), hashlib.sha256).digest(),
    )


@pytest.mark.parametrize("key", ["contentkey", "a:b", KEY + "\n", "a" * 24 + ":" + "z" * 64])
def test_invalid_admin_key_never_echoed(key):
    with pytest.raises(GhostApiError) as error:
        admin_token(key)
    assert key not in str(error.value)
    assert not error.value.side_effect_possible


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://blog.example/?key=secret",
        "https://user:key@blog.example",
        "https://blog.example/a/../b",
        "https://blog.example/%2e",
        "http://127.0.0.1:9999",
        "https://blog.example/\\evil",
    ],
)
def test_ambiguous_credential_or_private_url_rejected(url, monkeypatch):
    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "1")
    with pytest.raises(GhostApiError):
        validate_admin_url(url)


def test_self_hosted_admin_paths_normalize_without_guessing(monkeypatch):
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368")
    assert validate_admin_url("http://ghost:2368/blog/ghost/") == "http://ghost:2368/blog"
    assert validate_admin_url("http://ghost:2368/blog/ghost/api/admin/") == "http://ghost:2368/blog"


@pytest.mark.parametrize(
    "status,uncertain", [(401, False), (403, False), (429, False), (500, True), (302, False)]
)
async def test_http_failure_is_not_a_success_or_a_retry(monkeypatch, status, uncertain):
    seen = []

    def reply(request):
        seen.append(request)
        assert request.headers["Authorization"].startswith("Ghost ")
        assert KEY not in str(request.url)
        return httpx.Response(
            status,
            json={"errors": [{"message": KEY}]},
            headers={"Location": "https://evil.example"},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw)
    )
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368")
    with pytest.raises(GhostApiError) as error:
        await ghost_request("http://ghost:2368", KEY, "POST", "posts/", body={"posts": []})
    assert len(seen) == 1
    assert error.value.side_effect_possible is uncertain
    assert KEY not in str(error.value)


def test_draft_schema_has_no_publish_or_status_escape():
    fields = {"connection_id": "a", "title": "Draft", "html": "<p>Hello</p>", "slug": "hello"}
    with pytest.raises(ValidationError):
        DraftCreateInput(**fields, status="published")
    with pytest.raises(ValidationError):
        DraftUpdateInput(**fields, post_id="b", expected_updated_at="now", published_at="now")


def test_review_revision_changes_with_content_even_if_timestamp_is_reused():
    post = {"id": "a" * 24, "title": "One", "updated_at": "today", "html": "one"}
    assert post_revision(post) != post_revision({**post, "html": "two"})
    assert post_revision(post) == post_revision({**post, "reading_time": 2})


def _use_transport(monkeypatch, reply):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw)
    )
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368")


async def test_full_article_page_can_exceed_the_shared_provider_limit(monkeypatch):
    payload = {"posts": [{"id": "a" * 24, "html": "x" * 600_000}]}
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    _use_transport(monkeypatch, reply)
    assert MAX_PROVIDER_RESPONSE_BYTES == 524_288
    result = await ghost_request("http://ghost:2368", KEY, "GET", "posts/")
    assert result == payload
    assert len(seen) == 1


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("method,status,uncertain", [("GET", 200, False), ("POST", 201, True)])
async def test_oversize_ghost_response_remains_bounded_and_truthful(
    monkeypatch, declared, method, status, uncertain
):
    seen = []
    chunks_read = []
    limit = 16 * 1024 * 1024

    class LargeResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(18):
                chunks_read.append(index)
                yield b"x" * (1024 * 1024)

    def reply(request):
        seen.append(request)
        headers = {"Content-Length": str(limit + 1)} if declared else {}
        return httpx.Response(status, headers=headers, stream=LargeResponse())

    _use_transport(monkeypatch, reply)
    with pytest.raises(GhostApiError) as error:
        await ghost_request("http://ghost:2368", KEY, method, "posts/")
    assert error.value.code == "ghost_response_too_large"
    assert "16 MiB" in str(error.value)
    assert error.value.status_code == status
    assert error.value.side_effect_possible is uncertain
    assert len(seen) == 1
    assert len(chunks_read) == (0 if declared else 17)
    assert KEY not in str(error.value)


@pytest.mark.parametrize(
    "method,status,uncertain", [("GET", 200, False), ("POST", 201, True), ("PUT", 200, True)]
)
async def test_success_status_with_invalid_json_is_unconfirmed_not_an_http_error(
    monkeypatch, method, status, uncertain
):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(status, text=f"<html>{KEY}</html>")

    _use_transport(monkeypatch, reply)
    with pytest.raises(GhostApiError) as error:
        await ghost_request("http://ghost:2368", KEY, method, "posts/")
    assert error.value.code == "ghost_bad_response"
    assert "invalid JSON" in str(error.value)
    assert error.value.status_code == status
    assert error.value.side_effect_possible is uncertain
    assert len(seen) == 1
    assert KEY not in str(error.value)


@pytest.mark.parametrize("method,status,uncertain", [("GET", 200, False), ("POST", 201, True)])
async def test_success_status_with_interrupted_body_is_unconfirmed(
    monkeypatch, method, status, uncertain
):
    seen = []

    class InterruptedResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"posts":'
            raise httpx.ReadError(KEY)

    def reply(request):
        seen.append(request)
        return httpx.Response(status, stream=InterruptedResponse())

    _use_transport(monkeypatch, reply)
    with pytest.raises(GhostApiError) as error:
        await ghost_request("http://ghost:2368", KEY, method, "posts/")
    assert error.value.code == "ghost_response_unconfirmed"
    assert error.value.status_code == status
    assert error.value.side_effect_possible is uncertain
    assert len(seen) == 1
    assert KEY not in str(error.value)


def test_a_lone_surrogate_in_a_post_does_not_break_its_revision() -> None:
    # A real Fanclan post carried an unpaired surrogate and crashed the whole
    # archive scan at post 601 with UnicodeEncodeError.
    broken = {"id": "a" * 24, "title": "Split emoji \ud83d", "html": "<p>body</p>"}
    plain = {"id": "a" * 24, "title": "Split emoji", "html": "<p>body</p>"}

    revision = post_revision(broken)

    assert len(revision) == 64
    assert revision != post_revision(plain)


def test_surrogate_handling_leaves_ordinary_revisions_unchanged() -> None:
    # The fix must not invalidate a single already-approved review, so every
    # post that hashed before has to hash to exactly the same value.
    post = {"id": "b" * 24, "title": "Ordinary — em dash, emoji 🎉", "html": "<p>x</p>"}

    expected = hashlib.sha256(
        json.dumps(
            {"id": post["id"], "title": post["title"], "html": post["html"]},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert post_revision(post) == expected
