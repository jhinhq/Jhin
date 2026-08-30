"""Shared fixtures for the catalog sync tests.

Everything here is built in Python: no network, no archive on disk, and no
Postgres. The wire payloads are deliberately a *superset* of the fields the
models declare — the models ignore extras by design, so one fixture keeps
working when the upstream schema grows a field, which is the whole point of
``extra="ignore"``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_secrets.redaction import get_redactor


def _mcp_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "canonical_key": "mcp:registry:io.github.acme/notes",
        "kind": "mcp",
        "slug": "acme_notes",
        "name": "Acme Notes",
        "description": "Read and write notes in Acme.",
        "homepage": "https://acme.example.com",
        "docs_url": "https://docs.acme.example.com/mcp",
        "category": "Documents & knowledge",
        "icon": "notebook",
        "trust_tier": "registry_verified",
        "popularity": 0.75,
        "tags": ["notes", "docs"],
        "alias_keys": [],
        "sources": [
            {
                "source_id": "registry",
                "upstream_id": "io.github.acme/notes",
                "url": "https://registry.example.com/v0/servers/acme-notes",
            }
        ],
        "license": "MIT",
        "deprecated": False,
        "connector_type": None,
        "mcp_url": "https://mcp.acme.example.com/mcp",
        "url_unverified": False,
        "transport": "streamable_http",
        "auth_hint": "bearer",
        "auth_note": "Create a token in Acme settings.",
        "setup_note": "",
        "stdio_only": False,
        "connector_config": {},
        "repo": {"host": "github.com", "owner": "acme", "repo": "notes-mcp", "subpath": ""},
        "packages": [
            {
                "registry_type": "npm",
                "identifier": "@acme/notes-mcp",
                "version": "1.2.3",
                "runtime_hint": "node",
                "transport": "stdio",
            }
        ],
        "remotes": [
            {
                "transport": "streamable_http",
                "url": "https://mcp.acme.example.com/mcp",
                "templated": False,
                "header_names": [],
            }
        ],
        "tool_count": 12,
        "registry_name": "io.github.acme/notes",
        "smithery_qualified_name": "",
        "npm_package": "@acme/notes-mcp",
        "verified_upstream": True,
        "popularity_signals": {
            "github_stars": 420,
            "github_forks": 21,
            "npm_downloads_monthly": 9000,
            "npm_dependents": 3,
            "smithery_use_count": None,
            "registry_version_count": 4,
        },
    }
    payload.update(overrides)
    return payload


def _skill_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "canonical_key": "skill:github:acme/skills/release-notes",
        "kind": "skill",
        "slug": "release_notes",
        "name": "Release notes",
        "description": "Turn a changelog into user-facing release notes.",
        "homepage": "https://github.com/acme/skills",
        "docs_url": "",
        "category": "Productivity",
        "icon": "notebook",
        "trust_tier": "indexed",
        "popularity": 0.2,
        "tags": ["writing"],
        "alias_keys": [],
        "sources": [
            {
                "source_id": "github",
                "upstream_id": "acme/skills",
                "url": "https://github.com/acme/skills",
            }
        ],
        "license": "Apache-2.0",
        "deprecated": False,
        "repo": {"host": "github.com", "owner": "acme", "repo": "skills", "subpath": "skills"},
        "skill_name": "release-notes",
        "source_ref": "acme/skills@main",
        "skill_path": "skills/release-notes/SKILL.md",
        "plugin": None,
        "commit_sha": "b" * 40,
        "model_invocable": True,
        "allowed_tools": ["Read", "Write"],
        "skill_version": "1.0.0",
        "frontmatter_bytes": 384,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def mcp_payload() -> Callable[..., dict[str, Any]]:
    """Factory for one wire-shaped MCP record."""
    return _mcp_payload


@pytest.fixture
def skill_payload() -> Callable[..., dict[str, Any]]:
    """Factory for one wire-shaped skill record."""
    return _skill_payload


def _jsonl(payloads: Sequence[Mapping[str, Any]]) -> bytes:
    """One JSONL shard body. Separators are tight so byte caps mean something."""
    return "".join(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        for payload in payloads
    ).encode("utf-8")


@pytest.fixture
def jsonl() -> Callable[[Sequence[Mapping[str, Any]]], bytes]:
    return _jsonl


def _tar_gz(members: Sequence[tuple[str, bytes]], *, extra: Sequence[Any] = ()) -> bytes:
    """Build a tar.gz in memory from ``(name, body)`` pairs plus raw members.

    ``extra`` carries pre-built :class:`tarfile.TarInfo` objects (symlinks,
    directories, absolute paths) that cannot be expressed as a name/body pair —
    exactly the shapes ``open_archive`` has to refuse.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
        for info in extra:
            archive.addfile(info)
    return buffer.getvalue()


@pytest.fixture
def tar_gz() -> Callable[..., bytes]:
    return _tar_gz


def _sources_lock(**overrides: Any) -> dict[str, Any]:
    lock: dict[str, Any] = {
        "schema_version": 1,
        "sources": [
            {
                "source_id": "registry",
                "url": "https://registry.example.com/v0/servers",
                "fetched_at": "2026-08-28T00:00:00Z",
                "sha256": "c" * 64,
                "entry_count": 2,
                "page_count": 1,
            }
        ],
    }
    lock.update(overrides)
    return lock


@pytest.fixture
def sources_lock() -> Callable[..., dict[str, Any]]:
    return _sources_lock


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture
def sha256() -> Callable[[bytes], str]:
    return _sha256


@pytest.fixture(scope="session")
def gzip_bomb() -> bytes:
    """A tar.gz that expands past the uncompressed ceiling while the archive
    itself stays a few kilobytes.

    Every member is individually legal — a valid shard name, comfortably under
    the per-member cap — so only a *running* total can catch this. Zeros
    compress to nothing, which is exactly the shape of the attack.
    """
    member = b"\0" * (8 * 1024 * 1024)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(25):  # 200 MiB, past the 192 MiB ceiling
            info = tarfile.TarInfo(name=f"data/mcp/{index:02x}.jsonl")
            info.size = len(member)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(member))
    return buffer.getvalue()


@pytest.fixture
def plain_gzip() -> Callable[[bytes], bytes]:
    return gzip.compress


@pytest.fixture
def registered_secret() -> Iterator[str]:
    """A plaintext the process-wide redactor knows about.

    The redactor is global, so the value is removed again afterwards: a test
    that leaves a secret registered silently rewrites unrelated strings in
    every test that runs after it.
    """
    secret = "sk-live-catalogsynctest-9f2a"
    redactor = get_redactor()
    redactor.register(secret)
    try:
        yield secret
    finally:
        redactor.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """The full schema on in-memory SQLite, one session, one connection."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()
