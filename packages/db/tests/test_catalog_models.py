"""Catalog index schema invariants (docs/architecture/catalog.md).

The catalog is the first non-workspace reference data in the schema, and the
only place where correctness rests on a *partial* unique index: exactly one
``catalog_version`` may be ``active``, which is what lets a sync fill a whole
new generation while readers keep seeing the old one. These tests run the real
metadata against SQLite, so they also prove the dialect variants hold — the
JSON columns must degrade off JSONB and the partial index must carry a
``sqlite_where`` twin, or ``create_all`` would silently produce a schema the
unit tests cannot exercise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import DateTime, create_engine, func, inspect, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.migrate import alembic_config
from jhin_db.models import CatalogEntry, CatalogIcon, CatalogVersion


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.connect() as conn:
        # SQLite ignores FK clauses unless asked, and the pragma is a no-op
        # inside a transaction. An in-memory database is one static pooled
        # connection, so setting it once here covers every later session.
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.exec_driver_sql("PRAGMA foreign_keys=ON")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


def _version(tag: str, *, status: str = "loading") -> CatalogVersion:
    return CatalogVersion(release_tag=tag, data_sha256=f"{tag:0>64}"[:64], status=status)


def _entry(version_id: UUID, **overrides: Any) -> CatalogEntry:
    defaults: dict[str, Any] = {
        "version_id": version_id,
        "canonical_key": "mcp:registry:example/server",
        "kind": "mcp",
        "slug": "example",
        "name": "Example",
        "category": "Developer tools",
    }
    return CatalogEntry(**{**defaults, **overrides})


async def _add(session: AsyncSession, *rows: Any) -> None:
    session.add_all(rows)
    await session.commit()


async def _rejects(session: AsyncSession, *rows: Any) -> str:
    """Insert rows that must be refused; return the database's complaint."""
    session.add_all(rows)
    with pytest.raises(IntegrityError) as caught:
        await session.commit()
    await session.rollback()
    return str(caught.value)


async def test_both_tables_build_under_create_all(session: AsyncSession) -> None:
    """The JSON columns and the partial index must have non-Postgres variants,
    or every unit test downstream would be running against a schema that only
    exists in production."""
    assert "catalog_version" in Base.metadata.tables
    assert "catalog_entry" in Base.metadata.tables
    assert await session.scalar(select(func.count()).select_from(CatalogVersion)) == 0
    assert await session.scalar(select(func.count()).select_from(CatalogEntry)) == 0


async def test_only_one_version_may_be_active_at_a_time(session: AsyncSession) -> None:
    await _add(session, _version("a", status="active"))
    message = await _rejects(session, _version("b", status="active"))
    assert "catalog_version.status" in message

    # The restriction is on 'active' alone: a generation history of superseded
    # rows, and a sync loading the next one, coexist happily.
    await _add(
        session,
        _version("c", status="superseded"),
        _version("d", status="superseded"),
        _version("e", status="superseded"),
        _version("f", status="loading"),
    )
    active = await session.scalars(select(CatalogVersion).where(CatalogVersion.status == "active"))
    assert [row.release_tag for row in active] == ["a"]


async def test_release_tag_and_digest_are_unique_together(session: AsyncSession) -> None:
    first = CatalogVersion(release_tag="2026.08.28", data_sha256="a" * 64)
    await _add(session, first)
    message = await _rejects(
        session, CatalogVersion(release_tag="2026.08.28", data_sha256="a" * 64)
    )
    assert "release_tag" in message
    # A rebuilt archive under the same tag is a different generation.
    await _add(session, CatalogVersion(release_tag="2026.08.28", data_sha256="b" * 64))


async def test_entries_are_unique_per_generation_but_repeat_across_them(
    session: AsyncSession,
) -> None:
    old = _version("old")
    new = _version("new")
    await _add(session, old, new)
    # Held as plain values: a rejected insert rolls the session back, which
    # expires every loaded row and would turn a later attribute read into IO.
    old_id, new_id = old.id, new.id
    await _add(session, _entry(old_id))

    duplicate_key = await _rejects(session, _entry(old_id, slug="other"))
    assert "canonical_key" in duplicate_key

    duplicate_slug = await _rejects(
        session, _entry(old_id, canonical_key="mcp:registry:other/server")
    )
    assert "slug" in duplicate_slug

    # The same upstream server in the next generation is the normal case.
    await _add(session, _entry(new_id))
    assert await session.scalar(select(func.count()).select_from(CatalogEntry)) == 2

    # kind is part of the slug key, so a skill and a server may share a slug.
    await _add(
        session,
        _entry(
            new_id,
            canonical_key="skill:github:owner/repo/example",
            kind="skill",
            slug="example",
        ),
    )
    assert await session.scalar(select(func.count()).select_from(CatalogEntry)) == 3


