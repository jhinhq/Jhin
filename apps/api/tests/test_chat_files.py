"""Immutable files, revision conflicts, scoped context, and archive retention."""

from datetime import UTC, datetime

import pytest

from jhin_db.models import Conversation
from jhin_media.files import FileStore
from jhin_media.managed_files import (
    FileAccessError,
    attachment_content,
    pin_attachments,
    publish_file,
)


async def conversation(session, admin_ctx):
    row = Conversation(
        workspace_id=admin_ctx.workspace_id, title="Files", last_activity_at=datetime.now(UTC)
    )
    session.add(row)
    await session.flush()
    return row


async def test_file_revisions_pin_old_input_and_refuse_stale_saves(session, admin_ctx, tmp_path):
    chat = await conversation(session, admin_ctx)
    store = FileStore(tmp_path)
    file = await publish_file(
        session, admin_ctx.workspace_id, chat.id, "notes.txt", b"first", store=store
    )
    original = file.current_revision_id
    refs = await pin_attachments(session, admin_ctx.workspace_id, chat.id, [file.id])
    await publish_file(
        session,
        admin_ctx.workspace_id,
        chat.id,
        "notes.txt",
        b"second",
        store=store,
        expected_revision_id=original,
    )
    assert file.current_revision_id != original
    assert file.version == 2
    assert (await attachment_content(session, admin_ctx.workspace_id, refs, store=store))[0][
        "text"
    ].endswith("first")
    with pytest.raises(FileAccessError, match="changed"):
        await publish_file(
            session,
            admin_ctx.workspace_id,
            chat.id,
            "notes.txt",
            b"stale",
            store=store,
            expected_revision_id=original,
        )


async def test_cross_workspace_and_forged_revision_refs_are_rejected(session, admin_ctx, tmp_path):
    from uuid import uuid4

    chat = await conversation(session, admin_ctx)
    file = await publish_file(
        session, admin_ctx.workspace_id, chat.id, "notes.txt", b"private", store=FileStore(tmp_path)
    )
    with pytest.raises(FileAccessError):
        await pin_attachments(session, uuid4(), chat.id, [file.id])
    refs = await pin_attachments(session, admin_ctx.workspace_id, chat.id, [file.id])
    refs[0]["revision_id"] = str(uuid4())
    with pytest.raises(FileAccessError):
        await attachment_content(session, admin_ctx.workspace_id, refs, store=FileStore(tmp_path))


async def test_archiving_preserves_published_files(session, admin_ctx, tmp_path):
    chat = await conversation(session, admin_ctx)
    file = await publish_file(
        session,
        admin_ctx.workspace_id,
        chat.id,
        "notes.txt",
        b"retained",
        store=FileStore(tmp_path),
    )
    chat.status = "archived"
    await session.flush()
    refs = await pin_attachments(session, admin_ctx.workspace_id, chat.id, [file.id])
    assert (
        await attachment_content(session, admin_ctx.workspace_id, refs, store=FileStore(tmp_path))
    )[0]["text"].endswith("retained")


