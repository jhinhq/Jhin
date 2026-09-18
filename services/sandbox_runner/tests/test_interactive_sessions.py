from uuid import uuid4

import pytest
from pydantic import ValidationError

from jhin_sandbox_runner.jobs import WORKSPACE_KIND_LABEL, WORKSPACE_LABEL, JobManager


def test_chat_volume_is_persistent_even_without_explicit_kind_label():
    key = f"conversation-{uuid4().hex}-{uuid4().hex}"
    assert JobManager.workspace_kind({"Labels": {WORKSPACE_LABEL: key}}) == "conversation"
    assert (
        JobManager.workspace_kind({"Labels": {WORKSPACE_KIND_LABEL: "conversation"}})
        == "conversation"
    )


def test_session_inputs_are_bounded_and_cannot_name_host_directories():
    from jhin_sandbox_runner.sessions import SessionRequest

    with pytest.raises(ValidationError):
        SessionRequest(session_id=str(uuid4()), workspace_key="../../host", kind="terminal")
    with pytest.raises(ValidationError):
        SessionRequest(session_id=str(uuid4()), workspace_key="conversation-test", port=70000)


def test_terminal_config_keeps_the_sandbox_security_boundary():
    from jhin_sandbox_runner.sessions import SessionRequest, session_container_config
    from jhin_sandbox_runner.settings import Settings

    settings = Settings(
        sandbox_docker_mode="rootless",
        sandbox_docker_transport_url="http://rootless-docker-transport:2375",
    )
    req = SessionRequest(
        session_id=str(uuid4()), workspace_key="conversation-test", kind="terminal"
    )
    config = session_container_config(req, settings)
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert not host.get("PortBindings")
    assert config["User"] == "1000:1000"
    assert len(host["Mounts"]) == 1
    assert host["Mounts"][0]["Source"].endswith("conversation-test")


def test_input_sequence_deduplicates_without_replaying_keystrokes():
    from jhin_sandbox_runner.sessions import InputSequence

    seq = InputSequence()
    assert seq.accept(1)
    assert not seq.accept(1)
    assert seq.accept(3)
    assert not seq.accept(2)


@pytest.mark.asyncio
async def test_reconnecting_input_uses_an_independent_bounded_namespace():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jhin_sandbox_runner.sessions import SessionManager, SessionRecord, SessionRequest

    record = SessionRecord(SessionRequest(session_id=str(uuid4()), workspace_key="chat-test"), None)
    record.status = "running"
    record.stream = SimpleNamespace(write_in=AsyncMock())
    manager = SessionManager(SimpleNamespace(), SimpleNamespace())
    manager.records[record.request.session_id] = record
    for client, seq, data in [
        ("ticket-one", 1, "a"),
        ("ticket-one", 1, "a"),
        ("ticket-two", 1, "b"),
    ]:
        await manager.input(
            record.request.session_id,
            {"type": "input", "client_id": client, "seq": seq, "data": data},
        )
    assert [call.args[0] for call in record.stream.write_in.call_args_list] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_session_expiry_stops_container_and_preserves_retained_output():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jhin_sandbox_runner.sessions import SessionManager, SessionRecord, SessionRequest

    req = SessionRequest(session_id=str(uuid4()), workspace_key="chat-test", expires_in_seconds=60)
    container = SimpleNamespace(delete=AsyncMock())
    record = SessionRecord(req, container, status="running", output="retained", offset=8)
    record.created_at -= 61
    jobs = SimpleNamespace(_workspace_holders={req.workspace_key: req.session_id})
    manager = SessionManager(jobs, SimpleNamespace())
    manager.records[req.session_id] = record
    await manager.cleanup()
    assert record.status == "expired"
    assert manager.get(req.session_id).snapshot()["output"] == "retained"
    assert not jobs._workspace_holders
    container.delete.assert_awaited_once()


def test_terminal_output_withholds_a_split_secret_before_replay(monkeypatch):
    import jhin_sandbox_runner.sessions as sessions
    from jhin_secrets.redaction import SecretRedactor

    redactor = SecretRedactor()
    redactor.register("secret-canary-terminal")
    monkeypatch.setattr(sessions, "get_redactor", lambda: redactor)
    record = sessions.SessionRecord(
        sessions.SessionRequest(session_id=str(uuid4()), workspace_key="chat-test"), None
    )
    record.append("visible secret-can")
    first = record.snapshot()
    assert first["output"] == "visible "
    record.append("ary-terminal complete")
    assert record.snapshot(after=first["output_offset"])["output"] == "[REDACTED] complete"
    assert "secret-can" not in record.snapshot()["output"]


@pytest.mark.asyncio
async def test_input_namespace_capacity_never_evicts_replay_history(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    import jhin_sandbox_runner.sessions as sessions

    monkeypatch.setattr(sessions, "INPUT_CLIENT_LIMIT", 1)
    record = sessions.SessionRecord(
        sessions.SessionRequest(session_id=str(uuid4()), workspace_key="chat-test"),
        None,
        status="running",
        stream=SimpleNamespace(write_in=AsyncMock()),
    )
    manager = sessions.SessionManager(SimpleNamespace(), SimpleNamespace())
    manager.records[record.request.session_id] = record
    message = {"type": "input", "client_id": "first", "seq": 1, "data": "once"}
    await manager.input(record.request.session_id, message)
    with pytest.raises(HTTPException) as full:
        await manager.input(record.request.session_id, {**message, "client_id": "new"})
    assert full.value.status_code == 429
    await manager.input(record.request.session_id, message)
    assert record.stream.write_in.await_count == 1


@pytest.mark.asyncio
async def test_terminal_close_measures_retained_disk_before_releasing_writer():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jhin_sandbox_runner.sessions import SessionManager, SessionRecord, SessionRequest

    request = SessionRequest(session_id=str(uuid4()), workspace_key="chat-test")
    holders = {request.workspace_key: request.session_id}

    async def measure(key, *, job_id):
        assert holders[key] == job_id
        return 128, False

    jobs = SimpleNamespace(_workspace_holders=holders, _ensure_workspace_volume=measure)
    record = SessionRecord(request, SimpleNamespace(delete=AsyncMock()), status="running")
    manager = SessionManager(jobs, SimpleNamespace())
    manager.records[request.session_id] = record
    state = await manager.stop(request.session_id)
    assert state["status"] == "stopped" and not holders
    assert state["workspace_size_bytes"] == 128 and not state["workspace_size_partial"]
    assert state["workspace_size_measured_at"]
