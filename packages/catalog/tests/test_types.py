"""Wire models: what the sync will accept from upstream, and what it will not.

The models are deliberately permissive about *unknown* fields and strict about
*known* ones. A catalog published by a newer generator must still load — an
index that stops refreshing because upstream added a column is worse than one
that ignores the column — but nothing outside the declared vocabularies is
allowed to reach a database column or a rendered badge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from jhin_catalog_sync.types import (
    CANONICAL_KEY_RE,
    SLUG_RE,
    SUPPORTED_SCHEMA_VERSION,
    CatalogFetchError,
    CatalogFormatError,
    CatalogIntegrityError,
    CatalogSyncError,
    McpEntry,
    SkillEntry,
    SourcesLock,
    shard_for,
)


def test_a_full_mcp_record_round_trips(mcp_payload: Callable[..., dict[str, Any]]) -> None:
    record = McpEntry.model_validate(mcp_payload())

    assert record.canonical_key == "mcp:registry:io.github.acme/notes"
    assert record.kind == "mcp"
    assert record.slug == "acme_notes"
    assert record.name == "Acme Notes"
    assert record.trust_tier == "registry_verified"
    assert record.transport == "streamable_http"
    assert record.auth_hint == "bearer"
    assert record.mcp_url == "https://mcp.acme.example.com/mcp"
    assert record.url_unverified is False
    assert tuple(record.tags) == ("notes", "docs")


def test_a_full_skill_record_round_trips(skill_payload: Callable[..., dict[str, Any]]) -> None:
    record = SkillEntry.model_validate(skill_payload())

    assert record.canonical_key == "skill:github:acme/skills/release-notes"
    assert record.kind == "skill"
    assert record.slug == "release_notes"
    assert record.skill_name == "release-notes"
    assert record.skill_path == "skills/release-notes/SKILL.md"
    assert record.model_invocable is True
    assert tuple(record.allowed_tools) == ("Read", "Write")


def test_records_are_frozen(mcp_payload: Callable[..., dict[str, Any]]) -> None:
    """A parsed record is evidence, not a scratchpad: the projection to a row
    builds a new value rather than editing what upstream said."""
    record = McpEntry.model_validate(mcp_payload())
    with pytest.raises(ValidationError):
        record.name = "Something else"  # type: ignore[misc]


def test_an_unknown_upstream_field_is_ignored_not_rejected(
    mcp_payload: Callable[..., dict[str, Any]],
) -> None:
    record = McpEntry.model_validate(
        mcp_payload(quality_score=0.91, brand_new_upstream_field={"nested": True})
    )
    assert record.slug == "acme_notes"
    assert not hasattr(record, "quality_score")


def test_a_value_outside_a_closed_vocabulary_is_refused(
    mcp_payload: Callable[..., dict[str, Any]],
) -> None:
    for field, value in (
        ("trust_tier", "self_declared"),
        ("transport", "websocket"),
        ("auth_hint", "basic"),
        ("kind", "plugin"),
    ):
        with pytest.raises(ValidationError):
            McpEntry.model_validate(mcp_payload(**{field: value}))


def test_the_supported_schema_version_is_readable_from_the_record(
    mcp_payload: Callable[..., dict[str, Any]],
) -> None:
    """The shard parser refuses a future schema per record, so the version has
    to survive validation rather than being validated away."""
    assert SUPPORTED_SCHEMA_VERSION == 1
    current = McpEntry.model_validate(mcp_payload())
    future = McpEntry.model_validate(mcp_payload(schema_version=2))
    assert current.schema_version == SUPPORTED_SCHEMA_VERSION
    assert future.schema_version != SUPPORTED_SCHEMA_VERSION


def test_canonical_key_and_slug_patterns() -> None:
    assert CANONICAL_KEY_RE.fullmatch("mcp:registry:io.github.acme/notes")
    assert CANONICAL_KEY_RE.fullmatch("skill:github:acme/skills/release-notes")
    # A space, an empty source, an unknown kind, and a non-ASCII tail are all
    # ways a crafted key could confuse a shard path or a log line.
    assert not CANONICAL_KEY_RE.fullmatch("mcp:registry:has a space")
    assert not CANONICAL_KEY_RE.fullmatch("mcp::notes")
    assert not CANONICAL_KEY_RE.fullmatch("plugin:registry:notes")
    assert not CANONICAL_KEY_RE.fullmatch("mcp:registry:notes more")
    assert not CANONICAL_KEY_RE.fullmatch(f"mcp:registry:{'x' * 221}")

    assert SLUG_RE.fullmatch("acme_notes")
    assert SLUG_RE.fullmatch("a" * 32)
    assert not SLUG_RE.fullmatch("a" * 33)
    assert not SLUG_RE.fullmatch("Acme")
    assert not SLUG_RE.fullmatch("acme-notes")
    assert not SLUG_RE.fullmatch("")


def test_shard_for_is_the_first_two_hex_digits_of_the_key_digest() -> None:
    assert shard_for("mcp:registry:io.github.acme/notes") == "ec"
    assert shard_for("skill:github:acme/skills/release-notes") == "93"
    assert shard_for("mcp:smithery:@acme/ghost") == "90"
    for key in ("mcp:registry:a", "skill:github:b/c", "mcp:npm:@scope/pkg"):
        assert shard_for(key) == hashlib.sha256(key.encode()).hexdigest()[:2]
        assert len(shard_for(key)) == 2


def test_sources_lock_parses_and_tolerates_an_empty_source_list(
    sources_lock: Callable[..., dict[str, Any]],
) -> None:
    lock = SourcesLock.model_validate(sources_lock())
    assert lock.schema_version == 1
    assert len(lock.sources) == 1
    assert lock.sources[0].source_id == "registry"
    assert lock.sources[0].entry_count == 2

    assert SourcesLock.model_validate({}).sources == ()


def test_every_sync_failure_is_one_display_safe_family() -> None:
    """Callers catch one type. The three specialisations exist so the CLI can
    map them to distinct exit codes, not so anyone has to catch three."""
    for error in (CatalogIntegrityError, CatalogFormatError, CatalogFetchError):
        assert issubclass(error, CatalogSyncError)
    assert issubclass(CatalogSyncError, Exception)
