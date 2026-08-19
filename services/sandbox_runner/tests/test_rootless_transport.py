"""Behavior tests for the fixed TCP-to-Unix rootless Docker adapter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import stat
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from jhin_sandbox_runner.rootless_transport import (
    RootlessTransportConfigurationError,
    serve_rootless_transport,
    validate_production_boundary,
)

PING = b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"


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


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (SimpleNamespace(st_mode=stat.S_IFLNK, st_uid=0, st_gid=0), "not a socket"),
        (SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0, st_gid=0), "not a socket"),
        (SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=1, st_gid=0), "UID/GID 0:0"),
        (SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0, st_gid=1), "UID/GID 0:0"),
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
