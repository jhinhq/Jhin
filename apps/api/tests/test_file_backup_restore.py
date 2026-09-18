"""Offline metadata+blob backups restore archived chats and pinned file history."""

import shutil
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Conversation, ManagedFile, Workspace
from jhin_media.files import FileStore
from jhin_media.managed_files import attachment_content, pin_attachments, publish_file


async def test_archived_file_versions_survive_metadata_and_volume_restore(tmp_path):
    database = tmp_path / "original.db"
    blobs = tmp_path / "original-blobs"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as db:
        workspace = Workspace(name="Backed up", slug="backed-up")
        db.add(workspace)
        await db.flush()
        chat = Conversation(
            workspace_id=workspace.id,
            title="Archived deliverables",
            last_activity_at=datetime.now(UTC),
        )
        db.add(chat)
        await db.flush()
        file = await publish_file(
            db, workspace.id, chat.id, "report.csv", b"total\n42\n", store=FileStore(blobs)
        )
        original_refs = await pin_attachments(db, workspace.id, chat.id, [file.id])
        await publish_file(
            db, workspace.id, chat.id, "report.csv", b"total\n84\n", store=FileStore(blobs)
        )
        chat.status = "archived"
        await db.commit()
        workspace_id, conversation_id, file_id = workspace.id, chat.id, file.id
    await engine.dispose()

    # A consistent self-hosted backup consists of quiesced DB metadata plus the
    # complete managed-files volume. Restore into independent paths/runtimes.
    restored_db = tmp_path / "restored.db"
    restored_blobs = tmp_path / "restored-blobs"
    shutil.copy2(database, restored_db)
    shutil.copytree(blobs, restored_blobs)
    restored_engine = create_async_engine(f"sqlite+aiosqlite:///{restored_db}")
    restored_factory = async_sessionmaker(restored_engine, expire_on_commit=False)
    try:
        async with restored_factory() as db:
            chat = await db.get(Conversation, conversation_id)
            file = await db.get(ManagedFile, file_id)
            assert chat.status == "archived" and file.version == 2
            store = FileStore(restored_blobs)
            before = await attachment_content(db, workspace_id, original_refs, store=store)
            current_refs = await pin_attachments(db, workspace_id, conversation_id, [file_id])
            after = await attachment_content(db, workspace_id, current_refs, store=store)
            assert before[0]["text"].endswith("total\n42\n")
            assert after[0]["text"].endswith("total\n84\n")
            assert store.read(workspace_id, current_refs[0]["sha256"]) == b"total\n84\n"
    finally:
        await restored_engine.dispose()
