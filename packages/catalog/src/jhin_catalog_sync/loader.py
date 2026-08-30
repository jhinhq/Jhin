"""Loading one archive into the database, without a reader ever noticing.

The whole design is one idea: a reader resolves the active version id first
and filters every query on it, so a generation that is still filling up is
invisible no matter how long it takes. That turns a catalog refresh from a
migration into an append followed by a two-row swap.

The protocol, in order:

1. find or create the ``catalog_version`` for ``(release_tag, data_sha256)``;
   an already-active one is a no-op and writes nothing;
2. a ``failed`` row is reset and its entries dropped; a ``loading`` row is
   resumed from its ``shard_cursor``; a ``superseded`` row is re-activated
   without reloading the rows it already holds;
3. shards load in a fixed order — ``mcp/00``…``mcp/ff``, then
   ``skills/00``…``skills/ff`` — in batches of at most :data:`BATCH_SIZE`,
   each an ``INSERT … ON CONFLICT DO UPDATE`` keyed on
   ``(version_id, canonical_key)``, and the cursor advances only once a
   shard is completely in;
4. the swap — every active row to ``superseded``, this row to ``active`` —
   is one transaction with nothing else in it;
5. everything but the active version and the newest
   :data:`KEEP_INACTIVE_VERSIONS` inactive ones is pruned.

Two syncs may race freely. ``uq_catalog_version_active`` — the partial
unique index over ``status = 'active'`` — makes a double activation
unrepresentable, so the loser fails the swap and gets a
:class:`~jhin_catalog_sync.types.CatalogSyncError` instead of corrupting
the generation the winner just published.

Nothing this module writes to the database carries upstream prose: the
``error`` column records the *shape* of a failure, never a message that came
in over the network.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from jhin_catalog_sync.archive import CatalogArchive
from jhin_catalog_sync.types import (
    SUPPORTED_SCHEMA_VERSION,
    CatalogSyncError,
    EntryKind,
)
from jhin_catalog_sync.wire import parse_shard, to_row
from jhin_db.models import CatalogEntry, CatalogVersion
from jhin_domain import new_uuid7

BATCH_SIZE: int = 500
KEEP_INACTIVE_VERSIONS: int = 2

STATUS_LOADING: Final = "loading"
STATUS_ACTIVE: Final = "active"
STATUS_SUPERSEDED: Final = "superseded"
STATUS_FAILED: Final = "failed"

# Every ``catalog_entry`` column except the identity of the row and the
# moment it first appeared: those are what an upsert must leave alone.
_MUTABLE_COLUMNS: tuple[str, ...] = (
    "kind",
    "slug",
    "name",
    "description",
    "summary",
    "homepage",
    "docs_url",
    "icon_url",
    "trust_tier",
    "trust_rank",
    "default_risk",
    "popularity",
    "category",
    "icon",
    "connector_type",
    "mcp_url",
    "url_unverified",
    "transport",
    "auth_hint",
    "auth_note",
    "setup_note",
    "stdio_only",
    "deprecated",
    "publishable",
    "license",
    "tags_json",
    "alias_keys_json",
    "sources_json",
    "connector_config_json",
    "mcp_json",
    "skill_json",
    "search_text",
    "updated_at",
)

_HEX: Final = tuple(f"{value:02x}" for value in range(256))
# The fixed load order. Lexicographic on the shard key, which is what makes
# "skip every shard at or before the cursor" a plain string comparison.
_SHARD_PLAN: Final[tuple[tuple[str, EntryKind], ...]] = tuple(
    [(f"mcp/{suffix}", "mcp") for suffix in _HEX]
    + [(f"skills/{suffix}", "skill") for suffix in _HEX]
)

_MAX_ERROR_CHARS: Final = 500
_Rows = Sequence[Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What one call to :func:`load_archive` did."""

    version_id: UUID
    release_tag: str
    data_sha256: str
    changed: bool
    entry_count: int
    mcp_count: int
    skill_count: int
    rejected_count: int
    resumed: bool


async def active_version(db: AsyncSession) -> CatalogVersion | None:
    """The one generation readers are served from, or ``None`` before the
    first successful sync."""
    current: CatalogVersion | None = await db.scalar(
        select(CatalogVersion).where(CatalogVersion.status == STATUS_ACTIVE)
    )
    return current


