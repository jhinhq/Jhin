"""Fetching one published catalog release: bounded, timed, and verified.

The upstream catalog ships as a GitHub Release per build: a ``SHA256SUMS``
manifest and a ``jhin-catalog-<tag>-data.tar.gz`` archive. This module is
the only place in Jhin that talks to that host, and it keeps the posture of
the connector HTTP clients (:mod:`jhin_connectors.http_client`): the
declared ``Content-Length`` *and* the streamed byte count are both checked
against a cap, every request carries an explicit timeout, and no error
crossing this boundary carries a URL, a header, or a byte of upstream text.

Redirects are refused, with exactly one deliberate exception. GitHub serves
release assets as a ``302`` into its object store, so a single hop is
followed when — and only when — the ``Location`` is an absolute ``https``
URL whose host is in :data:`ASSET_REDIRECT_HOSTS`. The hop is issued as a
freshly constructed request rather than a copy of the first one, so the
client's default headers — an ``Authorization`` among them — never travel to
the object store, and the byte cap is applied again on the new stream. Any
other 3xx, and any second hop, raises.

Integrity is settled here, before the loader ever opens a database session:
:func:`download_verified_archive` computes the digest over the streamed
bytes, compares it with the manifest, and raises rather than returning when
they differ. Nothing downstream is reachable with unverified bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

import httpx

from jhin_catalog_sync.types import (
    CatalogFetchError,
    CatalogFormatError,
    CatalogIntegrityError,
)
from jhin_tools.sanitize import strict_json_loads

# The upstream repository is configurable per deployment: a fork, a mirror,
# or an internally published index all work the same way.
SOURCE_REPO_ENV: Final = "CATALOG_SOURCE_REPO"
DEFAULT_SOURCE_REPO: Final = "jhinhq/jhin-catalog"

GITHUB_API_HOST: str = "api.github.com"
# The object stores a GitHub release asset legitimately redirects into. This
# list is the entire redirect policy of this module.
ASSET_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
MAX_RELEASE_JSON_BYTES: int = 262_144
MAX_SUMS_BYTES: int = 4_096
MAX_ARCHIVE_BYTES: int = 33_554_432  # 32 MiB
CONNECT_TIMEOUT_SECONDS: float = 10.0
READ_TIMEOUT_SECONDS: float = 60.0
WRITE_TIMEOUT_SECONDS: float = 10.0
POOL_TIMEOUT_SECONDS: float = 10.0
# Wall clock for one whole sync, enforced by the caller around the network
# phase (:func:`jhin_catalog_sync.cli.sync_once`).
TOTAL_TIMEOUT_SECONDS: float = 300.0

SUMS_ASSET_NAME: Final = "SHA256SUMS"
USER_AGENT: Final = "jhin-catalog-sync"
# Exactly one asset hop, and never more.
MAX_REDIRECT_HOPS: Final = 1
# A release with more assets than this is not one of ours.
MAX_RELEASE_ASSETS: Final = 100

# Hosts a release asset URL itself may name. The release JSON is upstream
# data, so the URLs in it get the same treatment as the redirect target.
_ASSET_HOSTS: Final = frozenset({"github.com", GITHUB_API_HOST}) | ASSET_REDIRECT_HOSTS

_REPO_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_TAG_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ASSET_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$")
_SUMS_LINE_RE: Final = re.compile(r"^([a-f0-9]{64})\s+\*?(\S{1,120})$")
_PUBLISHED_AT_RE: Final = re.compile(r"^[0-9A-Za-z:.+-]{1,40}$")
# The asset URL in the release JSON is short (~74 chars), but the location
# GitHub redirects it to is not: it carries a signed SAS query and a JWT, and
# was measured at 899 characters against ``release-assets.githubusercontent``
# on 2026-08-29. A 512 cap rejected every real release. This is a sanity
# bound on unbounded upstream input, not a policy — the host allowlist in
# ``ASSET_REDIRECT_HOSTS`` is what actually decides where a hop may go.
_MAX_ASSET_URL_CHARS: Final = 4_096


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    """One resolved release: its tag and the assets it publishes."""

    tag: str
    assets: Mapping[str, str]  # asset name -> download URL
    published_at: str


def request_timeout() -> httpx.Timeout:
    """The explicit per-phase timeout every request in this module carries."""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=WRITE_TIMEOUT_SECONDS,
        pool=POOL_TIMEOUT_SECONDS,
    )


def new_client() -> httpx.AsyncClient:
    """A client with the house posture: explicit timeouts, no redirects.

    Redirect handling belongs to :func:`fetch_bounded`, which knows the one
    hop that is allowed; leaving it to the transport would follow anything.
    """
    return httpx.AsyncClient(
        timeout=request_timeout(),
        follow_redirects=False,
        headers={
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "user-agent": USER_AGENT,
        },
    )


def data_asset_name(tag: str) -> str:
    """The data-archive asset name a release with this tag publishes."""
    return f"jhin-catalog-{tag}-data.tar.gz"


def _https_url(raw: object, *, failure: str) -> httpx.URL:
    """Parse one absolute, credential-free ``https`` URL, or raise."""
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_ASSET_URL_CHARS:
        raise CatalogFetchError(failure)
    try:
        parsed = httpx.URL(raw)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise CatalogFetchError(failure) from None
    if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password:
        raise CatalogFetchError(failure)
    return parsed


def _is_release_asset_url(raw: str) -> bool:
    """Whether an asset URL from the release JSON names a host we expect."""
    try:
        parsed = _https_url(raw, failure="unusable asset URL")
    except CatalogFetchError:
        return False
    return parsed.host.lower() in _ASSET_HOSTS


def _hop_url(location: object) -> httpx.URL:
    """The one redirect target this module will follow."""
    parsed = _https_url(location, failure="the catalog asset redirected to an unusable location")
    if parsed.host.lower() not in ASSET_REDIRECT_HOSTS:
        raise CatalogFetchError("the catalog asset redirected to an unexpected host")
    return parsed


def _declared_content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _read_bounded(response: httpx.Response, *, max_bytes: int) -> tuple[bytes, str]:
    """Drain one 2xx response under the cap, hashing as the bytes arrive."""
    status = response.status_code
    if not 200 <= status < 300:
        raise CatalogFetchError(f"the catalog asset request failed with status {status}")
    declared = _declared_content_length(response)
    if declared is not None and declared > max_bytes:
        raise CatalogFetchError("the catalog asset is larger than the allowed size")

    digest = hashlib.sha256()
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise CatalogFetchError("the catalog asset is larger than the allowed size")
            body.extend(chunk)
            digest.update(chunk)
    except CatalogFetchError:
        raise
    except Exception:
        raise CatalogFetchError("the catalog asset could not be read") from None
    return bytes(body), digest.hexdigest()


async def fetch_bounded(
    url: str, *, max_bytes: int, client: httpx.AsyncClient
) -> tuple[bytes, str]:
    """Stream one asset under a hard cap; return its bytes and sha256 digest.

    The digest is computed over the bytes as they stream past, never over a
    re-read, so what was hashed is exactly what is returned. At most one
    redirect hop is followed, into :data:`ASSET_REDIRECT_HOSTS` only, without
    the client's default headers and with the cap applied again.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    target = _https_url(url, failure="the catalog asset URL is not an absolute https URL")
    request = client.build_request("GET", target)
    hops = 0
    while True:
        auth: Any = httpx.USE_CLIENT_DEFAULT if hops == 0 else None
        try:
            response = await client.send(request, stream=True, follow_redirects=False, auth=auth)
        except Exception:
            raise CatalogFetchError("the catalog asset request failed") from None
        try:
            if not 300 <= response.status_code < 400:
                return await _read_bounded(response, max_bytes=max_bytes)
            if hops >= MAX_REDIRECT_HOPS:
                raise CatalogFetchError("the catalog asset redirected more than once")
            hop = _hop_url(response.headers.get("location"))
        finally:
            await response.aclose()
        hops += 1
        # Built from scratch, not copied: the object store must not receive
        # the Authorization header the API request may have carried.
        request = httpx.Request("GET", hop, headers={"accept": "*/*", "user-agent": USER_AGENT})


