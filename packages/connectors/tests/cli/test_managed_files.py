"""Chat inputs and artifact results cross the same guarded file boundary."""

import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from jhin_connectors.cli import managed_files
from jhin_connectors.cli.managed_files import FilePublishInput, file_publish, stage_chat_inputs
from jhin_db.models import AuditEvent, Conversation, ManagedFile, Message, Task
from jhin_media.files import FileStore
from jhin_media.managed_files import pin_attachments, publish_file
from jhin_tools.builtin import ToolExecutionContext


async def context(session, workspace):
    chat = Conversation(
        workspace_id=workspace.id, title="Files", last_activity_at=datetime.now(UTC)
    )
    session.add(chat)
    await session.flush()
    task = Task(
        workspace_id=workspace.id,
        conversation_id=chat.id,
        title="Files",
        correlation_id=uuid4(),
        metadata_json={},
    )
    session.add(task)
    await session.flush()
    ctx = ToolExecutionContext(
        session=session,
        workspace_id=workspace.id,
        task_id=task.id,
        run_id=uuid4(),
        agent_id=uuid4(),
        agent_name="Bisby",
        session_factory=async_sessionmaker(session.bind, expire_on_commit=False),
    )
    return chat, task, ctx


async def test_input_staging_is_pinned_and_once_per_run(session, workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    chat, task, ctx = await context(session, workspace)
    file = await publish_file(
        session, workspace.id, chat.id, "data.csv", b"a,b\n1,2", kind="upload"
    )
    refs = await pin_attachments(session, workspace.id, chat.id, [file.id])
    task.metadata_json = {"attachments": refs}
    await session.commit()
    requests = []

    async def stage(key, operation, args):
        requests.append((key, operation, args))
        return {"staged": True}

    monkeypatch.setattr(managed_files, "workspace_file_operation", stage)
    await stage_chat_inputs(ctx, "chat-one")
    await stage_chat_inputs(ctx, "chat-one")
    assert len(requests) == 1
    assert requests[0][1] == "stage"
    assert requests[0][2]["path"] == refs[0]["mounted_path"]
    assert base64.b64decode(requests[0][2]["content_base64"]) == b"a,b\n1,2"
    receipts = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "chat.input.staged"))
    )
    assert len(receipts) == 1
    assert "content_base64" not in receipts[0].metadata_json


