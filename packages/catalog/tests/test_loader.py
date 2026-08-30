"""Loading a generation without a reader ever seeing half of one.

The invariant under test is narrow and absolute: at every instant, a reader
that resolves the active version id sees one complete generation. A load in
progress is invisible; a load that dies is invisible; only the two-statement
swap changes what anybody reads. Everything else here — resumability,
idempotence, pruning — exists so that invariant survives a sync that crashes
at an inconvenient moment.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_catalog_sync import loader as loader_module
from jhin_catalog_sync.archive import CatalogArchive
from jhin_catalog_sync.loader import (
    KEEP_INACTIVE_VERSIONS,
    SyncOutcome,
    active_version,
    load_archive,
    prune_versions,
)
from jhin_catalog_sync.types import CatalogSyncError, SourcesLock
from jhin_db.models import CatalogEntry, CatalogVersion

# Canonical keys chosen so each one hashes into a named shard: the loader
# checks that a record was filed where its key says it belongs, and the load
# order is what makes ``shard_cursor`` a resumable position.
MCP_00_A = "mcp:registry:acme/srv83"
MCP_00_B = "mcp:registry:acme/srv260"
MCP_41 = "mcp:registry:acme/srv7"
MCP_FF = "mcp:registry:acme/srv53"
SKILL_7F = "skill:github:acme/skills/srv367"

SHA_ONE = "1" * 64
SHA_TWO = "2" * 64
NO_RESERVED: frozenset[str] = frozenset()

LOAD_ARGS: dict[str, Any] = {
    "source_repo": "jhinhq/jhin-catalog",
    "asset_url": "https://api.github.com/repos/jhinhq/jhin-catalog/releases/assets/1",
    "reserved_slugs": NO_RESERVED,
}


def _archive(shards: Mapping[str, list[dict[str, Any]]]) -> CatalogArchive:
    body = {
        key: "".join(json.dumps(payload) + "\n" for payload in payloads).encode("utf-8")
        for key, payloads in shards.items()
    }
    return CatalogArchive(shards=body, sources_lock=SourcesLock())


def _two_shard_archive(
    mcp_payload: Callable[..., dict[str, Any]], skill_payload: Callable[..., dict[str, Any]]
) -> CatalogArchive:
    return _archive(
        {
            "mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug="acme_one", name="Acme One")],
            "mcp/41": [mcp_payload(canonical_key=MCP_41, slug="acme_two", name="Acme Two")],
            "skills/7f": [
                skill_payload(canonical_key=SKILL_7F, slug="acme_skill", name="Acme Skill")
            ],
        }
    )


async def _counts(db: AsyncSession) -> tuple[int, int]:
    versions = await db.scalar(select(func.count()).select_from(CatalogVersion))
    entries = await db.scalar(select(func.count()).select_from(CatalogEntry))
    return int(versions or 0), int(entries or 0)


async def _load(
    db: AsyncSession,
    archive: CatalogArchive,
    *,
    tag: str = "2026.08.28",
    sha: str = SHA_ONE,
    **kw: Any,
) -> SyncOutcome:
    return await load_archive(db, archive, release_tag=tag, data_sha256=sha, **{**LOAD_ARGS, **kw})


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_a_first_load_publishes_the_generation(
    session: AsyncSession,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    assert await active_version(session) is None

    outcome = await _load(session, _two_shard_archive(mcp_payload, skill_payload))

    assert outcome.changed is True
    assert outcome.resumed is False
    assert (outcome.entry_count, outcome.mcp_count, outcome.skill_count) == (3, 2, 1)

    published = await active_version(session)
    assert published is not None
    assert published.id == outcome.version_id
    assert published.status == "active"
    assert published.release_tag == "2026.08.28"
    assert published.data_sha256 == SHA_ONE
    assert published.entry_count == 3
    assert published.activated_at is not None
    assert published.error == ""
    # Every shard was walked, so the cursor sits at the end of the plan.
    assert published.shard_cursor == "skills/ff"

    rows = (await session.scalars(select(CatalogEntry.canonical_key))).all()
    assert set(rows) == {MCP_00_A, MCP_41, SKILL_7F}


async def test_an_empty_archive_still_publishes_an_empty_generation(
    session: AsyncSession,
) -> None:
    """Better an honest empty catalog than a stale one nobody can explain."""
    outcome = await _load(session, _archive({}))

    assert outcome.changed is True
    assert outcome.entry_count == 0
    published = await active_version(session)
    assert published is not None and published.entry_count == 0


# --------------------------------------------------------------------------
# idempotence
# --------------------------------------------------------------------------


async def test_reloading_the_active_release_writes_nothing(
    session: AsyncSession,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    archive = _two_shard_archive(mcp_payload, skill_payload)
    first = await _load(session, archive)
    before = await _counts(session)

    second = await _load(session, archive)

    assert second.changed is False
    assert second.version_id == first.version_id
    assert second.entry_count == first.entry_count
    assert await _counts(session) == before
    published = await active_version(session)
    assert published is not None and published.id == first.version_id


async def test_a_rebuilt_archive_under_the_same_tag_is_a_new_generation(
    session: AsyncSession,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    """The digest is half the identity: the same tag with different bytes has
    to replace, not silently do nothing."""
    first = await _load(session, _two_shard_archive(mcp_payload, skill_payload))
    second = await _load(
        session,
        _archive({"mcp/ff": [mcp_payload(canonical_key=MCP_FF, slug="acme_tail")]}),
        sha=SHA_TWO,
    )

    assert second.changed is True
    assert second.version_id != first.version_id
    published = await active_version(session)
    assert published is not None and published.id == second.version_id
    superseded = await session.get(CatalogVersion, first.version_id)
    assert superseded is not None and superseded.status == "superseded"


# --------------------------------------------------------------------------
# resuming
# --------------------------------------------------------------------------


async def test_an_interrupted_load_resumes_after_its_cursor(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    """A sync that died at ``mcp/40`` must not re-walk the 65 shards it already
    committed — and must not lose the ones it had not reached."""
    interrupted = CatalogVersion(
        release_tag="2026.08.28",
        data_sha256=SHA_ONE,
        status="loading",
        shard_cursor="mcp/40",
    )
    session.add(interrupted)
    await session.commit()
    version_id = interrupted.id

    outcome = await _load(
        session,
        _archive(
            {
                "mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug="already_loaded")],
                "mcp/41": [mcp_payload(canonical_key=MCP_41, slug="acme_two")],
                "mcp/ff": [mcp_payload(canonical_key=MCP_FF, slug="acme_tail")],
            }
        ),
    )

    assert outcome.resumed is True
    assert outcome.version_id == version_id
    keys = set((await session.scalars(select(CatalogEntry.canonical_key))).all())
    assert keys == {MCP_41, MCP_FF}, "shards at or before the cursor must be skipped"
    assert outcome.entry_count == 2


async def test_a_failed_generation_is_reset_rather_than_resumed(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    """A failed run may have stopped halfway through a shard it never committed
    as complete, so its cursor is not evidence of anything."""
    failed = CatalogVersion(
        release_tag="2026.08.28",
        data_sha256=SHA_ONE,
        status="failed",
        shard_cursor="mcp/40",
        error="database error: OperationalError",
    )
    session.add(failed)
    await session.flush()
    session.add(
        CatalogEntry(
            version_id=failed.id,
            canonical_key="mcp:registry:acme/stale",
            kind="mcp",
            slug="stale",
            name="Stale",
            category="Developer tools",
        )
    )
    await session.commit()

    outcome = await _load(
        session, _archive({"mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug="acme_one")]})
    )

    keys = set((await session.scalars(select(CatalogEntry.canonical_key))).all())
    assert keys == {MCP_00_A}, "the failed run's rows must be dropped, not kept"
    published = await active_version(session)
    assert published is not None and published.error == ""
    assert outcome.entry_count == 1


# --------------------------------------------------------------------------
# the atomic swap
# --------------------------------------------------------------------------


async def test_a_reader_sees_the_previous_generation_throughout_a_load(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    """Checked at every committed step of the next load, not just at the end."""
    first = await _load(session, _two_shard_archive(mcp_payload, skill_payload))

    original = loader_module._advance_cursor
    seen: list[UUID] = []

    async def watching(db: AsyncSession, version_id: UUID, shard_key: str) -> None:
        current = await active_version(db)
        assert current is not None
        seen.append(current.id)
        await original(db, version_id, shard_key)

    monkeypatch.setattr(loader_module, "_advance_cursor", watching)

    second = await _load(
        session,
        _archive({"mcp/ff": [mcp_payload(canonical_key=MCP_FF, slug="acme_tail")]}),
        sha=SHA_TWO,
    )

    assert seen, "the load must actually have committed shards"
    assert set(seen) == {first.version_id}, "the old generation stayed active all the way through"
    published = await active_version(session)
    assert published is not None and published.id == second.version_id


async def test_a_load_that_dies_leaves_the_active_generation_untouched(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    first = await _load(session, _two_shard_archive(mcp_payload, skill_payload))

    async def exploding(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the swap fell over")

    monkeypatch.setattr(loader_module, "_swap", exploding)

    with pytest.raises(RuntimeError):
        await _load(
            session,
            _archive({"mcp/ff": [mcp_payload(canonical_key=MCP_FF, slug="acme_tail")]}),
            sha=SHA_TWO,
        )

    published = await active_version(session)
    assert published is not None
    assert published.id == first.version_id, "the swap never happened, so nothing moved"
    assert published.entry_count == 3

    doomed = await session.scalar(
        select(CatalogVersion).where(CatalogVersion.data_sha256 == SHA_TWO)
    )
    assert doomed is not None
    assert doomed.status == "failed"
    assert doomed.error
    assert len(doomed.error) <= 500
    assert "the swap fell over" not in doomed.error, (
        "an arbitrary exception's message may quote the row it was writing"
    )


async def test_a_failure_is_reported_as_a_sync_error_and_recorded_without_prose(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mcp_payload: Callable[..., dict[str, Any]],
) -> None:
    def exploding(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("INSERT INTO catalog_entry", {}, Exception("CANARY-row-text"))

    monkeypatch.setattr(loader_module, "_upsert", exploding)

    with pytest.raises(CatalogSyncError) as caught:
        await _load(
            session, _archive({"mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug="acme_one")]})
        )

    assert "CANARY-row-text" not in str(caught.value)
    version = await session.scalar(select(CatalogVersion))
    assert version is not None and version.status == "failed"
    assert "CANARY-row-text" not in version.error
    assert await active_version(session) is None


async def test_only_one_generation_is_ever_active(
    session: AsyncSession,
    mcp_payload: Callable[..., dict[str, Any]],
    skill_payload: Callable[..., dict[str, Any]],
) -> None:
    await _load(session, _two_shard_archive(mcp_payload, skill_payload))
    await _load(
        session,
        _archive({"mcp/ff": [mcp_payload(canonical_key=MCP_FF, slug="acme_tail")]}),
        sha=SHA_TWO,
    )
    await _load(
        session,
        _archive({"mcp/41": [mcp_payload(canonical_key=MCP_41, slug="acme_two")]}),
        sha="3" * 64,
    )

    actives = await session.scalar(
        select(func.count()).select_from(CatalogVersion).where(CatalogVersion.status == "active")
    )
    assert actives == 1


# --------------------------------------------------------------------------
# slug reservation
# --------------------------------------------------------------------------


async def test_a_synced_row_never_takes_a_reserved_slug(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    await _load(
        session,
        _archive({"mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug="github")]}),
        reserved_slugs=frozenset({"github", "notion"}),
    )

    slug = await session.scalar(select(CatalogEntry.slug))
    assert slug != "github"
    assert slug is not None and slug.startswith("github_")


async def test_two_upstream_entries_wanting_one_slug_do_not_break_the_sync(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    """The database would refuse the second row and take the whole refresh
    down with it. One of them is dropped and counted instead."""
    outcome = await _load(
        session,
        _archive(
            {
                "mcp/00": [
                    mcp_payload(canonical_key=MCP_00_A, slug="duplicate", name="First"),
                    mcp_payload(canonical_key=MCP_00_B, slug="duplicate", name="Second"),
                ]
            }
        ),
    )

    assert outcome.entry_count == 1
    assert outcome.rejected_count >= 1
    published = await active_version(session)
    assert published is not None and published.status == "active"


# --------------------------------------------------------------------------
# pruning
# --------------------------------------------------------------------------


async def test_pruning_keeps_the_active_generation_and_the_newest_two(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    for index in range(5):
        await _load(
            session,
            _archive({"mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug=f"gen_{index}")]}),
            sha=str(index) * 64,
        )

    deleted = await prune_versions(session)

    assert deleted == 5 - 1 - KEEP_INACTIVE_VERSIONS
    remaining = (await session.scalars(select(CatalogVersion.status))).all()
    assert sorted(remaining) == ["active", "superseded", "superseded"]
    assert await prune_versions(session) == 0


async def test_pruning_takes_the_entries_with_it(
    session: AsyncSession, mcp_payload: Callable[..., dict[str, Any]]
) -> None:
    """Explicitly, not on the cascade: SQLite honours ``ON DELETE CASCADE``
    only with foreign keys switched on, and an orphaned entry row outlives
    every reader that could find it."""
    for index in range(4):
        await _load(
            session,
            _archive({"mcp/00": [mcp_payload(canonical_key=MCP_00_A, slug=f"gen_{index}")]}),
            sha=str(index) * 64,
        )
    await prune_versions(session)

    live = set((await session.scalars(select(CatalogVersion.id))).all())
    owners = set((await session.scalars(select(CatalogEntry.version_id))).all())
    assert owners <= live, "no entry may outlive the generation it belongs to"