def _batches(rows: _Rows) -> Iterator[tuple[_Rows, bool]]:
    """Yield ``(batch, is_last)`` so the cursor advances only on the last."""
    total = len(rows)
    for start in range(0, total, BATCH_SIZE):
        end = min(start + BATCH_SIZE, total)
        yield rows[start:end], end >= total


def _upsert(db: AsyncSession, rows: _Rows) -> Insert:
    """One dialect-appropriate ``INSERT … ON CONFLICT DO UPDATE``.

    Both dialects' ``insert`` are imported by name rather than rebound to one
    symbol: they are distinct types, and a conditional rebinding is exactly
    the shape a type checker cannot follow.
    """
    conflict_target = [CatalogEntry.version_id, CatalogEntry.canonical_key]
    values = list(rows)
    if db.get_bind().dialect.name == "postgresql":
        postgres = pg_insert(CatalogEntry).values(values)
        return postgres.on_conflict_do_update(
            index_elements=conflict_target,
            set_={column: postgres.excluded[column] for column in _MUTABLE_COLUMNS},
        )
    sqlite = sqlite_insert(CatalogEntry).values(values)
    return sqlite.on_conflict_do_update(
        index_elements=conflict_target,
        set_={column: sqlite.excluded[column] for column in _MUTABLE_COLUMNS},
    )


def _failure_reason(error: BaseException) -> str:
    """A bounded, catalog-text-free description of why a load stopped.

    Sync errors are display-safe by contract, so their message is kept.
    Anything else is reduced to its type: a driver exception happily quotes
    the row it was writing, and that row came from the internet.
    """
    if isinstance(error, CatalogSyncError):
        return str(error)[:_MAX_ERROR_CHARS]
    if isinstance(error, SQLAlchemyError):
        return f"database error: {type(error).__name__}"[:_MAX_ERROR_CHARS]
    return f"unexpected error: {type(error).__name__}"[:_MAX_ERROR_CHARS]