async def test_staging_covers_twenty_refs_but_excludes_future_queue(
    session, workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    chat, task, ctx = await context(session, workspace)
    ids = []
    for index in range(20):
        file = await publish_file(
            session, workspace.id, chat.id, f"input-{index}.txt", str(index).encode()
        )
        ids.append(file.id)
    refs = await pin_attachments(session, workspace.id, chat.id, ids)
    task.metadata_json = {"attachments": refs}
    future = await publish_file(session, workspace.id, chat.id, "future.txt", b"not consumed")
    future_refs = await pin_attachments(session, workspace.id, chat.id, [future.id])
    for task_id, delivery in [(uuid4(), "queued"), (task.id, "pending")]:
        session.add(
            Message(
                workspace_id=workspace.id,
                conversation_id=chat.id,
                task_id=task_id,
                sender_type="user",
                recipient_type="agent",
                message_type="instruction",
                content_json={"attachments": future_refs, "delivery": delivery},
                visibility="visible",
            )
        )
    await session.commit()
    staged = []

    async def stage(key, operation, args):
        staged.append(args["path"])
        return {}

    monkeypatch.setattr(managed_files, "workspace_file_operation", stage)
    await stage_chat_inputs(ctx, "current-chat")
    assert set(staged) == {ref["mounted_path"] for ref in refs}


@pytest.mark.parametrize("source_deleted", [False, True])
async def test_branch_keeps_its_checkpoint_instead_of_inherited_project_source(
    session, workspace, tmp_path, monkeypatch, source_deleted
):
    from sqlalchemy import delete

    from jhin_connectors.cli import chat_snapshots
    from jhin_db.models import ChatProject

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    project_files = {"notes.txt": b"project starter", "project-only.txt": b"starter context"}
    checkpoint_id = uuid4()
    project = ChatProject(
        workspace_id=workspace.id,
        name="Inherited project",
        source_revision=f"checkpoint:{checkpoint_id}",
        source_manifest_json={
            "schema_version": 1,
            "checkpoint_id": str(checkpoint_id),
            "files": [
                {
                    "path": path,
                    "sha256": FileStore().put(workspace.id, data),
                    "size_bytes": len(data),
                }
                for path, data in project_files.items()
            ],
        },
    )
    session.add(project)
    await session.flush()
    original = Conversation(
        workspace_id=workspace.id,
        project_id=project.id,
        title="Original chat",
        last_activity_at=datetime.now(UTC),
    )
    session.add(original)
    await session.flush()
    branch = Conversation(
        workspace_id=workspace.id,
        project_id=project.id,
        title="Fresh branch",
        last_activity_at=datetime.now(UTC),
        source_conversation_id=original.id,
        source_checkpoint_id=uuid4(),
    )
    ordinary = Conversation(
        workspace_id=workspace.id,
        project_id=project.id,
        title="New project chat",
        last_activity_at=datetime.now(UTC),
    )
    session.add_all([branch, ordinary])
    await session.flush()
    if source_deleted:
        session.add(
            AuditEvent(
                workspace_id=workspace.id,
                actor_type="user",
                action="chat.project.seeded",
                target_type="conversation",
                target_id=branch.id,
                metadata_json={
                    "source_kind": "branch",
                    "source_checkpoint_id": str(branch.source_checkpoint_id),
                },
            )
        )
        original_id = original.id
        await session.execute(delete(Conversation).where(Conversation.id == original_id))
        # Also clear the ancestry hint to exercise the persisted receipt alone.
        # Deletion currently retains this non-FK ID, so leaving it populated
        # would merely re-test the early-return guard and mask a lost receipt.
        branch.source_conversation_id = None
        assert await session.get(Conversation, original_id) is None
    await session.commit()
    receipts = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.target_id == branch.id,
                AuditEvent.action == "chat.project.seeded",
            )
        )
    )
    assert len(receipts) == int(source_deleted)
    branch_bytes = {"notes.txt": b"selected branch checkpoint", "branch-only.txt": b"branch work"}
    disks = {"branch": dict(branch_bytes), "ordinary": {}}
    calls = []

    async def stage(key, operation, args):
        assert operation == "stage"
        calls.append(key)
        disks[key][args["path"]] = base64.b64decode(args["content_base64"])
        return {}

    monkeypatch.setattr(chat_snapshots, "workspace_file_operation", stage)
    await chat_snapshots.seed_project_source(session, branch, "branch")
    assert calls == []
    assert disks["branch"] == branch_bytes
    # The inherited source is valid and nonempty: without either branch guard,
    # it would stage different bytes and an extra file into the branch disk.
    await chat_snapshots.seed_project_source(session, ordinary, "ordinary")
    assert disks["ordinary"] == project_files
    assert calls == ["ordinary", "ordinary"]


async def test_publication_returns_verified_durable_download(
    session, workspace, tmp_path, monkeypatch
):
    from jhin_connectors.cli import tools

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    chat, _task, ctx = await context(session, workspace)

    async def connection(*args):
        return None

    async def binding(*args):
        return SimpleNamespace(key="chat-one", kind="conversation")

    async def read(key, operation, args):
        assert operation == "read" and args == {"path": "report.md"}
        return {
            "content_base64": base64.b64encode(b"# Report\nDone").decode(),
            "sha256": hashlib.sha256(b"# Report\nDone").hexdigest(),
        }

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(tools, "_binding", binding)
    monkeypatch.setattr(managed_files, "workspace_file_operation", read)
    output = await file_publish(ctx, FilePublishInput(connection_id=str(uuid4()), path="report.md"))
    assert output.preview_kind == "text"
    assert output.version == 1
    file = await session.scalar(select(ManagedFile).where(ManagedFile.conversation_id == chat.id))
    assert file.kind == "artifact"
    assert FileStore().read(workspace.id, output.sha256) == b"# Report\nDone"
    repeated = await file_publish(
        ctx, FilePublishInput(connection_id=str(uuid4()), path="report.md")
    )
    assert repeated.revision_id == output.revision_id


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", ".git/config", "nested/.git/config"]
)
def test_artifact_publication_refuses_outside_paths(path):
    with pytest.raises(ValueError):
        FilePublishInput(connection_id=str(uuid4()), path=path)