def _release_from_json(raw: bytes) -> ReleaseRef:
    """Project the release JSON onto the two things this sync needs."""
    try:
        payload = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CatalogFetchError("the catalog release metadata was not readable JSON") from None
    if not isinstance(payload, dict):
        raise CatalogFetchError("the catalog release metadata was not an object")

    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
        raise CatalogFetchError("the catalog release metadata carries no usable tag")

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_RELEASE_ASSETS:
        raise CatalogFetchError("the catalog release metadata carries no usable asset list")
    assets: dict[str, str] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        href = item.get("browser_download_url")
        if not isinstance(name, str) or not _ASSET_NAME_RE.fullmatch(name):
            continue
        if not isinstance(href, str) or not _is_release_asset_url(href):
            continue
        # First listing of a name wins; a release that lists one twice is not
        # allowed to make the later entry the effective one.
        assets.setdefault(name, href)

    published = payload.get("published_at")
    return ReleaseRef(
        tag=tag,
        assets=MappingProxyType(assets),
        published_at=(
            published
            if isinstance(published, str) and _PUBLISHED_AT_RE.fullmatch(published)
            else ""
        ),
    )


async def resolve_release(
    *, repo: str, tag: str | None = None, client: httpx.AsyncClient | None = None
) -> ReleaseRef:
    """Resolve the latest release of ``repo``, or the one carrying ``tag``.

    ``repo`` and ``tag`` are pattern-checked before they reach a URL, so
    neither can walk out of the ``/repos/{owner}/{repo}/releases`` path.
    """
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise CatalogFetchError("the catalog source repository is not a valid owner/repo")
    if tag is not None and not _TAG_RE.fullmatch(tag):
        raise CatalogFetchError("the requested catalog release tag is not a valid tag")

    path = "releases/latest" if tag is None else f"releases/tags/{tag}"
    url = f"https://{GITHUB_API_HOST}/repos/{repo}/{path}"
    if client is not None:
        raw, _ = await fetch_bounded(url, max_bytes=MAX_RELEASE_JSON_BYTES, client=client)
        return _release_from_json(raw)
    async with new_client() as owned:
        raw, _ = await fetch_bounded(url, max_bytes=MAX_RELEASE_JSON_BYTES, client=owned)
    return _release_from_json(raw)