@pytest.fixture
async def client(session, admin_ctx, tmp_path, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from jhin_api.chat_files.router import router
    from jhin_api.deps import AdminCtx, MemberCtx, ViewerCtx, get_db
    from jhin_api.security.csrf import csrf_protect

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(router)

    async def context():
        return admin_ctx

    async def database():
        yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[csrf_protect] = lambda: None
    for annotation in (AdminCtx, MemberCtx, ViewerCtx):
        app.dependency_overrides[annotation.__metadata__[0].dependency] = context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_upload_preview_download_and_invalid_content(client, session, admin_ctx):
    chat = await conversation(session, admin_ctx)
    prefix = f"/api/v1/workspaces/{admin_ctx.workspace_id}"
    uploaded = await client.post(
        f"{prefix}/conversations/{chat.id}/files",
        files={"file": ("app.html", b"<script>alert(1)</script>", "text/html")},
    )
    assert uploaded.status_code == 201, uploaded.text
    file = uploaded.json()
    assert file["status"] == "ready"
    preview = await client.get(file["preview_url"])
    assert preview.headers["content-type"].startswith("text/plain")
    assert "sandbox" in preview.headers["content-security-policy"]
    assert preview.text == "<script>alert(1)</script>"
    download = await client.get(file["download_url"])
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"
    invalid = await client.post(
        f"{prefix}/conversations/{chat.id}/files",
        files={"file": ("fake.png", b"not image", "image/png")},
    )
    assert invalid.status_code == 422
    traversal = await client.post(
        f"{prefix}/conversations/{chat.id}/files",
        files={"file": ("secret.txt", b"no", "text/plain")},
        data={"path": "../secret.txt"},
    )
    assert traversal.status_code == 422


async def test_stale_disk_save_does_not_publish_new_revision(
    client, session, admin_ctx, monkeypatch
):
    from fastapi import HTTPException

    from jhin_api.chat_files import service

    chat = await conversation(session, admin_ctx)
    file = await publish_file(session, admin_ctx.workspace_id, chat.id, "notes.txt", b"initial")
    initial = file.current_revision_id

    async def disk_conflict(db, ctx, conversation_id, operation, args, **kwargs):
        assert operation == "write"
        assert args["expected_sha256"]
        assert kwargs["expected_generation"] == 7
        raise HTTPException(409, "Workspace file changed")

    monkeypatch.setattr(service, "workspace_operation", disk_conflict)
    result = await client.put(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/files/{file.id}/content",
        json={"content": "overwrite", "expected_revision_id": str(initial), "lease_generation": 7},
    )
    assert result.status_code == 409
    await session.refresh(file)
    assert file.current_revision_id == initial
    assert file.version == 1


async def test_checkpoint_restore_uses_actual_disk_hash_and_preserves_version_history(
    client, session, admin_ctx, monkeypatch
):
    import base64
    import hashlib

    from jhin_api.chat_files import service

    chat = await conversation(session, admin_ctx)
    disk = {"notes.txt": b"first"}

    async def runtime(db, ctx, conversation_id, operation, args, **kwargs):
        if operation == "snapshot":
            return {
                "files": [
                    {
                        "path": path,
                        "content_base64": base64.b64encode(data).decode(),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for path, data in disk.items()
                ],
                "excluded": [],
            }
        assert operation == "restore"
        for item in args["files"]:
            assert item["expected_sha256"] == hashlib.sha256(disk[item["path"]]).hexdigest()
        for item in args["files"]:
            disk[item["path"]] = base64.b64decode(item["content_base64"])
        return {"restored": list(disk)}

    monkeypatch.setattr(service, "workspace_operation", runtime)
    prefix = f"/api/v1/workspaces/{admin_ctx.workspace_id}/conversations/{chat.id}"
    checkpoint = await client.post(f"{prefix}/checkpoints", json={"label": "Before edits"})
    assert checkpoint.status_code == 201, checkpoint.text
    disk["notes.txt"] = b"second"
    changes = (await client.get(f"{prefix}/changes")).json()
    entry = changes["items"][0]
    assert entry["current_sha256"] == hashlib.sha256(b"second").hexdigest()
    assert "-first" in entry["diff"] and "+second" in entry["diff"]
    restore = await client.post(
        f"{prefix}/checkpoints/{checkpoint.json()['id']}/restore",
        json={
            "paths": ["notes.txt"],
            "expected_revisions": {"notes.txt": entry["current_sha256"]},
            "lease_generation": 3,
        },
    )
    assert restore.status_code == 200, restore.text
    assert disk["notes.txt"] == b"first"


async def test_project_archive_retains_config_and_rejects_embedded_credentials(client, admin_ctx):
    prefix = f"/api/v1/workspaces/{admin_ctx.workspace_id}/projects"
    invalid = await client.post(
        prefix, json={"name": "Secret", "repository_url": "https://token@example.com/repo"}
    )
    assert invalid.status_code == 422
    result = await client.post(
        prefix,
        json={
            "name": "Project",
            "repository_url": "https://github.com/example/repo",
            "context": "Use Python",
        },
    )
    assert result.status_code == 201, result.text
    project = result.json()
    assert (await client.delete(f"{prefix}/{project['id']}")).status_code == 204
    assert (await client.get(prefix)).json() == []
    assert (await client.get(f"{prefix}/{project['id']}")).json()["context"] == "Use Python"


async def test_branch_snapshot_copies_versions_without_mutating_source(
    session, admin_ctx, tmp_path
):
    from sqlalchemy import select

    from jhin_db.models import FileCheckpoint, ManagedFile
    from jhin_media.managed_files import clone_checkpoint_files

    source = await conversation(session, admin_ctx)
    destination = await conversation(session, admin_ctx)
    file = await publish_file(
        session,
        admin_ctx.workspace_id,
        source.id,
        "notes.txt",
        b"before",
        store=FileStore(tmp_path),
    )
    before = file.current_revision_id
    checkpoint = FileCheckpoint(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=source.id,
        label="Original",
        manifest_json={file.path: str(before)},
        excluded_json=[],
    )
    session.add(checkpoint)
    await session.flush()
    await publish_file(
        session, admin_ctx.workspace_id, source.id, "notes.txt", b"after", store=FileStore(tmp_path)
    )
    cloned = await clone_checkpoint_files(
        session, admin_ctx.workspace_id, source.id, destination.id, checkpoint.id
    )
    assert cloned.manifest_json["notes.txt"] != str(before)
    branch_file = await session.scalar(
        select(ManagedFile).where(ManagedFile.conversation_id == destination.id)
    )
    refs = await pin_attachments(session, admin_ctx.workspace_id, destination.id, [branch_file.id])
    assert (
        await attachment_content(session, admin_ctx.workspace_id, refs, store=FileStore(tmp_path))
    )[0]["text"].endswith("before")
    assert file.version == 2
    with pytest.raises(FileAccessError, match="already contains"):
        await clone_checkpoint_files(
            session, admin_ctx.workspace_id, source.id, destination.id, checkpoint.id
        )


async def test_branch_attachments_own_revisions_and_reuse_mapping(session, admin_ctx, tmp_path):
    from sqlalchemy import delete, select

    from jhin_db.models import FileRevision, ManagedFile
    from jhin_media.managed_files import clone_attachment_references

    source = await conversation(session, admin_ctx)
    destination = await conversation(session, admin_ctx)
    file = await publish_file(
        session,
        admin_ctx.workspace_id,
        source.id,
        "report.csv",
        b"amount\n42\n",
        store=FileStore(tmp_path),
    )
    refs = await pin_attachments(session, admin_ctx.workspace_id, source.id, [file.id])
    copied = await clone_attachment_references(
        session, admin_ctx.workspace_id, destination.id, refs
    )
    assert copied[0]["id"] != refs[0]["id"]
    assert copied[0]["revision_id"] != refs[0]["revision_id"]
    assert copied[0]["sha256"] == refs[0]["sha256"]
    assert copied == await clone_attachment_references(
        session, admin_ctx.workspace_id, destination.id, refs
    )
    assert (
        len(
            (
                await session.scalars(
                    select(ManagedFile).where(ManagedFile.conversation_id == destination.id)
                )
            ).all()
        )
        == 1
    )
    await session.execute(delete(FileRevision).where(FileRevision.file_id == file.id))
    await session.delete(file)
    await session.flush()
    assert (
        await attachment_content(session, admin_ctx.workspace_id, copied, store=FileStore(tmp_path))
    )[0]["text"].endswith("amount\n42\n")


async def test_saved_project_source_survives_source_chat_deletion(
    session, admin_ctx, tmp_path, monkeypatch
):
    import base64

    from sqlalchemy import delete, select

    from jhin_api.runtime.router import ProjectSourceIn, save_project_source
    from jhin_connectors.cli import chat_snapshots
    from jhin_db.models import ChatProject, FileCheckpoint, FileRevision, ManagedFile

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    source = await conversation(session, admin_ctx)
    destination = await conversation(session, admin_ctx)
    project = ChatProject(workspace_id=admin_ctx.workspace_id, name="Retained project")
    session.add(project)
    await session.flush()
    file = await publish_file(
        session, admin_ctx.workspace_id, source.id, "notes.txt", b"retained project bytes"
    )
    checkpoint = FileCheckpoint(
        workspace_id=admin_ctx.workspace_id,
        conversation_id=source.id,
        label="Project source",
        manifest_json={file.path: str(file.current_revision_id)},
    )
    session.add(checkpoint)
    await session.flush()
    await save_project_source(
        project.id,
        ProjectSourceIn(conversation_id=source.id, checkpoint_id=checkpoint.id),
        admin_ctx,
        session,
    )
    await session.delete(checkpoint)
    await session.execute(delete(FileRevision).where(FileRevision.file_id == file.id))
    await session.delete(file)
    await session.delete(source)
    destination.project_id = project.id
    await session.flush()
    staged = {}

    async def operation(key, operation, args):
        assert key == "destination" and operation == "stage"
        staged[args["path"]] = base64.b64decode(args["content_base64"])
        return {}

    monkeypatch.setattr(chat_snapshots, "workspace_file_operation", operation)
    await chat_snapshots.seed_project_source(session, destination, "destination")
    assert staged == {"notes.txt": b"retained project bytes"}
    # A branch inherits the project context but must keep its own checkpoint.
    staged.clear()
    destination.source_conversation_id = source.id
    await chat_snapshots.seed_project_source(session, destination, "destination")
    assert staged == {}
    assert (
        await session.scalars(
            select(ManagedFile).where(ManagedFile.conversation_id == destination.id)
        )
    ).all() == []


async def test_edit_uploaded_text_creates_absent_working_path_and_refuses_collisions(
    client, session, admin_ctx, monkeypatch
):
    import base64
    import hashlib

    from jhin_api.chat_files import service

    chat = await conversation(session, admin_ctx)
    prefix = f"/api/v1/workspaces/{admin_ctx.workspace_id}"
    uploaded = (
        await client.post(
            f"{prefix}/conversations/{chat.id}/files",
            files={"file": ("notes.txt", b"uploaded source", "text/plain")},
        )
    ).json()
    disk = {}
    writes = []

    async def runtime(db, ctx, conversation_id, operation, args, **kwargs):
        if operation == "read":
            assert args.get("allow_missing") is True
            if args["path"] not in disk:
                return {"missing": True, "path": args["path"]}
            return {"sha256": hashlib.sha256(disk[args["path"]]).hexdigest()}
        assert operation == "write" and kwargs["expected_generation"] == 2
        expected = hashlib.sha256(disk[args["path"]]).hexdigest() if args["path"] in disk else None
        assert args["expected_sha256"] == expected
        writes.append(args)
        disk[args["path"]] = base64.b64decode(args["content_base64"])
        return {}

    monkeypatch.setattr(service, "workspace_operation", runtime)
    edited = await client.put(
        f"{prefix}/files/{uploaded['id']}/content",
        json={
            "content": "edited source",
            "expected_revision_id": uploaded["current_revision_id"],
            "lease_generation": 2,
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2 and disk["notes.txt"] == b"edited source"
    assert writes[0]["expected_sha256"] is None
    disk["notes.txt"] = b"unpublished newer contents"
    conflict = await client.put(
        f"{prefix}/files/{uploaded['id']}/content",
        json={
            "content": "overwrite",
            "expected_revision_id": edited.json()["current_revision_id"],
            "lease_generation": 2,
        },
    )
    assert conflict.status_code == 409
    assert disk["notes.txt"] == b"unpublished newer contents" and len(writes) == 1


async def test_artifact_download_keeps_extension_when_display_title_has_none(
    client, session, admin_ctx
):
    chat = await conversation(session, admin_ctx)
    file = await publish_file(
        session, admin_ctx.workspace_id, chat.id, "results.csv", b"total\n60\n"
    )
    file.name = "Sales results"
    await session.flush()
    response = await client.get(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/files/{file.id}/download"
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith("results.csv")
    assert response.content == b"total\n60\n"