async def test_deleting_a_version_takes_its_entries_with_it(session: AsyncSession) -> None:
    doomed = _version("doomed")
    kept = _version("kept")
    await _add(session, doomed, kept)
    await _add(
        session,
        _entry(doomed.id),
        _entry(doomed.id, canonical_key="mcp:registry:second", slug="second"),
        _entry(kept.id),
    )

    await session.delete(doomed)
    await session.commit()

    survivors = await session.scalars(select(CatalogEntry.version_id))
    assert list(survivors) == [kept.id]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("kind", "plugin"),
        ("trust_tier", "self_declared"),
        ("transport", "websocket"),
        ("auth_hint", "basic"),
        ("default_risk", "catastrophic"),
        ("trust_rank", 5),
        ("trust_rank", -1),
    ],
)
async def test_out_of_vocabulary_values_are_refused(
    session: AsyncSession, column: str, value: object
) -> None:
    """The vocabularies are closed in the database, not only in the loader:
    a synced row that invented a trust tier would otherwise reach the UI as an
    unstyled badge and a risk floor nobody mapped."""
    version = _version("vocab")
    await _add(session, version)
    message = await _rejects(session, _entry(version.id, **{column: value}))
    assert "CHECK constraint failed" in message


async def test_row_defaults_are_the_conservative_ones(session: AsyncSession) -> None:
    version = _version("defaults")
    await _add(session, version)
    entry = _entry(version.id)
    await _add(session, entry)

    assert version.status == "loading"
    assert version.shard_cursor == ""
    assert (version.entry_count, version.mcp_count, version.skill_count) == (0, 0, 0)
    assert version.sources_lock_json == {}
    assert version.activated_at is None

    # An unqualified entry is the least trusted, most-gated thing it can be.
    assert entry.trust_tier == "indexed"
    assert entry.trust_rank == 4
    assert entry.default_risk == "elevated"
    assert entry.icon_url == ""
    assert entry.url_unverified is True
    assert entry.transport == "unknown"
    assert entry.auth_hint == "bearer"
    assert entry.publishable is False
    assert entry.stdio_only is False
    assert entry.deprecated is False
    assert entry.icon == "mcp"
    assert entry.popularity == 0.0
    assert (entry.tags_json, entry.alias_keys_json, entry.sources_json) == ([], [], [])
    assert (entry.connector_config_json, entry.mcp_json, entry.skill_json) == ({}, {}, {})
    assert entry.search_text == ""


async def test_the_reviewed_tier_is_inside_the_vocabulary(session: AsyncSession) -> None:
    """``reviewed`` sits between ``smithery_verified`` and ``indexed``: a
    skill from a library the Jhin team looked at. Both the tier and its rank
    must pass the checks, or the sync's election would take a refresh down."""
    version = _version("reviewed")
    await _add(session, version)
    await _add(
        session,
        _entry(
            version.id,
            canonical_key="skill:github:acme/skills/notes",
            kind="skill",
            slug="notes",
            trust_tier="reviewed",
            trust_rank=3,
            icon_url="https://github.com/acme.png?size=128",
        ),
    )

    row = await session.scalar(select(CatalogEntry).where(CatalogEntry.slug == "notes"))
    assert row is not None
    assert (row.trust_tier, row.trust_rank) == ("reviewed", 3)
    assert row.icon_url == "https://github.com/acme.png?size=128"


