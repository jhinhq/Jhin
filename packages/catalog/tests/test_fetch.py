"""Fetching a release without letting the release decide anything.

Three properties are load-bearing here and each has its own tests:

* **bounds** — every response is capped twice, once on the declared
  ``Content-Length`` and once on the bytes that actually arrive, because a
  server that lies about its length is exactly the server we are guarding
  against;
* **redirects** — the repo-wide rule is "never follow one". GitHub serves
  release assets only via a 302 into its object store, so there is exactly one
  narrow exception: one hop, https only, an allow-listed host, and the
  ``Authorization`` header left behind;
* **integrity** — the archive digest is compared against ``SHA256SUMS`` before
  the bytes are handed to anything that can write to the database.

Every failure message is also asserted to be free of upstream text: a fetch
error is a thing an operator reads in a log, and a crafted release body must
not be able to write that log line.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from jhin_catalog_sync.fetch import (
    ASSET_REDIRECT_HOSTS,
    CONNECT_TIMEOUT_SECONDS,
    GITHUB_API_HOST,
    MAX_ARCHIVE_BYTES,
    MAX_RELEASE_JSON_BYTES,
    MAX_SUMS_BYTES,
    READ_TIMEOUT_SECONDS,
    TOTAL_TIMEOUT_SECONDS,
    ReleaseRef,
    download_verified_archive,
    fetch_bounded,
    parse_sha256sums,
    resolve_release,
)
from jhin_catalog_sync.types import CatalogFetchError, CatalogFormatError, CatalogIntegrityError

REPO = "jhinhq/jhin-catalog"
TAG = "2026.08.28"
ARCHIVE_NAME = f"jhin-catalog-{TAG}-data.tar.gz"
ARCHIVE_URL = f"https://api.github.com/repos/{REPO}/releases/assets/1"
SUMS_URL = f"https://api.github.com/repos/{REPO}/releases/assets/2"
OBJECT_STORE = "https://objects.githubusercontent.com/jhin-catalog/data.tar.gz"
ARCHIVE_BODY = b"pretend this is a gzipped tarball" * 32
ARCHIVE_SHA = hashlib.sha256(ARCHIVE_BODY).hexdigest()

# Planted in every upstream body: if it ever shows up in an exception message,
# a hostile release can write our logs.
MARKER = "CANARY-upstream-prose-must-not-surface"


@dataclass(slots=True)
class Recorder:
    """Every request the client actually made, in order."""

    requests: list[httpx.Request] = field(default_factory=list)
    body_started: bool = False

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def _release_json(*, tag: str = TAG, extra_assets: Iterable[dict[str, str]] = ()) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "name": f"Catalog {tag} {MARKER}",
            "body": MARKER,
            "published_at": "2026-08-28T04:00:00Z",
            "assets": [
                {"name": ARCHIVE_NAME, "browser_download_url": ARCHIVE_URL, "url": ARCHIVE_URL},
                {"name": "SHA256SUMS", "browser_download_url": SUMS_URL, "url": SUMS_URL},
                *extra_assets,
            ],
        }
    ).encode("utf-8")


def _sums(digest: str = ARCHIVE_SHA, name: str = ARCHIVE_NAME) -> bytes:
    return f"{digest}  {name}\n".encode()


async def _stream(
    chunks: Iterable[bytes], recorder: Recorder | None = None
) -> AsyncIterator[bytes]:
    for chunk in chunks:
        if recorder is not None:
            recorder.body_started = True
        yield chunk


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    recorder: Recorder,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    def record(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(record),
        headers=headers or {},
        follow_redirects=False,
    )


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_the_bounds_and_timeouts_are_the_declared_ones() -> None:
    assert GITHUB_API_HOST == "api.github.com"
    assert (
        frozenset(
            {
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
                "github-releases.githubusercontent.com",
            }
        )
        == ASSET_REDIRECT_HOSTS
    )
    assert (MAX_RELEASE_JSON_BYTES, MAX_SUMS_BYTES, MAX_ARCHIVE_BYTES) == (
        262_144,
        4_096,
        33_554_432,
    )
    assert (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS) == (
        10.0,
        60.0,
        300.0,
    )


# --------------------------------------------------------------------------
# resolve_release
# --------------------------------------------------------------------------


async def test_the_latest_release_is_read_from_the_api_host() -> None:
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == GITHUB_API_HOST
        return httpx.Response(200, content=_release_json())

    async with _client(handler, recorder) as client:
        release = await resolve_release(repo=REPO, client=client)

    assert isinstance(release, ReleaseRef)
    assert release.tag == TAG
    assert release.published_at == "2026-08-28T04:00:00Z"
    assert release.assets[ARCHIVE_NAME] == ARCHIVE_URL
    assert release.assets["SHA256SUMS"] == SUMS_URL
    assert recorder.urls == [f"https://api.github.com/repos/{REPO}/releases/latest"]


async def test_a_named_tag_is_read_from_the_tag_endpoint() -> None:
    recorder = Recorder()
    async with _client(lambda _r: httpx.Response(200, content=_release_json()), recorder) as client:
        await resolve_release(repo=REPO, tag=TAG, client=client)

    assert recorder.urls == [f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"]


@pytest.mark.parametrize("status", [301, 302, 307, 404, 403, 500])
async def test_a_release_lookup_that_is_not_a_200_fails_cleanly(status: int) -> None:
    recorder = Recorder()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"location": "https://evil.example.com/release.json"},
            content=json.dumps({"message": MARKER}).encode(),
        )

    async with _client(handler, recorder) as client:
        with pytest.raises(CatalogFetchError) as caught:
            await resolve_release(repo=REPO, client=client)

    assert MARKER not in str(caught.value)
    assert len(recorder.requests) == 1, "a redirect must not be followed"


async def test_an_oversized_release_document_is_refused() -> None:
    recorder = Recorder()
    padded = json.dumps({"tag_name": TAG, "assets": [], "body": "p" * MAX_RELEASE_JSON_BYTES})

    async with _client(lambda _r: httpx.Response(200, content=padded.encode()), recorder) as client:
        with pytest.raises(CatalogFetchError):
            await resolve_release(repo=REPO, client=client)


async def test_a_release_document_that_is_not_json_is_refused() -> None:
    recorder = Recorder()
    async with _client(
        lambda _r: httpx.Response(200, content=b"<html>" + MARKER.encode()), recorder
    ) as client:
        with pytest.raises(CatalogFetchError) as caught:
            await resolve_release(repo=REPO, client=client)
    assert MARKER not in str(caught.value)


# --------------------------------------------------------------------------
# fetch_bounded — redirects
# --------------------------------------------------------------------------


async def test_one_hop_to_an_allow_listed_host_is_followed_without_the_token() -> None:
    """The single documented exception to the no-redirect rule. The credential
    must not travel: the object store is a different party."""
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GITHUB_API_HOST:
            return httpx.Response(302, headers={"location": OBJECT_STORE})
        return httpx.Response(200, content=ARCHIVE_BODY)

    async with _client(
        handler, recorder, headers={"authorization": "Bearer gh-token-do-not-forward"}
    ) as client:
        body, digest = await fetch_bounded(ARCHIVE_URL, max_bytes=MAX_ARCHIVE_BYTES, client=client)

    assert body == ARCHIVE_BODY
    assert digest == ARCHIVE_SHA
    assert len(recorder.requests) == 2
    hop = recorder.requests[1]
    assert str(hop.url) == OBJECT_STORE
    assert "authorization" not in {name.lower() for name in hop.headers}


async def test_a_real_length_signed_redirect_is_followed() -> None:
    """A real release hop is ~900 characters, not the short one above.

    GitHub signs the object-store location with a SAS query and a JWT. A
    512-character cap rejected every genuine release with "the catalog asset
    redirected to an unusable location" while every fixture here passed,
    because the fixtures were short. Measured at 899 characters against
    ``release-assets.githubusercontent.com`` on 2026-08-29.
    """
    signed = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/"
        "1350269219/8d2ae029-4270-4a83-83d8-fbaa169b5e70?"
        + "&".join(f"k{index}=" + "x" * 40 for index in range(20))
    )
    assert len(signed) > 800, "the point of this test is a long URL"
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GITHUB_API_HOST:
            return httpx.Response(302, headers={"location": signed})
        return httpx.Response(200, content=ARCHIVE_BODY)

    async with _client(handler, recorder) as client:
        body, digest = await fetch_bounded(ARCHIVE_URL, max_bytes=MAX_ARCHIVE_BYTES, client=client)

    assert body == ARCHIVE_BODY
    assert digest == ARCHIVE_SHA
    assert str(recorder.requests[1].url) == signed


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/asset",
        "https://objects.githubusercontent.com.evil.example.com/asset",
        "http://objects.githubusercontent.com/asset",
        "/relative/asset",
        "https://api.github.com/another",
        "https://release-assets.githubusercontent.com/a?q=" + "x" * 5000,
    ],
    ids=[
        "other-host",
        "suffix-lookalike",
        "plain-http",
        "relative",
        "api-host",
        "absurdly-long",
    ],
)
async def test_any_other_redirect_target_is_refused(location: str) -> None:
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if len(recorder.requests) == 1:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=ARCHIVE_BODY)

    async with _client(handler, recorder) as client:
        with pytest.raises(CatalogFetchError):
            await fetch_bounded(ARCHIVE_URL, max_bytes=MAX_ARCHIVE_BYTES, client=client)


async def test_a_second_hop_is_refused() -> None:
    """One hop is a documented GitHub behaviour; a chain is a redirect loop
    somebody built to walk us somewhere."""
    recorder = Recorder()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": OBJECT_STORE})

    async with _client(handler, recorder) as client:
        with pytest.raises(CatalogFetchError):
            await fetch_bounded(ARCHIVE_URL, max_bytes=MAX_ARCHIVE_BYTES, client=client)

    assert len(recorder.requests) <= 2


# --------------------------------------------------------------------------
# fetch_bounded — bounds and digest
# --------------------------------------------------------------------------


async def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read() -> None:
    recorder = Recorder()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_ARCHIVE_BYTES + 1)},
            content=_stream([b"x" * 1024], recorder),
        )

    async with _client(handler, recorder) as client:
        with pytest.raises(CatalogFetchError):
            await fetch_bounded(ARCHIVE_URL, max_bytes=MAX_ARCHIVE_BYTES, client=client)

    assert recorder.body_started is False, "the cap must be checked on the header first"


async def test_a_body_that_outgrows_the_cap_mid_stream_is_refused() -> None:
    """A server may simply not send ``Content-Length``, or lie about it. The
    streamed count is the check that cannot be talked out of."""
    recorder = Recorder()
    cap = 4_096

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream([b"z" * 1_024] * 8, recorder))

    async with _client(handler, recorder) as client:
        with pytest.raises(CatalogFetchError):
            await fetch_bounded(ARCHIVE_URL, max_bytes=cap, client=client)

    assert recorder.body_started is True


async def test_a_body_exactly_at_the_cap_is_allowed() -> None:
    recorder = Recorder()
    body = b"e" * 1_024

    async with _client(lambda _r: httpx.Response(200, content=body), recorder) as client:
        fetched, digest = await fetch_bounded(ARCHIVE_URL, max_bytes=1_024, client=client)

    assert fetched == body
    assert digest == hashlib.sha256(body).hexdigest()


async def test_the_digest_is_computed_over_the_streamed_bytes() -> None:
    """Not over a re-read: a digest taken from a second request is a digest of
    whatever the server felt like sending the second time."""
    recorder = Recorder()
    chunks = [b"one", b"two", b"three"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream(chunks))

    async with _client(handler, recorder) as client:
        body, digest = await fetch_bounded(ARCHIVE_URL, max_bytes=1_024, client=client)

    assert body == b"".join(chunks)
    assert digest == hashlib.sha256(b"".join(chunks)).hexdigest()
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_an_error_status_on_an_asset_is_a_fetch_failure(status: int) -> None:
    recorder = Recorder()
    async with _client(
        lambda _r: httpx.Response(status, content=MARKER.encode()), recorder
    ) as client:
        with pytest.raises(CatalogFetchError) as caught:
            await fetch_bounded(ARCHIVE_URL, max_bytes=1_024, client=client)
    assert MARKER not in str(caught.value)


# --------------------------------------------------------------------------
# parse_sha256sums
# --------------------------------------------------------------------------


def test_sums_parse_in_both_gnu_forms() -> None:
    parsed = parse_sha256sums(f"{'a' * 64}  {ARCHIVE_NAME}\n{'b' * 64} *other.tar.gz\n".encode())
    assert parsed == {ARCHIVE_NAME: "a" * 64, "other.tar.gz": "b" * 64}
    assert parse_sha256sums(b"") == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"not a checksum line\n",
        b"deadbeef  short-digest.tar.gz\n",
        ("A" * 64 + "  upper.tar.gz\n").encode(),
        ("a" * 64 + "  name with spaces.tar.gz\n").encode(),
        ("a" * 64 + "  " + "n" * 200 + "\n").encode(),
        ("a" * 63 + "  short.tar.gz\n").encode(),
        b"\xff\xfe binary\n",
    ],
    ids=["prose", "short-digest", "uppercase", "spaces", "long-name", "63-hex", "not-utf8"],
)
def test_a_malformed_sums_line_is_a_format_error(raw: bytes) -> None:
    with pytest.raises(CatalogFormatError):
        parse_sha256sums(raw)


# --------------------------------------------------------------------------
# download_verified_archive
# --------------------------------------------------------------------------


def _release(**overrides: Any) -> ReleaseRef:
    assets = {ARCHIVE_NAME: ARCHIVE_URL, "SHA256SUMS": SUMS_URL}
    return ReleaseRef(
        tag=overrides.get("tag", TAG),
        assets=overrides.get("assets", assets),
        published_at="2026-08-28T04:00:00Z",
    )


def _asset_handler(
    *, sums: bytes = b"", archive: bytes = ARCHIVE_BODY
) -> Callable[[httpx.Request], httpx.Response]:
    body = sums or _sums()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SUMS_URL:
            return httpx.Response(200, content=body)
        if str(request.url) == ARCHIVE_URL:
            return httpx.Response(200, content=archive)
        return httpx.Response(404, content=MARKER.encode())

    return handler


async def test_a_matching_digest_returns_the_archive_and_its_sha() -> None:
    recorder = Recorder()
    async with _client(_asset_handler(), recorder) as client:
        blob, digest = await download_verified_archive(_release(), client=client)

    assert blob == ARCHIVE_BODY
    assert digest == ARCHIVE_SHA
    assert SUMS_URL in recorder.urls and ARCHIVE_URL in recorder.urls
    assert recorder.urls.index(SUMS_URL) < recorder.urls.index(ARCHIVE_URL), (
        "the expected digest has to be known before the archive is trusted"
    )


async def test_a_digest_mismatch_is_an_integrity_failure() -> None:
    recorder = Recorder()
    async with _client(_asset_handler(archive=ARCHIVE_BODY + b"tampered"), recorder) as client:
        with pytest.raises(CatalogIntegrityError) as caught:
            await download_verified_archive(_release(), client=client)

    message = str(caught.value)
    assert MARKER not in message
    assert "tampered" not in message


async def test_a_sums_file_with_no_line_for_the_archive_is_an_integrity_failure() -> None:
    recorder = Recorder()
    async with _client(
        _asset_handler(sums=_sums(name="some-other-release.tar.gz")), recorder
    ) as client:
        with pytest.raises(CatalogIntegrityError):
            await download_verified_archive(_release(), client=client)


async def test_a_release_missing_the_archive_never_downloads_anything() -> None:
    """A release that publishes a manifest but no archive is refused before a
    single asset byte is fetched."""
    recorder = Recorder()
    async with _client(_asset_handler(), recorder) as client:
        with pytest.raises(CatalogIntegrityError):
            await download_verified_archive(
                _release(assets={"SHA256SUMS": SUMS_URL}), client=client
            )

    assert recorder.requests == []


async def test_a_release_missing_the_manifest_never_downloads_anything() -> None:
    recorder = Recorder()
    async with _client(_asset_handler(), recorder) as client:
        with pytest.raises(CatalogIntegrityError):
            await download_verified_archive(
                _release(assets={ARCHIVE_NAME: ARCHIVE_URL}), client=client
            )

    assert recorder.requests == []


async def test_a_malformed_manifest_is_a_format_error_not_a_silent_skip() -> None:
    recorder = Recorder()
    async with _client(_asset_handler(sums=b"this is not a manifest\n"), recorder) as client:
        with pytest.raises(CatalogFormatError):
            await download_verified_archive(_release(), client=client)

    assert ARCHIVE_URL not in recorder.urls
