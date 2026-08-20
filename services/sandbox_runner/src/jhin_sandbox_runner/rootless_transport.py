"""Fixed TCP-to-Unix transport for a private rootless Docker boundary.

The production entry point intentionally has no configuration surface. The
injectable ``serve_rootless_transport`` arguments exist only for unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from jhin_sandbox_runner.docker_socket import normalize_supplemental_groups

logger = logging.getLogger(__name__)

_FIXED_UPSTREAM = Path("/run/host/docker.sock")
_FIXED_LISTEN_HOST = "0.0.0.0"
_FIXED_LISTEN_PORT = 2375
_CHUNK_SIZE = 64 * 1024
_MAX_CONNECTIONS = 32
_IO_TIMEOUT_SECONDS = 5.0
_PING_REQUEST = b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n"


class RootlessTransportConfigurationError(RuntimeError):
    """The adapter cannot safely provide its fixed Docker transport."""


class _UpstreamConnectionError(Exception):
    """An upstream operation failed; details must not cross the boundary."""


def validate_production_boundary(
    *,
    argv: Sequence[str],
    environ: Mapping[str, str],
    effective_uid: int,
    effective_gid: int,
    supplemental_groups: set[int],
    upstream: Path = _FIXED_UPSTREAM,
) -> Path:
    """Validate the immutable production identity and socket boundary."""
    if argv:
        raise RootlessTransportConfigurationError("transport accepts no arguments")
    forbidden_env = {
        name
        for name in environ
        if name.startswith(("DOCKER_", "SANDBOX_DOCKER_", "ROOTLESS_TRANSPORT_"))
    }
    if forbidden_env:
        raise RootlessTransportConfigurationError(
            "transport accepts no Docker environment override"
        )
    if effective_uid != 0 or effective_gid != 0:
        raise RootlessTransportConfigurationError("transport requires UID/GID 0:0")
    authority_groups = normalize_supplemental_groups(
        effective_gid=effective_gid,
        process_groups=supplemental_groups,
    )
    if authority_groups:
        raise RootlessTransportConfigurationError("transport requires no supplemental groups")
    try:
        info = upstream.lstat()
    except OSError as exc:
        raise RootlessTransportConfigurationError("cannot inspect fixed upstream socket") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise RootlessTransportConfigurationError("fixed upstream is not a socket")
    # The rootless daemon owner maps to UID 0 here, while its host socket GID
    # may map through the subordinate range to any nonnegative container GID.
    if info.st_uid != 0:
        raise RootlessTransportConfigurationError("upstream socket requires UID 0")
    return upstream


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=_IO_TIMEOUT_SECONDS)


async def _probe_upstream(upstream: Path) -> None:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(upstream), timeout=_IO_TIMEOUT_SECONDS
        )
        assert writer is not None
        writer.write(_PING_REQUEST)
        await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=_IO_TIMEOUT_SECONDS)
        status_line = headers.split(b"\r\n", 1)[0]
        if status_line not in (b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK"):
            raise RootlessTransportConfigurationError("upstream Docker ping failed")
    except RootlessTransportConfigurationError:
        raise
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise RootlessTransportConfigurationError("upstream Docker ping failed") from exc
    finally:
        if writer is not None:
            await _close_writer(writer)


async def _copy_client_to_upstream(
    client_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter
) -> None:
    while True:
        try:
            chunk = await client_reader.read(_CHUNK_SIZE)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if not chunk:
            return
        try:
            upstream_writer.write(chunk)
            await upstream_writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _UpstreamConnectionError from exc


async def _copy_upstream_to_client(
    upstream_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    while True:
        try:
            chunk = await upstream_reader.read(_CHUNK_SIZE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _UpstreamConnectionError from exc
        if not chunk:
            return
        try:
            client_writer.write(chunk)
            await client_writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            return


async def _relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream: Path,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    copies: tuple[asyncio.Task[None], asyncio.Task[None]] | None = None
    results: list[BaseException | None] = []
    try:
        try:
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _UpstreamConnectionError from exc
        copies = (
            asyncio.create_task(_copy_client_to_upstream(client_reader, upstream_writer)),
            asyncio.create_task(_copy_upstream_to_client(upstream_reader, client_writer)),
        )
        await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if copies is not None:
            for task in copies:
                if not task.done():
                    task.cancel()
            results = list(await asyncio.gather(*copies, return_exceptions=True))
        await _close_writer(client_writer)
        if upstream_writer is not None:
            await _close_writer(upstream_writer)
    for result in results:
        if isinstance(result, _UpstreamConnectionError):
            raise result


async def serve_rootless_transport(
    *,
    upstream: Path,
    listen_host: str,
    listen_port: int,
    connection_limit: int,
) -> None:
    """Probe Docker, then relay TCP connections to one Unix socket."""
    if connection_limit < 1 or connection_limit > _MAX_CONNECTIONS:
        raise RootlessTransportConfigurationError("invalid connection limit")
    await _probe_upstream(upstream)

    semaphore = asyncio.Semaphore(connection_limit)
    fatal: asyncio.Future[RootlessTransportConfigurationError] = (
        asyncio.get_running_loop().create_future()
    )
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        assert current is not None
        handlers.add(current)
        acquired = False
        try:
            if semaphore.locked():
                await _close_writer(writer)
                return
            await semaphore.acquire()
            acquired = True
            try:
                await _relay_connection(reader, writer, upstream=upstream)
            except _UpstreamConnectionError:
                if not fatal.done():
                    fatal.set_result(
                        RootlessTransportConfigurationError("upstream connection failed")
                    )
        finally:
            if acquired:
                semaphore.release()
            handlers.discard(current)

    server = await asyncio.start_server(handle, host=listen_host, port=listen_port)

    async def run_server() -> object:
        await server.serve_forever()
        return None

    async def receive_fatal() -> object:
        return await fatal

    server_task = asyncio.create_task(run_server())
    fatal_task = asyncio.create_task(receive_fatal())
    logger.info("rootless_transport.ready", extra={"code": "ready"})
    try:
        done, _pending = await asyncio.wait(
            {server_task, fatal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if fatal_task in done:
            error = fatal_task.result()
            assert isinstance(error, RootlessTransportConfigurationError)
            raise error
        await server_task
    finally:
        server.close()
        await server.wait_closed()
        server_task.cancel()
        fatal_task.cancel()
        await asyncio.gather(server_task, fatal_task, return_exceptions=True)
        for task in list(handlers):
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)


async def _run_production() -> None:
    upstream = validate_production_boundary(
        argv=sys.argv[1:],
        environ=os.environ,
        effective_uid=os.geteuid(),
        effective_gid=os.getegid(),
        supplemental_groups=set(os.getgroups()),
    )
    await serve_rootless_transport(
        upstream=upstream,
        listen_host=_FIXED_LISTEN_HOST,
        listen_port=_FIXED_LISTEN_PORT,
        connection_limit=_MAX_CONNECTIONS,
    )


def main() -> None:
    try:
        asyncio.run(_run_production())
    except RootlessTransportConfigurationError:
        logger.error("rootless_transport.failed", extra={"code": "configuration_error"})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