async def test_the_icon_cache_holds_one_row_per_slug(session: AsyncSession) -> None:
    """The proxy's cache is keyed by the handle a browser asks for, and two
    fetches racing for one slug must collapse onto one row rather than two."""
    icon = CatalogIcon(
        slug="acme_notes",
        source_url="https://github.com/acme.png?size=128",
        content_type="image/webp",
        body=b"webp-bytes",
        status="ok",
    )
    await _add(session, icon)

    assert icon.status == "ok"
    assert icon.fetched_at is None

    message = await _rejects(session, CatalogIcon(slug="acme_notes"))
    assert "slug" in message

    pending = CatalogIcon(slug="other_app")
    await _add(session, pending)
    assert pending.status == "pending"
    assert pending.body is None
    assert (pending.source_url, pending.content_type) == ("", "")


def test_migration_0034_round_trips_on_sqlite(tmp_path: Path) -> None:
    """0033 builds the catalog tables from nothing and 0034 alters them, so
    the pair runs on a database stamped at 0032 — the earlier chain uses
    Postgres-only DDL and is exercised elsewhere. Up widens the vocabulary and
    adds the cache; down demotes what only the wider vocabulary allowed, then
    restores the narrower checks exactly."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog.sqlite'}"
    sync_engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    config = alembic_config(url)
    command.stamp(config, "0032")
    # 0034, not "head": this database was stamped rather than built, so the
    # tables the earlier Postgres-only chain would have created do not exist
    # and later revisions that alter them cannot run here. The catalog pair is
    # what this test is about.
    command.upgrade(config, "0034")

    inspector = inspect(sync_engine)
    assert "catalog_icon" in inspector.get_table_names()
    assert "icon_url" in {column["name"] for column in inspector.get_columns("catalog_entry")}
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("catalog_entry")
    }
    assert "'reviewed'" in checks["ck_catalog_entry_trust_tier"]
    assert checks["ck_catalog_entry_trust_rank_range"] == "trust_rank BETWEEN 0 AND 4"

    with sync_engine.begin() as conn:
        conn.execute(
            sa_text(
                "INSERT INTO catalog_version (id, release_tag, data_sha256, status) "
                "VALUES (:id, 'tag', 'digest', 'active')"
            ),
            {"id": uuid4().bytes},
        )
        version_id = conn.execute(sa_text("SELECT id FROM catalog_version")).scalar_one()
        conn.execute(
            sa_text(
                "INSERT INTO catalog_entry "
                "(id, version_id, canonical_key, kind, slug, name, category, "
                " trust_tier, trust_rank) "
                "VALUES (:id, :version_id, 'skill:github:acme/skills/x', 'skill', 'x', 'X', "
                "'Productivity', 'reviewed', 3)"
            ),
            {"id": uuid4().bytes, "version_id": version_id},
        )
    sync_engine.dispose()

    command.downgrade(config, "0033")

    inspector = inspect(sync_engine)
    assert "catalog_icon" not in inspector.get_table_names()
    assert "icon_url" not in {column["name"] for column in inspector.get_columns("catalog_entry")}
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("catalog_entry")
    }
    assert "'reviewed'" not in checks["ck_catalog_entry_trust_tier"]
    assert checks["ck_catalog_entry_trust_rank_range"] == "trust_rank BETWEEN 0 AND 3"
    with sync_engine.connect() as conn:
        row = conn.execute(sa_text("SELECT trust_tier, trust_rank FROM catalog_entry")).one()
    assert tuple(row) == ("indexed", 3), "downgrade must demote, not refuse to apply"
    sync_engine.dispose()

    command.upgrade(config, "0034")


async def test_ids_are_time_ordered_uuids_and_timestamps_are_declared_utc(
    session: AsyncSession,
) -> None:
    version = _version("ids")
    await _add(session, version)
    first = _entry(version.id)
    await _add(session, first)
    second = _entry(version.id, canonical_key="mcp:registry:later", slug="later")
    await _add(session, second)

    for row in (version, first, second):
        assert type(row.id) is UUID
    assert first.id.hex < second.id.hex, "UUIDv7 keys must sort in insertion order"

    # SQLite hands back naive datetimes whatever the column says, so the
    # timezone promise is asserted where it is actually made: on the schema.
    for table in (CatalogVersion.__table__, CatalogEntry.__table__):
        for name in ("created_at", "updated_at"):
            column = table.c[name]
            assert isinstance(column.type, DateTime)
            assert column.type.timezone is True
            assert column.server_default is not None
    assert version.created_at is not None and first.updated_at is not None
