"""CLI executor behavior against a stubbed runner: sandbox_job persistence,
audit trail, secret injection/redaction, checkout wiring (plan 14, 48.9)."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.base import VerifyContext
from jhin_connectors.cli import tools as cli_tools
from jhin_connectors.cli.connector import CliConnector
from jhin_connectors.cli.schemas import (
    CommandExecuteInput,
    CommandExecuteOutput,
    FileReadInput,
    FileReadOutput,
    FileWriteInput,
    FileWriteOutput,
    RepositoryCheckoutInput,
    RepositoryCheckoutOutput,
    TestRunInput,
    TestRunOutput,
)
from jhin_db.models import AuditEvent, SandboxJob, Workspace
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext

TOKEN = "ghp_sandbox_secret_token_9876"


class RunnerStub:
    """Captures the submitted payload and returns a scripted terminal doc."""

    def __init__(self, **overrides: Any) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.response: dict[str, Any] = {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 120,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            **overrides,
        }

    async def __call__(
        self, payload: dict[str, Any], *, job_timeout_seconds: int
    ) -> dict[str, Any]:
        self.payloads.append(payload)
        return dict(self.response)


@pytest.fixture
def linked_context(context: ToolExecutionContext) -> ToolExecutionContext:
    """Context as the gateway builds it just before execution."""
    from dataclasses import replace

    return replace(context, tool_call_id=new_uuid7())


async def _cli_connection(make_connection, workspace: Workspace, **config: object):
    return await make_connection(
        workspace,
        connector_type="cli",
        name=f"cli-{new_uuid7().hex[:6]}",
        auth_type="none",
        credentials={},
        config=config or {"default_image": "jhin-sandbox:latest", "default_network": "none"},
    )


class TestCommandExecute:
    async def test_persists_sandbox_job_linked_to_tool_call(
        self,
        session: AsyncSession,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = RunnerStub(stdout="hello\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        connection = await _cli_connection(make_connection, workspace)

        output = await cli_tools._command_execute(
            linked_context,
            CommandExecuteInput(connection_id=str(connection.id), command="echo hello"),
        )

        assert isinstance(output, CommandExecuteOutput)
        assert output.exit_code == 0
        assert output.stdout == "hello\n"

        row = await session.scalar(select(SandboxJob))
        assert row is not None
        assert row.status == "completed"
        assert row.tool_call_id == linked_context.tool_call_id
        assert row.run_id == linked_context.run_id
        assert row.workspace_id == workspace.id
        assert str(row.id) == output.sandbox_job_id

        # One job per run shares one workspace volume (plan 14.5).
        payload = stub.payloads[0]
        assert payload["workspace_key"] == f"run-{linked_context.run_id}"
        assert payload["network_policy"] == "none"
        assert payload["image"] == "jhin-sandbox:latest"
        assert payload["command"][0] == "bash"

        actions = [
            event.action
            for event in (await session.scalars(select(AuditEvent))).all()
            if event.action.startswith("sandbox.")
        ]
        assert actions == ["sandbox.job.started", "sandbox.job.completed"]

    async def test_runner_failure_marks_row_failed_and_audits(
        self,
        session: AsyncSession,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict[str, Any]:
            raise cli_tools.SandboxRunnerError("sandbox runner unreachable: ConnectError")

        monkeypatch.setattr(cli_tools, "run_sandbox_job", boom)
        connection = await _cli_connection(make_connection, workspace)

        with pytest.raises(cli_tools.CliToolError, match="unreachable"):
            await cli_tools._command_execute(
                linked_context,
                CommandExecuteInput(connection_id=str(connection.id), command="true"),
            )
        row = await session.scalar(select(SandboxJob))
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "runner_error"
        actions = [
            event.action
            for event in (await session.scalars(select(AuditEvent))).all()
            if event.action.startswith("sandbox.")
        ]
        assert actions == ["sandbox.job.started", "sandbox.job.failed"]

    async def test_timeout_status_recorded(
        self,
        session: AsyncSession,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = RunnerStub(status="timeout", exit_code=None)
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        connection = await _cli_connection(make_connection, workspace)
        output = await cli_tools._command_execute(
            linked_context,
            CommandExecuteInput(connection_id=str(connection.id), command="sleep 999"),
        )
        assert isinstance(output, CommandExecuteOutput)
        assert output.status == "timeout"
        row = await session.scalar(select(SandboxJob))
        assert row is not None
        assert row.status == "timeout"
        assert row.error_code == "timeout"


class TestCheckoutAndGitCredentials:
    async def test_checkout_injects_short_lived_token_as_secret_env(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        github = await make_connection(
            workspace,
            connector_type="github",
            name="GitHub",
            credentials={"token": TOKEN},
            config={"base_url": "http://fake-github:8080"},
        )
        cli = await _cli_connection(
            make_connection,
            workspace,
            default_image="jhin-sandbox:latest",
            git_connection_id=str(github.id),
        )
        stub = RunnerStub(stdout="JHIN_HEAD=abc123def\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)

        output = await cli_tools._repository_checkout(
            linked_context,
            RepositoryCheckoutInput(connection_id=str(cli.id), repository="octo/alpha"),
        )

        assert isinstance(output, RepositoryCheckoutOutput)
        assert output.head_sha == "abc123def"
        assert output.path == "/workspace/repo"
        # Default branch naming: agent/<task-prefix>-<repo-slug> (plan 14.5).
        assert output.branch.startswith("agent/")
        assert output.branch.endswith("-alpha")

        payload = stub.payloads[0]
        assert payload["network_policy"] == "internet"
        assert payload["secret_env"] == {"GIT_TOKEN": TOKEN}
        script = payload["command"][2]
        # Token travels only via secret env — never embedded in the URL.
        assert TOKEN not in script
        assert "http://fake-github:8080/git/octo/alpha.git" in script
        assert "GIT_ASKPASS" in script

    async def test_checkout_without_git_connection_fails_safely(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = await _cli_connection(make_connection, workspace)
        monkeypatch.setattr(cli_tools, "run_sandbox_job", RunnerStub())
        with pytest.raises(cli_tools.CliToolError, match="no GitHub connection configured"):
            await cli_tools._repository_checkout(
                linked_context,
                RepositoryCheckoutInput(connection_id=str(cli.id), repository="octo/alpha"),
            )

    async def test_command_execute_reinjects_git_token_on_internet_jobs(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        github = await make_connection(
            workspace,
            connector_type="github",
            name="GitHub push",
            credentials={"token": TOKEN},
            config={"base_url": "http://fake-github:8080"},
        )
        cli = await _cli_connection(
            make_connection,
            workspace,
            git_connection_id=str(github.id),
        )
        stub = RunnerStub()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)

        await cli_tools._command_execute(
            linked_context,
            CommandExecuteInput(
                connection_id=str(cli.id), command="git push origin HEAD", network="internet"
            ),
        )
        payload = stub.payloads[0]
        assert payload["secret_env"] == {"GIT_TOKEN": TOKEN}
        assert payload["env"]["GIT_ASKPASS"] == "/workspace/.jhin-askpass"

        # Isolated jobs get no credential at all.
        await cli_tools._command_execute(
            linked_context,
            CommandExecuteInput(connection_id=str(cli.id), command="ls", network="none"),
        )
        assert stub.payloads[1]["secret_env"] == {}

    async def test_secret_value_redacted_from_persisted_tails(
        self,
        session: AsyncSession,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker-side defense in depth: even if the runner missed it, the
        process redactor scrubs the token before the row persists (48.9)."""
        github = await make_connection(
            workspace,
            connector_type="github",
            name="GitHub leak",
            credentials={"token": TOKEN},
            config={"base_url": "http://fake-github:8080"},
        )
        cli = await _cli_connection(
            make_connection,
            workspace,
            git_connection_id=str(github.id),
        )
        stub = RunnerStub(stdout=f"leaked: {TOKEN}\nJHIN_HEAD=beef\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)

        output = await cli_tools._repository_checkout(
            linked_context,
            RepositoryCheckoutInput(connection_id=str(cli.id), repository="octo/alpha"),
        )
        assert isinstance(output, RepositoryCheckoutOutput)
        assert TOKEN not in output.stdout
        row = await session.scalar(select(SandboxJob))
        assert row is not None
        assert TOKEN not in row.stdout_tail
        assert TOKEN not in row.command


class TestFileAndTestTools:
    async def test_file_read_returns_content_and_forces_isolation(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = RunnerStub(stdout="VALUE = 1\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)
        output = await cli_tools._file_read(
            linked_context, FileReadInput(connection_id=str(cli.id), path="app.py")
        )
        assert isinstance(output, FileReadOutput)
        assert output.content == "VALUE = 1\n"
        assert stub.payloads[0]["network_policy"] == "none"
        assert stub.payloads[0]["secret_env"] == {}

    async def test_file_read_failure_raises(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = RunnerStub(exit_code=1, stderr="cat: nope: No such file or directory\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)
        with pytest.raises(cli_tools.CliToolError, match="file read failed"):
            await cli_tools._file_read(
                linked_context, FileReadInput(connection_id=str(cli.id), path="nope")
            )

    async def test_file_write_passes_content_via_env_not_command(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = RunnerStub(stdout="10\n")
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)
        output = await cli_tools._file_write(
            linked_context,
            FileWriteInput(connection_id=str(cli.id), path="app.py", content="VALUE = 2\n"),
        )
        assert isinstance(output, FileWriteOutput)
        assert output.bytes_written == 10
        payload = stub.payloads[0]
        assert payload["env"]["JHIN_FILE_CONTENT"] == "VALUE = 2\n"
        assert "VALUE = 2" not in payload["command"][2]

    async def test_test_run_reports_pass_fail(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = await _cli_connection(make_connection, workspace)
        stub = RunnerStub(stdout="1 failed\n", exit_code=1)
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        failing = await cli_tools._test_run(linked_context, TestRunInput(connection_id=str(cli.id)))
        assert isinstance(failing, TestRunOutput)
        assert not failing.passed

        stub.response["exit_code"] = 0
        stub.response["stdout"] = "tests passed\n"
        passing = await cli_tools._test_run(linked_context, TestRunInput(connection_id=str(cli.id)))
        assert isinstance(passing, TestRunOutput)
        assert passing.passed


class TestVerifyConnection:
    async def test_accepts_valid_config(self) -> None:
        health = await CliConnector().verify_connection(
            VerifyContext(
                auth_type="none",
                credentials={},
                config={"default_image": "jhin-sandbox:latest", "default_network": "internet"},
            )
        )
        assert health.ok
        assert health.details["network"] == "internet"

    async def test_rejects_bad_network(self) -> None:
        health = await CliConnector().verify_connection(
            VerifyContext(auth_type="none", credentials={}, config={"default_network": "host"})
        )
        assert not health.ok