async def _mark_failed(db: AsyncSession, version_id: UUID, error: BaseException) -> None:
    """Record why this generation stopped, so the next run resets it.

    Best effort by design: the session may already be poisoned by the very
    failure being recorded, and losing the marker matters far less than
    losing the original exception behind a second one.
    """
    try:
        await db.rollback()
        await db.execute(
            update(CatalogVersion)
            .where(CatalogVersion.id == version_id, CatalogVersion.status == STATUS_LOADING)
            .values(status=STATUS_FAILED, error=_failure_reason(error))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        # The statement bypassed the ORM, so anything the caller still holds
        # for this row is now a lie. Expiring is what makes the next read of
        # it go back to the database.
        db.expire_all()
    except Exception:
        await db.rollback()


async def _open_version(
    db: AsyncSession,
    *,
    release_tag: str,
    data_sha256: str,
    source_repo: str,
    asset_url: str,
    sources_lock_json: dict[str, Any],
) -> tuple[UUID | None, bool]:
    """Find or create the generation to load into.

    Returns ``(version_id, resumed)``, or ``(None, False)`` when this exact
    release is already active and there is nothing to do.
    """
    existing = await db.scalar(
        select(CatalogVersion).where(
            CatalogVersion.release_tag == release_tag,
            CatalogVersion.data_sha256 == data_sha256,
        )
    )
    if existing is not None and existing.status == STATUS_ACTIVE:
        return None, False

    if existing is None:
        version = CatalogVersion(
            release_tag=release_tag,
            data_sha256=data_sha256,
            source_repo=source_repo[:120],
            asset_url=asset_url[:512],
            schema_version=SUPPORTED_SCHEMA_VERSION,
            status=STATUS_LOADING,
            shard_cursor="",
            sources_lock_json=sources_lock_json,
            error="",
        )
        db.add(version)
        await db.flush()
        version_id = version.id
        await db.commit()
        return version_id, False

    version_id = existing.id
    if existing.status == STATUS_FAILED:
        # A failed load may have stopped halfway through a shard it never
        # committed as complete. Start clean rather than trust that cursor.
        await db.execute(delete(CatalogEntry).where(CatalogEntry.version_id == version_id))
        existing.shard_cursor = ""
    existing.status = STATUS_LOADING
    existing.error = ""
    existing.source_repo = source_repo[:120]
    existing.asset_url = asset_url[:512]
    existing.sources_lock_json = sources_lock_json
    await db.commit()
    return version_id, True


async def _shard_cursor(db: AsyncSession, version_id: UUID) -> str:
    cursor = await db.scalar(
        select(CatalogVersion.shard_cursor).where(CatalogVersion.id == version_id)
    )
    return cursor or ""


async def _claimed_slugs(db: AsyncSession, version_id: UUID) -> dict[tuple[str, str], str]:
    """``(kind, slug) -> canonical_key`` for rows already in this generation."""
    result = await db.execute(
        select(CatalogEntry.kind, CatalogEntry.slug, CatalogEntry.canonical_key).where(
            CatalogEntry.version_id == version_id
        )
    )
    return {(kind, slug): key for kind, slug, key in result.all()}


async def _advance_cursor(db: AsyncSession, version_id: UUID, shard_key: str) -> None:
    await db.execute(
        update(CatalogVersion)
        .where(CatalogVersion.id == version_id)
        .values(shard_cursor=shard_key)
        .execution_options(synchronize_session=False)
    )


async def _load_shards(
    db: AsyncSession,
    archive: CatalogArchive,
    version_id: UUID,
    *,
    reserved_slugs: frozenset[str],
) -> int:
    """Fill the generation shard by shard; return how many lines were dropped."""
    cursor = await _shard_cursor(db, version_id)
    # One timestamp for the whole load, written explicitly rather than left to
    # the column default: ``ON CONFLICT DO UPDATE`` sets ``updated_at`` from
    # the proposed row, and a proposed row has to actually carry the value for
    # both dialects to agree on what that means.
    loaded_at = datetime.now(UTC)
    rejected = 0
    # A slug is unique within a generation, and two unrelated upstream
    # entries can perfectly well want the same one. The database would refuse
    # the second and take the whole sync down with it, so the loser is
    # dropped here and counted, exactly like a malformed line.
    claimed = await _claimed_slugs(db, version_id) if cursor else {}

    for shard_key, kind in _SHARD_PLAN:
        if cursor and shard_key <= cursor:
            continue
        records, reasons = parse_shard(
            archive.shards.get(shard_key, b""), kind=kind, shard=shard_key.rpartition("/")[2]
        )
        rejected += len(reasons)

        rows: list[dict[str, Any]] = []
        for record in records:
            row: dict[str, Any] = dict(to_row(record, reserved_slugs=reserved_slugs))
            identity = (str(row["kind"]), str(row["slug"]))
            canonical_key = str(row["canonical_key"])
            if claimed.get(identity, canonical_key) != canonical_key:
                rejected += 1
                continue
            claimed[identity] = canonical_key
            row["id"] = new_uuid7()
            row["version_id"] = version_id
            row["updated_at"] = loaded_at
            rows.append(row)

        if not rows:
            await _advance_cursor(db, version_id, shard_key)
            await db.commit()
            continue

        for batch, is_last in _batches(rows):
            await db.execute(_upsert(db, batch))
            if is_last:
                # Only a shard that is entirely in advances the cursor; a
                # crash between batches replays the shard, which the upsert
                # makes free.
                await _advance_cursor(db, version_id, shard_key)
            await db.commit()

    return rejected


async def _entry_counts(db: AsyncSession, version_id: UUID) -> tuple[int, int, int]:
    """``(total, mcp, skill)``, counted in the database rather than in memory,
    so a resumed load reports the generation and not this run's share of it."""
    result = await db.execute(
        select(CatalogEntry.kind, func.count())
        .where(CatalogEntry.version_id == version_id)
        .group_by(CatalogEntry.kind)
    )
    by_kind = {kind: int(count) for kind, count in result.all()}
    mcp = by_kind.get("mcp", 0)
    skill = by_kind.get("skill", 0)
    return mcp + skill, mcp, skill


async def _swap(db: AsyncSession, version_id: UUID, counts: tuple[int, int, int]) -> None:
    """Publish the generation. One transaction, two statements, nothing else."""
    total, mcp, skill = counts
    await db.execute(
        update(CatalogVersion)
        .where(CatalogVersion.status == STATUS_ACTIVE)
        .values(status=STATUS_SUPERSEDED)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(CatalogVersion)
        .where(CatalogVersion.id == version_id)
        .values(
            status=STATUS_ACTIVE,
            activated_at=datetime.now(UTC),
            entry_count=total,
            mcp_count=mcp,
            skill_count=skill,
            error="",
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    # Both statements went round the ORM, so every CatalogVersion the session
    # has already loaded still claims the status it had before the swap. A
    # reader sharing this session must see the swap, not the memory of it.
    db.expire_all()


async def load_archive(
    db: AsyncSession,
    archive: CatalogArchive,
    *,
    release_tag: str,
    data_sha256: str,
    source_repo: str,
    asset_url: str,
    reserved_slugs: frozenset[str],
) -> SyncOutcome:
    """Load one verified archive and publish it as the active generation.

    Idempotent: a second call for a release that is already active returns
    ``changed=False`` and writes nothing. Resumable: an interrupted load
    picks up after its shard cursor. Safe: the active generation is not
    touched until the swap, so a reader during a refresh sees the previous
    catalog in full rather than this one in part.
    """
    version_id, resumed = await _open_version(
        db,
        release_tag=release_tag,
        data_sha256=data_sha256,
        source_repo=source_repo,
        asset_url=asset_url,
        sources_lock_json=archive.sources_lock.model_dump(mode="json"),
    )
    if version_id is None:
        current = await active_version(db)
        return SyncOutcome(
            version_id=current.id if current is not None else UUID(int=0),
            release_tag=release_tag,
            data_sha256=data_sha256,
            changed=False,
            entry_count=current.entry_count if current is not None else 0,
            mcp_count=current.mcp_count if current is not None else 0,
            skill_count=current.skill_count if current is not None else 0,
            rejected_count=0,
            resumed=False,
        )

    try:
        rejected = await _load_shards(db, archive, version_id, reserved_slugs=reserved_slugs)
        counts = await _entry_counts(db, version_id)
        await _swap(db, version_id, counts)
    except Exception as error:
        await _mark_failed(db, version_id, error)
        if isinstance(error, CatalogSyncError):
            raise
        if isinstance(error, SQLAlchemyError):
            raise CatalogSyncError(_failure_reason(error)) from None
        raise

    total, mcp, skill = counts
    return SyncOutcome(
        version_id=version_id,
        release_tag=release_tag,
        data_sha256=data_sha256,
        changed=True,
        entry_count=total,
        mcp_count=mcp,
        skill_count=skill,
        rejected_count=rejected,
        resumed=resumed,
    )


async def prune_versions(db: AsyncSession) -> int:
    """Drop every generation past the active one and the newest two after it.

    Two are kept so an operator can see what the last refresh replaced, and
    so rolling back to the previous release is a re-activation rather than a
    re-download. Entries are deleted explicitly: ``ON DELETE CASCADE`` is
    declared, but SQLite only honours it with foreign keys switched on, and
    orphaned entry rows would outlive every reader that could find them.
    """
    doomed = list(
        (
            await db.scalars(
                select(CatalogVersion.id)
                .where(CatalogVersion.status != STATUS_ACTIVE)
                .order_by(CatalogVersion.created_at.desc(), CatalogVersion.id.desc())
                .offset(KEEP_INACTIVE_VERSIONS)
            )
        ).all()
    )
    if not doomed:
        return 0
    await db.execute(delete(CatalogEntry).where(CatalogEntry.version_id.in_(doomed)))
    await db.execute(delete(CatalogVersion).where(CatalogVersion.id.in_(doomed)))
    await db.commit()
    return len(doomed)


__all__ = [
    "BATCH_SIZE",
    "KEEP_INACTIVE_VERSIONS",
    "STATUS_ACTIVE",
    "STATUS_FAILED",
    "STATUS_LOADING",
    "STATUS_SUPERSEDED",
    "SyncOutcome",
    "active_version",
    "load_archive",
    "prune_versions",
]
