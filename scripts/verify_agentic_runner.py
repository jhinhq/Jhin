"""Repeatable Docker acceptance test using unique disposable test workspaces.

Run inside the runner image with this repository mounted read-only at /repo.
Does not start another runner server or reap any existing jobs/volumes.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException

from jhin_sandbox_runner.jobs import JobManager
from jhin_sandbox_runner.sessions import SessionManager, SessionRecord, SessionRequest
from jhin_sandbox_runner.settings import Settings
from jhin_sandbox_runner.workspace_operations import WorkspaceOperation, run_operation


async def main() -> None:
    settings = Settings(
        sandbox_docker_mode="desktop",
        sandbox_docker_socket=Path("/run/jhin/docker.sock"),
        sandbox_default_image="jhin-sandbox:agentic",
    )
    jobs = JobManager(settings)

    # The test shares Docker Desktop with the user's existing runner. Disable
    # only this test object's startup reaper, which assumes a sole runner.
    async def no_reaping() -> None:
        pass

    jobs.reap_orphans = no_reaping  # type: ignore[method-assign]
    await jobs.start()
    sessions = SessionManager(jobs, settings)
    key = "conversation-acceptance-" + uuid4().hex
    other = "conversation-acceptance-" + uuid4().hex
    checks: list[str] = []

    async def operation(
        kind: Literal["read", "write", "list", "browse", "snapshot", "restore", "stage"],
        args: dict[str, Any],
        workspace: str = key,
    ) -> dict[str, Any]:
        return await run_operation(
            jobs, settings, workspace, WorkspaceOperation(operation=kind, args=args)
        )

    async def wait_output(record: SessionRecord, expected: str) -> None:
        async with asyncio.timeout(20):
            while expected not in record.output:
                if record.status != "running":
                    raise AssertionError(record.snapshot())
                await asyncio.sleep(0.1)

    try:
        data = b"persistent input\n"
        encoded = base64.b64encode(data).decode()
        await operation(
            "write", {"path": "input.txt", "content_base64": encoded, "expected_sha256": None}
        )
        result = await operation("read", {"path": "input.txt"})
        assert base64.b64decode(result["content_base64"]) == data
        checks.append("contained write/read")
        try:
            await operation(
                "write",
                {"path": "input.txt", "content_base64": encoded, "expected_sha256": "stale"},
            )
            raise AssertionError("stale write accepted")
        except HTTPException as exc:
            assert exc.status_code == 409
        assert (await operation("snapshot", {}, other))["files"] == []
        checks.append("revision guard and chat isolation")
        sid = str(uuid4())
        record = await sessions.create(
            SessionRequest(session_id=sid, workspace_key=key, network="internet")
        )
        config = await record.container.show()
        assert config["Config"]["User"] == "1000:1000"
        assert config["HostConfig"]["ReadonlyRootfs"]
        await sessions.input(
            sid,
            {
                "type": "input",
                "seq": 1,
                "data": "printf 'terminal-visible\\n'; pwd; cat input.txt\n",
            },
        )
        await wait_output(record, "persistent input")
        assert "/workspace" in record.output
        before = record.offset
        await sessions.input(sid, {"type": "resize", "cols": 90, "rows": 35})
        await sessions.input(
            sid, {"type": "input", "seq": 2, "data": "printf 'once\\n' >> count.txt\n"}
        )
        await sessions.input(
            sid, {"type": "input", "seq": 2, "data": "printf 'once\\n' >> count.txt\n"}
        )
        await sessions.input(sid, {"type": "input", "seq": 3, "data": "sleep 60\n"})
        await asyncio.sleep(0.3)
        await sessions.input(sid, {"type": "interrupt"})
        await sessions.input(sid, {"type": "input", "seq": 4, "data": "echo interrupt-confirmed\n"})
        await wait_output(record, "interrupt-confirmed")
        assert record.snapshot(before)["output_offset"] >= before
        checks.append("PTY output, resize, bounded replay, duplicate input, interrupt")
        await sessions.input(
            sid,
            {
                "type": "input",
                "seq": 5,
                "data": (
                    'python -c "import docx,openpyxl,pptx,pypdf; d=docx.Document(); '
                    "d.add_paragraph('Deliverable'); d.save('report.docx'); "
                    "print('documents-ready')\"\n"
                ),
            },
        )
        await wait_output(record, "documents-ready\r\n")
        await sessions.input(
            sid,
            {
                "type": "input",
                "seq": 6,
                "data": "curl --max-time 10 -I https://example.com; echo curl-finished\n",
            },
        )
        await wait_output(record, "curl-finished\r\n")
        assert "HTTP/" in record.output
        await sessions.stop(sid)
        checks.append("document generation and outbound internet")
        assert (
            base64.b64decode((await operation("read", {"path": "count.txt"}))["content_base64"])
            == b"once\n"
        )
        assert (await operation("read", {"path": "report.docx"}))["size_bytes"] > 1000
        assert (
            base64.b64decode((await operation("read", {"path": "input.txt"}))["content_base64"])
            == data
        )
        checks.append("files retained after terminal container destruction")
        print(json.dumps({"passed": checks}, indent=2))
    finally:
        await sessions.close()
        for test_key in (key, other):
            assert test_key.startswith("conversation-acceptance-")
            await jobs.delete_workspace(test_key)
        await jobs.close()


asyncio.run(main())
