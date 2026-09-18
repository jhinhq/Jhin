"""The migration acceptance follows the revision graph as new revisions land."""

from importlib import import_module
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_verifier_tracks_new_head_and_rejects_stale_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)
    verifier = import_module("scripts.verify_work_readiness_migrations")
    versions = tmp_path / "versions"
    versions.mkdir()
    previous = None
    for revision in ("0051", "0057", "0060"):
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {previous!r}\n", encoding="utf-8"
        )
        previous = revision
    config = Config()
    config.set_main_option("script_location", str(tmp_path))
    expected = verifier.migration_head(config)
    assert expected == "0060"

    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as db:
            await db.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
            await db.execute(text("INSERT INTO alembic_version VALUES ('0051')"))
            with pytest.raises(AssertionError, match="0060"):
                await verifier.verify_database_head(db, expected)
            await db.execute(text("UPDATE alembic_version SET version_num = '0060'"))
            await verifier.verify_database_head(db, expected)
            await db.execute(text("INSERT INTO alembic_version VALUES ('0057')"))
            with pytest.raises(AssertionError):
                await verifier.verify_database_head(db, expected)
    finally:
        await engine.dispose()