def parse_sha256sums(raw: bytes) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` manifest into ``{basename: hexdigest}``.

    Strict on purpose: this file is the integrity authority, so a line it
    cannot read is a failure rather than a line to skip. Blank lines are the
    one tolerance — every generator emits a trailing newline.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CatalogFormatError("the catalog SHA256SUMS manifest is not UTF-8") from None

    digests: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _SUMS_LINE_RE.fullmatch(stripped)
        if match is None:
            raise CatalogFormatError("the catalog SHA256SUMS manifest has an unreadable line")
        name = match.group(2).rpartition("/")[2]
        if not name:
            raise CatalogFormatError("the catalog SHA256SUMS manifest has an unreadable line")
        if name in digests:
            raise CatalogFormatError("the catalog SHA256SUMS manifest names a file twice")
        digests[name] = match.group(1)
    return digests


async def download_verified_archive(
    release: ReleaseRef, *, client: httpx.AsyncClient
) -> tuple[bytes, str]:
    """Download the data archive and prove it against the published digest.

    The manifest is fetched first and the archive second, so the expected
    digest is fixed before the bytes it describes exist in this process.
    Returns ``(tar_gz_bytes, sha256)``; raises rather than returning bytes
    that did not match.
    """
    sums_url = release.assets.get(SUMS_ASSET_NAME)
    if sums_url is None:
        raise CatalogIntegrityError("the catalog release publishes no SHA256SUMS manifest")
    archive_name = data_asset_name(release.tag)
    archive_url = release.assets.get(archive_name)
    if archive_url is None:
        raise CatalogIntegrityError("the catalog release publishes no data archive")

    sums_raw, _ = await fetch_bounded(sums_url, max_bytes=MAX_SUMS_BYTES, client=client)
    expected = parse_sha256sums(sums_raw).get(archive_name)
    if expected is None:
        raise CatalogIntegrityError(
            "the catalog SHA256SUMS manifest does not cover the data archive"
        )

    blob, digest = await fetch_bounded(archive_url, max_bytes=MAX_ARCHIVE_BYTES, client=client)
    if not hmac.compare_digest(digest, expected):
        raise CatalogIntegrityError("the catalog data archive does not match its published digest")
    return blob, digest


__all__ = [
    "ASSET_REDIRECT_HOSTS",
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_SOURCE_REPO",
    "GITHUB_API_HOST",
    "MAX_ARCHIVE_BYTES",
    "MAX_RELEASE_JSON_BYTES",
    "MAX_SUMS_BYTES",
    "READ_TIMEOUT_SECONDS",
    "SOURCE_REPO_ENV",
    "SUMS_ASSET_NAME",
    "TOTAL_TIMEOUT_SECONDS",
    "ReleaseRef",
    "data_asset_name",
    "download_verified_archive",
    "fetch_bounded",
    "new_client",
    "parse_sha256sums",
    "request_timeout",
    "resolve_release",
]
