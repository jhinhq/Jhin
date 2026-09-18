import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Conversation,
    RuntimeSession,
    SandboxWorkspace,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_tool_worker import runtime_gateway as gateway

TOKEN = "test-only-capability-value-at-least-thirty-two-characters"


async def _branch_seed_source(db, runtime):
    row = await db.get(RuntimeSession, runtime.row.id)
    row.config_json = {"action": "seed_branch", "write": False}
    source = Conversation(
        workspace_id=row.workspace_id,
        title="Branch source",
        last_activity_at=datetime.now(UTC),
    )
    db.add(source)
    await db.flush()
    return row, source


async def test_empty_branch_checkpoint_succeeds_without_calling_runner(runtime, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(gateway, "runner_request", request)
    async with runtime.factory() as db:
        row, source = await _branch_seed_source(db, runtime)
        result = await gateway.execute_operation(
            db, row, {"source_conversation_id": str(source.id), "manifest": {}}
        )
    assert result == {"restored": []}
    request.assert_not_awaited()


async def test_empty_preview_still_requires_published_source_files(runtime, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(gateway, "runner_request", request)
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        row.config_json = {"manifest": {}}
        with pytest.raises(HTTPException) as error:
            await gateway.seed_preview(db, row)
    assert error.value.status_code == 422
    assert error.value.detail == "Preview has no published source files"
    request.assert_not_awaited()


@pytest.mark.parametrize("source_exists", [False, True])
async def test_empty_branch_still_validates_source_workspace(runtime, monkeypatch, source_exists):
    from jhin_domain import new_uuid7

    request = AsyncMock()
    monkeypatch.setattr(gateway, "runner_request", request)
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        row.config_json = {"action": "seed_branch", "write": False}
        source_id = new_uuid7()
        if source_exists:
            foreign = Workspace(name="Foreign source", slug="foreign-source")
            db.add(foreign)
            await db.flush()
            db.add(
                Conversation(
                    id=source_id,
                    workspace_id=foreign.id,
                    title="Foreign chat",
                    last_activity_at=datetime.now(UTC),
                )
            )
            await db.flush()
        with pytest.raises(HTTPException) as error:
            await gateway.execute_operation(
                db, row, {"source_conversation_id": str(source_id), "manifest": {}}
            )
    assert error.value.status_code == 404
    request.assert_not_awaited()


async def test_nonempty_branch_restores_exact_checkpoint_files(runtime, monkeypatch, tmp_path):
    import base64

    from jhin_db.models import FileRevision, ManagedFile

    monkeypatch.setenv("JHIN_FILES_ROOT", str(tmp_path))
    request = AsyncMock(return_value={"restored": ["README.md"]})
    monkeypatch.setattr(gateway, "runner_request", request)
    data = b"Recorded branch source.\n"
    async with runtime.factory() as db:
        row, source = await _branch_seed_source(db, runtime)
        file = ManagedFile(
            workspace_id=row.workspace_id,
            conversation_id=source.id,
            name="README.md",
            path="README.md",
        )
        db.add(file)
        await db.flush()
        revision = FileRevision(
            workspace_id=row.workspace_id,
            file_id=file.id,
            version=1,
            sha256=gateway.FileStore().put(row.workspace_id, data),
            size_bytes=len(data),
            mime_type="text/markdown",
            preview_kind="text",
        )
        db.add(revision)
        await db.flush()
        result = await gateway.execute_operation(
            db,
            row,
            {"source_conversation_id": str(source.id), "manifest": {file.path: str(revision.id)}},
        )
    assert result == {"restored": ["README.md"]}
    request.assert_awaited_once_with(
        "POST",
        f"/v1/workspaces/{runtime.binding.workspace_key}/operation",
        {
            "operation": "restore",
            "args": {
                "files": [
                    {
                        "path": "README.md",
                        "content_base64": base64.b64encode(data).decode(),
                        "expected_sha256": None,
                    }
                ]
            },
        },
    )


@pytest.mark.parametrize("size,state", [(1024 * 1024 + 1, "measured"), (0, "unknown")])
async def test_gateway_rechecks_quota_after_capability_was_minted(
    runtime, monkeypatch, size, state
):
    monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
    request = AsyncMock(return_value={})
    monkeypatch.setattr(gateway, "runner_request", request)
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        binding = await db.get(SandboxWorkspace, runtime.binding.id)
        binding.size_bytes, binding.size_state = size, state
        await db.flush()
        with pytest.raises(HTTPException) as error:
            await gateway.execute_operation(
                db, row, {"operation": "write", "args": {"content_base64": "eA=="}}
            )
        assert error.value.status_code == 507
        request.assert_not_awaited()


async def test_terminal_input_quota_guard_preserves_recovery_authority(runtime, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        binding = await db.get(SandboxWorkspace, runtime.binding.id)
        binding.size_bytes = 1024 * 1024 + 1
        await db.flush()
        with pytest.raises(HTTPException) as error:
            await gateway.binding_for(db, row, write=True, grow=True, lock=True)
        assert error.value.status_code == 507
        # Status/output reads and explicit interrupt/resize/stop remain fenced
        # and authorized, but do not require room to add more working files.
        assert await gateway.binding_for(db, row, write=False) is binding
        assert await gateway.binding_for(db, row, write=True, grow=False, lock=True) is binding
        binding.holder_user_id = None
        with pytest.raises(HTTPException) as revoked:
            await gateway.binding_for(db, row, write=True, grow=False, lock=True)
        assert revoked.value.status_code == 409


async def test_human_quota_counts_all_tenant_disks_without_cross_tenant_charge(
    runtime, monkeypatch
):
    monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
    monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "2")
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        binding = await db.get(SandboxWorkspace, runtime.binding.id)
        binding.size_bytes = 1024 * 1024
        foreign = Workspace(name="Other", slug="other")
        db.add(foreign)
        await db.flush()
        db.add(SandboxWorkspace(workspace_id=foreign.id, workspace_key="foreign", size_bytes=10**9))
        await db.flush()
        assert await gateway.binding_for(db, row, write=True, grow=True) is binding
        db.add(
            SandboxWorkspace(
                workspace_id=row.workspace_id,
                workspace_key="retained",
                size_bytes=1024 * 1024 + 1,
            )
        )
        await db.flush()
        with pytest.raises(HTTPException) as error:
            await gateway.binding_for(db, row, write=True, grow=True)
        assert error.value.status_code == 507
        assert "organization" in error.value.detail


async def test_full_disk_socket_rejects_input_but_keeps_resize_and_interrupt(runtime, monkeypatch):
    import json

    from fastapi import WebSocketDisconnect

    monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
    async with runtime.factory() as db:
        binding = await db.get(SandboxWorkspace, runtime.binding.id)
        binding.size_bytes = 1024 * 1024 + 1
        parent = await db.get(RuntimeSession, runtime.row.id)
        parent.kind, parent.status = "terminal", "running"
        ticket = RuntimeSession(
            workspace_id=parent.workspace_id,
            conversation_id=parent.conversation_id,
            user_id=parent.user_id,
            workspace_key=parent.workspace_key,
            lease_generation=parent.lease_generation,
            kind="ticket",
            status="running",
            expires_at=parent.expires_at,
            ticket_expires_at=parent.ticket_expires_at,
            ticket_hash=parent.ticket_hash,
            config_json={"session_id": str(parent.id), "write": True},
        )
        db.add(ticket)
        await db.commit()

    messages = iter(
        [
            {"type": "input", "seq": 1, "data": "make more files\n"},
            {"type": "resize", "cols": 100, "rows": 35},
            {"type": "interrupt"},
        ]
    )
    sent = []

    async def receive():
        try:
            return json.dumps(next(messages))
        except StopIteration as exc:
            raise WebSocketDisconnect() from exc

    socket = SimpleNamespace(
        scope={"subprotocols": ["jhin-session", f"{ticket.id}.{TOKEN}"]},
        headers={},
        query_params={},
        accept=AsyncMock(),
        close=AsyncMock(),
        receive_text=receive,
        send_json=AsyncMock(side_effect=sent.append),
    )
    request = AsyncMock(return_value={"status": "running", "output": "", "output_offset": 0})
    monkeypatch.setattr(gateway, "runner_request", request)
    app = gateway.create_app(runtime.factory)
    endpoint = next(route.endpoint for route in app.routes if route.path.endswith("/ws"))
    await endpoint(socket, parent.id)
    delivered = [call.args[2] for call in request.await_args_list if call.args[0] == "POST"]
    assert [message["type"] for message in delivered] == ["resize", "interrupt"]
    assert all(message["client_id"] == str(ticket.id) for message in delivered)
    assert any(message.get("code") == "workspace_quota" for message in sent)


def test_runtime_measurement_replaces_old_size_but_not_newer_accounting():
    previous = datetime.now(UTC)
    binding = SimpleNamespace(size_bytes=100, size_state="measured", size_measured_at=previous)
    gateway.record_runtime_size(
        binding,
        {
            "workspace_size_bytes": 200,
            "workspace_size_partial": True,
            "workspace_size_measured_at": (previous + timedelta(seconds=1)).isoformat(),
        },
    )
    assert binding.size_bytes == 200 and binding.size_state == "unknown"
    gateway.record_runtime_size(
        binding,
        {
            "workspace_size_bytes": 2,
            "workspace_size_partial": False,
            "workspace_size_measured_at": previous.isoformat(),
        },
    )
    assert binding.size_bytes == 200 and binding.size_state == "unknown"


@pytest.fixture
async def runtime():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(
            email="runtime@example.test", display_name="Owner", password_hash="unused-test-password"
        )
        workspace = Workspace(name="Runtime", slug="runtime")
        db.add_all([user, workspace])
        await db.flush()
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        chat = Conversation(
            last_activity_at=datetime.now(UTC),
            title="Runtime chat",
            workspace_id=workspace.id,
            created_by_user_id=user.id,
        )
        db.add(chat)
        await db.flush()
        binding = SandboxWorkspace(
            workspace_id=workspace.id,
            conversation_id=chat.id,
            kind="conversation",
            workspace_key="conversation-test",
            holder_user_id=user.id,
            lease_generation=1,
        )
        row = RuntimeSession(
            workspace_id=workspace.id,
            conversation_id=chat.id,
            user_id=user.id,
            workspace_key="conversation-test",
            lease_generation=1,
            kind="operation",
            status="starting",
            ticket_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
            ticket_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            config_json={
                "write": True,
                "payload_hash": gateway.digest({"value": 1}),
                "action": "files",
            },
        )
        db.add_all([binding, row])
        await db.commit()
    yield SimpleNamespace(factory=factory, row=row, user=user, binding=binding, chat=chat)
    await engine.dispose()


async def test_capability_claim_is_one_shot_even_when_response_is_uncertain(runtime, monkeypatch):
    effect = AsyncMock(side_effect=HTTPException(503, "Response lost"))
    monkeypatch.setattr(gateway, "execute_operation", effect)
    app = gateway.create_app(runtime.factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime"
    ) as client:
        path = f"/internal/operations/{runtime.row.id}"
        headers = {"Authorization": "Bearer " + TOKEN}
        changed = await client.post(path, headers=headers, json={"value": 2})
        assert changed.status_code == 409
        first = await client.post(path, headers=headers, json={"value": 1})
        assert first.status_code == 503
        repeated = await client.post(path, headers=headers, json={"value": 1})
        assert repeated.status_code == 409
    assert effect.await_count == 1
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        assert row.status == "uncertain"


async def test_authority_rechecks_revocation_expiry_and_fenced_disk(runtime):
    async with runtime.factory() as db:
        row = await gateway.authority(db, runtime.row.id, TOKEN, kind="operation")
        assert (await gateway.binding_for(db, row, write=True)).workspace_key == "conversation-test"
        row.workspace_key = "other-disk"
        with pytest.raises(HTTPException) as changed:
            await gateway.binding_for(db, row, write=True)
        assert changed.value.status_code == 409
        user = await db.get(User, runtime.user.id)
        user.status = "disabled"
        await db.flush()
        with pytest.raises(HTTPException) as revoked:
            await gateway.authority(db, row.id, TOKEN, kind="operation")
        assert revoked.value.status_code == 403
        user.status = "active"
        row.ticket_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        with pytest.raises(HTTPException) as expired:
            await gateway.authority(db, row.id, TOKEN, kind="operation")
        assert expired.value.status_code == 401


def test_replayed_and_stale_output_never_duplicates_retained_logs():
    snapshot = {"output": "retained\n", "output_offset": 9}
    first = gateway.retained_state({}, snapshot)
    second = gateway.retained_state(first, snapshot)
    assert second["output"] == "retained\n"
    assert gateway.retained_state(second, {"output": "old", "output_offset": 3}) == second


async def test_cleanup_retains_terminal_logs_after_runner_restart(runtime, monkeypatch):
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        row.kind = "terminal"
        row.status = "running"
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        row.state_json = {"output": "saved", "output_offset": 5}
        await db.commit()
    monkeypatch.setattr(
        gateway, "runner_request", AsyncMock(side_effect=HTTPException(404, "Gone"))
    )
    await gateway.cleanup_sessions(runtime.factory)
    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        assert row.status == "expired" and row.state_json["output"] == "saved"


async def test_gateway_rejects_unconfirmed_prior_command_after_handoff(runtime):
    from jhin_db.models import SandboxJob
    from jhin_domain import new_uuid7

    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        binding = await db.get(SandboxWorkspace, runtime.binding.id)
        binding.last_holder_run_id = new_uuid7()
        db.add(
            SandboxJob(
                workspace_id=row.workspace_id,
                run_id=binding.last_holder_run_id,
                image="test",
                status="running",
            )
        )
        await db.flush()
        with pytest.raises(HTTPException) as conflict:
            await gateway.binding_for(db, row, write=True)
        assert conflict.value.status_code == 409


async def test_confirmed_exact_cancel_closes_legacy_uncertain_job(runtime, monkeypatch):
    from jhin_db.models import SandboxJob, Task
    from jhin_domain import new_uuid7

    async with runtime.factory() as db:
        row = await db.get(RuntimeSession, runtime.row.id)
        row.config_json = {"action": "cancel", "write": False}
        task = Task(
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            title="Stopped turn",
            correlation_id=new_uuid7(),
            metadata_json={"stop_requested_at": datetime.now(UTC).isoformat()},
        )
        db.add(task)
        await db.flush()
        job = SandboxJob(
            workspace_id=row.workspace_id,
            task_id=task.id,
            image="test",
            status="failed",
            error_code="runner_error",
        )
        db.add(job)
        await db.flush()
        request = AsyncMock(
            return_value={"status": "cancelled", "exit_code": 137, "stdout": "saved"}
        )
        monkeypatch.setattr(gateway, "runner_request", request)
        await gateway.execute_operation(db, row, {"job_id": str(job.id), "task_id": str(task.id)})
        await db.refresh(job)
        assert job.status == "cancelled" and job.completed_at is not None
        assert job.stdout_tail == "saved"
        request.assert_awaited_once_with("POST", f"/v1/jobs/{job.id}/cancel")
