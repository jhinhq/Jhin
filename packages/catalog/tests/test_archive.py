"""Opening a hostile tarball safely.

The archive is verified before it gets here, which proves it is the file
upstream published — not that upstream published something sane. These
tests are the second half: every classic tar trick, refused without a byte
touching the filesystem.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from jhin_catalog_sync.archive import (
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    CatalogArchive,
    open_archive,
)
from jhin_catalog_sync.types import CatalogFormatError

TarGz = Callable[..., bytes]
Jsonl = Callable[[Sequence[Mapping[str, Any]]], bytes]
Factory = Callable[..., dict[str, Any]]


def _lock_bytes(lock: Mapping[str, Any]) -> bytes:
    return json.dumps(lock).encode("utf-8")


def _valid(
    tar_gz: TarGz, jsonl: Jsonl, lock: Mapping[str, Any], payloads: Mapping[str, Any]
) -> bytes:
    members = [(name, jsonl([payload])) for name, payload in payloads.items()]
    members.append(("sources.lock", _lock_bytes(lock)))
    return tar_gz(members)


# --- the happy path ---


def test_a_valid_archive_yields_its_shards_and_its_lock(
    tar_gz: TarGz,
    jsonl: Jsonl,
    sources_lock: Factory,
    mcp_payload: Factory,
    skill_payload: Factory,
) -> None:
    blob = _valid(
        tar_gz,
        jsonl,
        sources_lock(),
        {"data/mcp/00.jsonl": mcp_payload(), "data/skills/ff.jsonl": skill_payload()},
    )

    archive = open_archive(blob)

    assert isinstance(archive, CatalogArchive)
    assert set(archive.shards) == {"mcp/00", "skills/ff"}
    assert archive.shards["mcp/00"].endswith(b"\n")
    assert len(archive.sources_lock.sources) == 1
    assert archive.sources_lock.sources[0].source_id == "registry"


def test_a_missing_shard_is_simply_absent(
    tar_gz: TarGz, jsonl: Jsonl, sources_lock: Factory, mcp_payload: Factory
) -> None:
    archive = open_archive(
        _valid(tar_gz, jsonl, sources_lock(), {"data/mcp/7f.jsonl": mcp_payload()})
    )

    assert set(archive.shards) == {"mcp/7f"}
    assert archive.shards.get("mcp/00") is None


def test_an_empty_shard_is_kept_as_an_empty_body(tar_gz: TarGz, sources_lock: Factory) -> None:
    archive = open_archive(
        tar_gz([("data/mcp/00.jsonl", b""), ("sources.lock", _lock_bytes(sources_lock()))])
    )

    assert archive.shards == {"mcp/00": b""}


def test_the_json_schema_member_is_allowed_and_ignored(
    tar_gz: TarGz, sources_lock: Factory
) -> None:
    archive = open_archive(
        tar_gz(
            [
                ("schema/catalog.schema.json", b'{"title": "catalog"}'),
                ("sources.lock", _lock_bytes(sources_lock())),
            ]
        )
    )

    assert archive.shards == {}


def test_the_shards_mapping_cannot_be_written_through(
    tar_gz: TarGz, jsonl: Jsonl, sources_lock: Factory, mcp_payload: Factory
) -> None:
    archive = open_archive(
        _valid(tar_gz, jsonl, sources_lock(), {"data/mcp/00.jsonl": mcp_payload()})
    )

    with pytest.raises(TypeError):
        archive.shards["mcp/01"] = b"injected"  # type: ignore[index]


# --- refusals ---


@pytest.mark.parametrize(
    "name",
    [
        "../escape.jsonl",
        "data/../../escape.jsonl",
        "/etc/passwd",
        "data/mcp/00.jsonl.bak",
        "data/mcp/GG.jsonl",
        "data/mcp/000.jsonl",
        "data/servers/00.jsonl",
        "sources.lock.bak",
        "data/mcp/00.jsonl/../../../etc/shadow",
    ],
)
def test_an_unexpected_member_name_is_refused(
    tar_gz: TarGz, sources_lock: Factory, name: str
) -> None:
    blob = tar_gz([(name, b"{}\n"), ("sources.lock", _lock_bytes(sources_lock()))])

    with pytest.raises(CatalogFormatError) as caught:
        open_archive(blob)

    assert name not in str(caught.value), "a refusal must not echo the name it refused"


def test_a_symlink_member_is_refused(tar_gz: TarGz, sources_lock: Factory) -> None:
    link = tarfile.TarInfo(name="data/mcp/00.jsonl")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    blob = tar_gz([("sources.lock", _lock_bytes(sources_lock()))], extra=[link])

    with pytest.raises(CatalogFormatError) as caught:
        open_archive(blob)

    assert "not a file" in str(caught.value)


def test_a_directory_member_is_skipped_not_refused(tar_gz: TarGz, sources_lock: Factory) -> None:
    """Every tar handed a directory writes one, the publisher's included.

    This asserted a refusal until 2026-08-29, when the first real release
    was rejected with "an unexpected member name": the workflow tars
    ``data`` as a directory argument, so ``data/``, ``data/mcp/`` and
    ``data/skills/`` are all in the archive. A directory carries no bytes
    and cannot redirect the reader, so it is skipped; a symlink, hardlink
    or device still is not, which the test above covers.
    """
    directory = tarfile.TarInfo(name="data/mcp")
    directory.type = tarfile.DIRTYPE
    blob = tar_gz([("sources.lock", _lock_bytes(sources_lock()))], extra=[directory])

    archive = open_archive(blob)

    assert archive.shards == {}


def test_an_oversized_member_is_refused(tar_gz: TarGz, sources_lock: Factory) -> None:
    blob = tar_gz(
        [
            ("data/mcp/00.jsonl", b"x" * (MAX_MEMBER_BYTES + 1)),
            ("sources.lock", _lock_bytes(sources_lock())),
        ]
    )

    with pytest.raises(CatalogFormatError) as caught:
        open_archive(blob)

    assert "oversized" in str(caught.value)


def test_more_members_than_the_ceiling_is_refused(tar_gz: TarGz, sources_lock: Factory) -> None:
    # There are only 514 legal member names, so an archive with 601 members
    # necessarily repeats one. Both gates return the same refusal; the count
    # ceiling stands behind the name gate rather than in front of it.
    members = [("data/mcp/00.jsonl", b"{}\n")] * (MAX_MEMBERS + 1)
    members.append(("sources.lock", _lock_bytes(sources_lock())))

    with pytest.raises(CatalogFormatError):
        open_archive(tar_gz(members))


def test_a_member_name_appearing_twice_is_refused(tar_gz: TarGz, sources_lock: Factory) -> None:
    blob = tar_gz(
        [
            ("data/mcp/00.jsonl", b"{}\n"),
            ("data/mcp/00.jsonl", b"{}\n"),
            ("sources.lock", _lock_bytes(sources_lock())),
        ]
    )

    with pytest.raises(CatalogFormatError) as caught:
        open_archive(blob)

    assert "twice" in str(caught.value)


def test_a_decompression_bomb_is_refused(gzip_bomb: bytes) -> None:
    assert len(gzip_bomb) < 1_000_000, "the fixture must stay small on the wire"

    with pytest.raises(CatalogFormatError) as caught:
        open_archive(gzip_bomb)

    assert "beyond the allowed size" in str(caught.value)


def test_something_that_is_not_a_gzip_tarball_is_refused() -> None:
    with pytest.raises(CatalogFormatError) as caught:
        open_archive(b"not a tarball at all")

    assert "gzip tarball" in str(caught.value)


def test_gzip_that_is_not_a_tarball_is_refused(plain_gzip: Callable[[bytes], bytes]) -> None:
    with pytest.raises(CatalogFormatError):
        open_archive(plain_gzip(b"just some gzipped bytes"))


def test_an_archive_without_a_sources_lock_is_refused(
    tar_gz: TarGz, jsonl: Jsonl, mcp_payload: Factory
) -> None:
    with pytest.raises(CatalogFormatError) as caught:
        open_archive(tar_gz([("data/mcp/00.jsonl", jsonl([mcp_payload()]))]))

    assert "sources.lock" in str(caught.value)


def test_a_sources_lock_that_is_not_json_is_refused(tar_gz: TarGz) -> None:
    with pytest.raises(CatalogFormatError) as caught:
        open_archive(tar_gz([("sources.lock", b"{not json")]))

    assert "readable JSON" in str(caught.value)


def test_a_sources_lock_of_the_wrong_shape_is_refused(tar_gz: TarGz) -> None:
    with pytest.raises(CatalogFormatError):
        open_archive(tar_gz([("sources.lock", b'{"sources": "not a list"}')]))
