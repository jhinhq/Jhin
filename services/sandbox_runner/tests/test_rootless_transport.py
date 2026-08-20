"""Behavior tests for the fixed TCP-to-Unix rootless Docker adapter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import stat
import struct
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import jhin_sandbox_runner.rootless_transport as transport_module
from jhin_sandbox_runner.rootless_transport import (
    RootlessTransportConfigurationError,
    serve_rootless_transport,
    validate_production_boundary,
)

PING = b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"


class _RecordingWriter:
    def __init__(self, *, write_error: Exception | None = None) -> None:
        self.write_error = write_error
        self.close_calls = 0
        self.wait_closed_calls = 0

    def write(self, _data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class _FailingReader:
    async def read(self, _size: int) -> bytes:
        raise RuntimeError("unexpected upstream read failure")


class _CancellationTrackingReader:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.cleanup_finished = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_calls += 1
            await asyncio.sleep(0)
            self.cleanup_finished.set()
            raise


async def close_client(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=1)


async def wait_for_tcp(host: str, port: int) -> None:
    for _ in range(100):
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.01)
        else:
            await close_client(writer)
            return
    raise AssertionError("transport did not become ready")


async def open_when_ready(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    for _ in range(100):
        try:
            return await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.01)
    raise AssertionError("transport did not become ready")


async def ping_transport(host: str, port: int) -> bytes:
    reader, writer = await open_when_ready(host, port)
    writer.write(PING)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), timeout=1)
    await close_client(writer)
    return response


@pytest.fixture
async def fake_docker_socket() -> AsyncIterator[tuple[Path, asyncio.Queue[bytes]]]:
    directory = Path(tempfile.mkdtemp(prefix="jhin-transport-", dir="/tmp"))
    path = directory / "docker.sock"
    requests: asyncio.Queue[bytes] = asyncio.Queue()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            await requests.put(request)
            writer.write(RESPONSE)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            await close_client(writer)

    server = await asyncio.start_unix_server(handle, path=path)
    try:
        yield path, requests
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
        await asyncio.to_thread(directory.rmdir)


@pytest.mark.asyncio
async def test_rootless_transport_relays_fixed_unix_upstream_bidirectionally(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]], unused_tcp_port: int
) -> None:
    path, requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    try:
        await wait_for_tcp("127.0.0.1", unused_tcp_port)
        reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        writer.write(PING)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=1) == RESPONSE
        await close_client(writer)
        observed = [await asyncio.wait_for(requests.get(), timeout=1) for _ in range(2)]
        assert observed == [PING, PING]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_transport_fails_before_readiness_without_docker(
    tmp_path: Path, unused_tcp_port: int
) -> None:
    with pytest.raises(RootlessTransportConfigurationError, match="upstream Docker ping failed"):
        await serve_rootless_transport(
            upstream=tmp_path / "missing.sock",
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", unused_tcp_port)


@pytest.mark.asyncio
async def test_upstream_connect_failure_after_readiness_terminates_server(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]], unused_tcp_port: int
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    await wait_for_tcp("127.0.0.1", unused_tcp_port)
    path.unlink()
    _reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    writer.write(PING)
    await writer.drain()
    await close_client(writer)
    with pytest.raises(RootlessTransportConfigurationError, match="upstream connection failed"):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_midstream_downstream_reset_keeps_transport_available(
    unused_tcp_port: int,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="jhin-reset-", dir="/tmp"))
    path = directory / "docker.sock"
    stream_closed = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if b"GET /stream " not in request:
                writer.write(RESPONSE)
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
            while True:
                writer.write(b"x" * 65536)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            stream_closed.set()
        finally:
            await close_client(writer)

    upstream_server = await asyncio.start_unix_server(handle, path=path)
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    try:
        reader, writer = await open_when_ready("127.0.0.1", unused_tcp_port)
        writer.write(b"GET /stream HTTP/1.1\r\nHost: docker\r\n\r\n")
        await writer.drain()
        assert b"200 OK" in await asyncio.wait_for(reader.read(65536), timeout=1)
        client_socket = writer.get_extra_info("socket")
        assert client_socket is not None
        client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.transport.abort()
        await asyncio.wait_for(stream_closed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert task.done() is False
        assert b"200 OK" in await ping_transport("127.0.0.1", unused_tcp_port)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        upstream_server.close()
        await upstream_server.wait_closed()
        path.unlink(missing_ok=True)
        await asyncio.to_thread(directory.rmdir)


def _install_next_upstream(
    monkeypatch: pytest.MonkeyPatch,
    reader: object,
    writer: _RecordingWriter,
) -> None:
    async def open_upstream(_path: Path) -> tuple[object, _RecordingWriter]:
        return reader, writer

    monkeypatch.setattr(transport_module.asyncio, "open_unix_connection", open_upstream)


@pytest.mark.asyncio
async def test_unexpected_upstream_connect_failure_reaches_fatal_channel(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    assert b"200 OK" in await ping_transport("127.0.0.1", unused_tcp_port)

    async def fail_connect(_path: Path) -> tuple[object, _RecordingWriter]:
        raise RuntimeError("unexpected upstream connect failure")

    monkeypatch.setattr(transport_module.asyncio, "open_unix_connection", fail_connect)
    _reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    await close_client(writer)
    with pytest.raises(RootlessTransportConfigurationError, match="upstream connection failed"):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_unexpected_upstream_read_failure_reaches_fatal_channel(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    assert b"200 OK" in await ping_transport("127.0.0.1", unused_tcp_port)
    upstream_writer = _RecordingWriter()
    _install_next_upstream(monkeypatch, _FailingReader(), upstream_writer)

    _reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    with pytest.raises(RootlessTransportConfigurationError, match="upstream connection failed"):
        await asyncio.wait_for(task, timeout=1)
    await close_client(writer)
    assert upstream_writer.close_calls == 1
    assert upstream_writer.wait_closed_calls == 1


@pytest.mark.asyncio
async def test_upstream_write_failure_cancels_and_awaits_peer_before_fatal(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    assert b"200 OK" in await ping_transport("127.0.0.1", unused_tcp_port)
    upstream_reader = _CancellationTrackingReader()
    upstream_writer = _RecordingWriter(
        write_error=RuntimeError("unexpected upstream write failure")
    )
    _install_next_upstream(monkeypatch, upstream_reader, upstream_writer)

    _reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    writer.write(PING)
    await writer.drain()
    with pytest.raises(RootlessTransportConfigurationError, match="upstream connection failed"):
        await asyncio.wait_for(task, timeout=1)
    assert upstream_reader.cleanup_finished.is_set()
    assert upstream_reader.cancel_calls == 1
    assert upstream_writer.close_calls == 1
    assert upstream_writer.wait_closed_calls == 1
    await close_client(writer)


@pytest.mark.asyncio
async def test_connection_limit_rejects_excess_and_recovers(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]], unused_tcp_port: int
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=1,
        )
    )
    try:
        first_reader, first_writer = await open_when_ready("127.0.0.1", unused_tcp_port)
        await asyncio.sleep(0)
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        assert await asyncio.wait_for(second_reader.read(), timeout=1) == b""
        await close_client(second_writer)

        await close_client(first_writer)
        assert await asyncio.wait_for(first_reader.read(), timeout=1) == b""
        await asyncio.sleep(0.01)

        recovered_reader, recovered_writer = await asyncio.open_connection(
            "127.0.0.1", unused_tcp_port
        )
        recovered_writer.write(PING)
        await recovered_writer.drain()
        assert b"200 OK" in await asyncio.wait_for(recovered_reader.read(), timeout=1)
        await close_client(recovered_writer)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_repeated_bare_health_connections_do_not_exhaust_limit(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]], unused_tcp_port: int
) -> None:
    path, _requests = fake_docker_socket
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    try:
        await wait_for_tcp("127.0.0.1", unused_tcp_port)
        for _ in range(40):
            _reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
            await close_client(writer)
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        writer.write(PING)
        await writer.drain()
        assert b"200 OK" in await asyncio.wait_for(reader.read(), timeout=1)
        await close_client(writer)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"argv": ["--listen", "127.0.0.1"]}, "arguments"),
        ({"environ": {"DOCKER_HOST": "tcp://attacker:2375"}}, "environment override"),
        ({"environ": {"SANDBOX_DOCKER_GID": "123"}}, "environment override"),
        ({"effective_uid": 1}, "UID/GID 0:0"),
        ({"effective_gid": 1}, "UID/GID 0:0"),
        ({"supplemental_groups": {1}}, "no supplemental groups"),
        ({"supplemental_groups": {0, 1}}, "no supplemental groups"),
        ({"supplemental_groups": {118}}, "no supplemental groups"),
        ({"supplemental_groups": {10001}}, "no supplemental groups"),
    ],
)
def test_production_boundary_rejects_identity_arguments_and_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    upstream = tmp_path / "docker.sock"
    upstream.touch()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0, st_gid=0),
    )
    values: dict[str, object] = {
        "argv": [],
        "environ": {},
        "effective_uid": 0,
        "effective_gid": 0,
        "supplemental_groups": set(),
        "upstream": upstream,
    }
    values.update(overrides)
    with pytest.raises(RootlessTransportConfigurationError, match=message):
        validate_production_boundary(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("process_groups", [set(), {0}])
def test_production_boundary_accepts_empty_or_primary_only_process_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_groups: set[int],
) -> None:
    upstream = tmp_path / "docker.sock"
    upstream.touch()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0, st_gid=0),
    )

    assert (
        validate_production_boundary(
            argv=[],
            environ={},
            effective_uid=0,
            effective_gid=0,
            supplemental_groups=process_groups,
            upstream=upstream,
        )
        == upstream
    )


def test_production_boundary_accepts_root_owned_socket_with_mapped_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = tmp_path / "docker.sock"
    upstream.touch()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0, st_gid=118),
    )

    assert (
        validate_production_boundary(
            argv=[],
            environ={},
            effective_uid=0,
            effective_gid=0,
            supplemental_groups=set(),
            upstream=upstream,
        )
        == upstream
    )


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (SimpleNamespace(st_mode=stat.S_IFLNK, st_uid=0, st_gid=0), "not a socket"),
        (SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0, st_gid=0), "not a socket"),
        (SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=1, st_gid=118), "UID 0"),
    ],
)
def test_production_boundary_rejects_wrong_socket_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: object,
    message: str,
) -> None:
    upstream = tmp_path / "docker.sock"
    upstream.touch()
    monkeypatch.setattr(Path, "lstat", lambda _path: metadata)
    with pytest.raises(RootlessTransportConfigurationError, match=message):
        validate_production_boundary(
            argv=[],
            environ={},
            effective_uid=0,
            effective_gid=0,
            supplemental_groups=set(),
            upstream=upstream,
        )


@pytest.mark.asyncio
async def test_transport_logs_never_contain_payload_or_socket_path(
    fake_docker_socket: tuple[Path, asyncio.Queue[bytes]],
    unused_tcp_port: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path, _requests = fake_docker_socket
    canary = b"GET /containers/SECRET-CANARY HTTP/1.1\r\nHost: docker\r\n\r\n"
    task = asyncio.create_task(
        serve_rootless_transport(
            upstream=path,
            listen_host="127.0.0.1",
            listen_port=unused_tcp_port,
            connection_limit=4,
        )
    )
    try:
        await wait_for_tcp("127.0.0.1", unused_tcp_port)
        reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        writer.write(canary)
        await writer.drain()
        await asyncio.wait_for(reader.read(), timeout=1)
        await close_client(writer)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRET-CANARY" not in logs
    assert str(path) not in logs
    assert logging.getLogger("jhin_sandbox_runner.rootless_transport").isEnabledFor(logging.ERROR)
