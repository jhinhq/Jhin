"""Opening the verified catalog archive, in memory and under limits.

A tarball from the internet is an attack surface with a long history:
member names that escape the extraction root, symlinks that point at
``/etc``, one member that inflates to fill the disk, and a million members
that fill the inode table. Jhin sidesteps the whole class by never
extracting. :func:`open_archive` reads members one at a time through
``extractfile`` into bounded ``bytes`` and returns them as a mapping; no
path is ever joined, opened, or created.

Four limits do the rest: a member must be a regular file whose name
fullmatches :data:`MEMBER_RE`, no member may exceed
:data:`MAX_MEMBER_BYTES`, the running uncompressed total may not exceed
:data:`MAX_UNCOMPRESSED_BYTES`, and there may be at most
:data:`MAX_MEMBERS` of them. Anything else raises
:class:`~jhin_catalog_sync.types.CatalogFormatError`.

A missing shard is not an error — the upstream build omits empty shards,
and 512 shards over a few thousand entries means many are empty. A missing
``sources.lock`` *is* an error: without it there is no provenance to record
against the version, and its absence means the archive is not what it
claims to be.
"""

from __future__ import annotations

import io
import re
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from jhin_catalog_sync.types import CatalogFormatError, SourcesLock
from jhin_tools.sanitize import strict_json_loads

MAX_MEMBER_BYTES: int = 8_388_608  # 8 MiB
MAX_UNCOMPRESSED_BYTES: int = 201_326_592  # 192 MiB
MAX_MEMBERS: int = 600
MEMBER_RE: re.Pattern[str] = re.compile(
    r"^(data/(mcp|skills)/[0-9a-f]{2}\.jsonl|sources\.lock|schema/catalog\.schema\.json)$"
)

SOURCES_LOCK_MEMBER: Final = "sources.lock"
_SHARD_PREFIX: Final = "data/"
_SHARD_SUFFIX: Final = ".jsonl"


@dataclass(frozen=True, slots=True)
class CatalogArchive:
    """The two things the loader needs out of one release archive."""

    shards: Mapping[str, bytes]  # "mcp/00" … "skills/ff" -> raw JSONL bytes
    sources_lock: SourcesLock


def _shard_key(member_name: str) -> str:
    """``data/mcp/7f.jsonl`` -> ``mcp/7f``."""
    return member_name[len(_SHARD_PREFIX) : -len(_SHARD_SUFFIX)]


def _member_bytes(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    """The member's content, refusing to read past the per-member cap."""
    handle = tar.extractfile(member)
    if handle is None:
        raise CatalogFormatError("the catalog archive holds an unreadable member")
    with handle:
        # One byte past the cap: a header that under-declares its size cannot
        # smuggle a larger body past the size check above.
        data = handle.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise CatalogFormatError("the catalog archive holds an oversized member")
    return data


def _sources_lock(raw: bytes) -> SourcesLock:
    try:
        payload = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CatalogFormatError(
            "the catalog archive's sources.lock is not readable JSON"
        ) from None
    try:
        return SourcesLock.model_validate(payload)
    except Exception:
        raise CatalogFormatError("the catalog archive's sources.lock is not usable") from None


def _open_tar(blob: bytes) -> tarfile.TarFile:
    """The gzip tarball, or a refusal that names no part of it."""
    try:
        return tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError):
        raise CatalogFormatError("the catalog archive is not a readable gzip tarball") from None


def open_archive(blob: bytes) -> CatalogArchive:
    """Read one verified ``.tar.gz`` into memory, member by member.

    ``blob`` must already have passed
    :func:`~jhin_catalog_sync.fetch.download_verified_archive`; this function
    checks shape, not provenance.
    """
    tar = _open_tar(blob)

    shards: dict[str, bytes] = {}
    lock: SourcesLock | None = None
    seen: set[str] = set()
    members = 0
    uncompressed = 0
    try:
        for member in tar:
            members += 1
            if members > MAX_MEMBERS:
                raise CatalogFormatError("the catalog archive holds too many members")
            name = member.name
            # A directory entry is inert — it carries no data and cannot
            # point anywhere — and every tar handed a directory writes one,
            # the publishing workflow's included. Rejecting them made the
            # reader refuse its own producer's archives.
            if member.isdir():
                continue
            # A symlink, hardlink, or device never carries shard data, and
            # each is a way to point the reader somewhere else.
            if not member.isfile():
                raise CatalogFormatError("the catalog archive holds a member that is not a file")
            if not MEMBER_RE.fullmatch(name):
                raise CatalogFormatError("the catalog archive holds an unexpected member name")
            if name in seen:
                raise CatalogFormatError("the catalog archive holds a member name twice")
            seen.add(name)
            if member.size > MAX_MEMBER_BYTES:
                raise CatalogFormatError("the catalog archive holds an oversized member")
            uncompressed += member.size
            if uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise CatalogFormatError("the catalog archive expands beyond the allowed size")

            if name == SOURCES_LOCK_MEMBER:
                lock = _sources_lock(_member_bytes(tar, member))
            elif name.startswith(_SHARD_PREFIX):
                shards[_shard_key(name)] = _member_bytes(tar, member)
            # schema/catalog.schema.json is allowed through the name gate for
            # completeness and deliberately ignored: the wire models in
            # types.py are Jhin's copy of that contract.
    except CatalogFormatError:
        raise
    except (tarfile.TarError, EOFError, OSError):
        raise CatalogFormatError("the catalog archive could not be read") from None
    finally:
        tar.close()

    if lock is None:
        raise CatalogFormatError("the catalog archive carries no sources.lock")
    return CatalogArchive(shards=MappingProxyType(shards), sources_lock=lock)


__all__ = [
    "MAX_MEMBERS",
    "MAX_MEMBER_BYTES",
    "MAX_UNCOMPRESSED_BYTES",
    "MEMBER_RE",
    "SOURCES_LOCK_MEMBER",
    "CatalogArchive",
    "open_archive",
]
