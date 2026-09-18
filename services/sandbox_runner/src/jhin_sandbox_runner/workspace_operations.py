"""Contained filesystem operations, executed as the sandbox UID, never the host."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from jhin_sandbox_runner.jobs import JobManager, build_container_config, resolve_limits
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

SCRIPT = Path(__file__).with_name("workspace_script.py").read_text(encoding="utf-8")


class WorkspaceOperation(BaseModel):
    operation: Literal["read", "write", "list", "browse", "snapshot", "restore", "stage"]
    args: dict[str, Any] = Field(default_factory=dict)


async def run_operation(
    jobs: JobManager, settings: Settings, key: str, payload: WorkspaceOperation
) -> dict[str, Any]:
    identifier = uuid4().hex
    # Serialized against agent command jobs and human PTYs. A read may otherwise
    # publish a half-written file as an immutable version.
    if key in jobs._workspace_holders:
        raise HTTPException(
            409,
            "Workspace is busy. Stop the command or terminal before editing or capturing files.",
        )
    jobs._workspace_holders[key] = identifier
    container = None
    try:
        request = SandboxJobRequest(
            job_id=identifier,
            command=["sleep", "infinity"],
            workspace_key=key,
            network_policy="none",
        )
        cpu, memory, pids, _ = resolve_limits(request, settings)
        config = build_container_config(
            request,
            settings,
            image=settings.sandbox_default_image,
            cpu_limit=cpu,
            memory_mb=memory,
            pids_limit=pids,
        )
        config["HostConfig"]["Mounts"][0]["ReadOnly"] = payload.operation in {
            "read",
            "list",
            "browse",
            "snapshot",
        }
        await jobs._ensure_workspace_volume(key, job_id=identifier)
        container = await jobs.docker.containers.create(config, name="jhin-file-" + identifier)
        await container.start()
        # Both arguments are data to Python, never interpolated into a shell.
        code = (
            "import base64; exec(base64.b64decode("
            + repr(base64.b64encode(SCRIPT.encode()).decode())
            + "))"
        )
        process = await container.exec(
            cmd=["python3", "-c", code],
            stdin=True,
            tty=False,
            user="1000:1000",
            workdir="/workspace",
        )
        stream = process.start()
        request_bytes = json.dumps({"operation": payload.operation, "args": payload.args}).encode()
        if len(request_bytes) > 48 * 1024 * 1024:
            raise HTTPException(413, "Workspace operation exceeds the transfer limit")
        # Script uses a single bounded line so no transport half-close is needed.
        output = bytearray()
        async with asyncio.timeout(90):
            await stream.write_in(request_bytes + b"\n")
            while message := await stream.read_out():
                if message.stream == 1:
                    output.extend(message.data)
                if len(output) > 48 * 1024 * 1024:
                    raise HTTPException(413, "Workspace response exceeds the transfer limit")
        result = json.loads(output)
        if not result.get("ok"):
            raise HTTPException(409, result.get("error", "Workspace operation failed")[:300])
        # Measure after writes while the same workspace fence is still held.
        # Human edits must update quota accounting just like agent commands.
        size, partial = await jobs._ensure_workspace_volume(key, job_id=identifier)
        return {
            **result["data"],
            "workspace_size_bytes": size,
            "workspace_size_partial": partial,
            "workspace_size_measured_at": datetime.now(UTC).isoformat(),
        }
    except TimeoutError as exc:
        raise HTTPException(
            504, "Workspace operation timed out; inspect current versions before retrying"
        ) from exc
    finally:
        if container is not None:
            with contextlib.suppress(Exception):
                await container.delete(force=True, v=True)
        if jobs._workspace_holders.get(key) == identifier:
            jobs._workspace_holders.pop(key, None)


def install_workspace_routes(
    app: FastAPI, jobs: JobManager, settings: Settings, require_token: Any
) -> None:
    @app.post("/v1/workspaces/{workspace_key}/operation", dependencies=[Depends(require_token)])
    async def workspace_operation(
        workspace_key: str, payload: WorkspaceOperation
    ) -> dict[str, Any]:
        from jhin_sandbox_runner.schemas import WORKSPACE_KEY_RE

        if not WORKSPACE_KEY_RE.fullmatch(workspace_key):
            raise HTTPException(422, "Invalid workspace identity")
        return await run_operation(jobs, settings, workspace_key, payload)
