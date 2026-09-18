"""What the two writing file tools promise about the file itself.

``cli.file.edit`` and ``cli.file.write`` both declare ``redispatch_is_safe``,
and that declaration no longer rests on anything in this file. A second
dispatch of one invocation is answered with the first dispatch's job and never
reaches a container at all — see
``services/sandbox_runner/tests/test_invocation_idempotency.py``, which is
where the repeat is actually stopped and why every sandbox tool is stopped by
the same thing.

These tests are about the promises that *are* the job's to keep, run against
the real edit program and the real write script the executor submits rather
than asserted about them:

* **Ambiguity.** ``old_string`` must occur exactly ``expected_count`` times or
  nothing is written. That is a question about the file, and the file can
  answer it.
* **Repetition is not.** Two guards over file contents were tried and both
  were wrong, the second one worse than the first. Counting ``old_string``
  cannot see a repeat (after an anchor insert the anchor is still there once).
  Adding "and the result is already present" refused ordinary first-time
  edits — every deletion, and anything where ``new_string`` is a substring of
  ``old_string`` — while *still* passing the case where the two strings are
  anagrams of one another. Both regressions are pinned below, because the
  lesson is not "that guard had a bug": it is that a file records an effect
  and the question was about an event.
* **All or nothing.** An interrupted job leaves the file as it was. Both
  tools stage their write beside the file and rename it into place, so there
  is no instant at which the file is half of each.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jhin_connectors.cli import tools as cli_tools
from jhin_connectors.cli.runner_client import SandboxInvocationUnknownError
from jhin_connectors.cli.schemas import FileEditInput, FileListInput, FileWriteInput
from jhin_connectors.cli.tools import _EDIT_PROGRAM
from jhin_db.models import SandboxJob, Workspace
from jhin_domain import SandboxJobStatus, new_uuid7
from jhin_tools import ToolExecutionContext
from jhin_tools.errors import ToolExecutionError

pytestmark = pytest.mark.anyio

_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(
    _BASH is None,
    reason="the write guard is a shell script; running it needs a POSIX shell",
)


@pytest.fixture
def linked_context(context: ToolExecutionContext) -> ToolExecutionContext:
    """Context as the gateway builds it just before execution."""
    return replace(context, tool_call_id=new_uuid7())


class _CaptureRunner:
    """Answers every job the same way and keeps what was submitted.

    The point here is never the answer: it is the argv and env the executor
    would have handed a container, which the tests below run themselves.
    """

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def __call__(
        self, payload: dict[str, Any], *, job_timeout_seconds: int
    ) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 5,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }


async def _cli_connection(make_connection, workspace: Workspace):
    return await make_connection(
        workspace,
        connector_type="cli",
        name=f"cli-{new_uuid7().hex[:6]}",
        auth_type="none",
        credentials={},
        config={"default_image": "jhin-sandbox:latest", "default_network": "none"},
    )


def _edit(path: Path, *, old: str, new: str, expected: int = 1) -> subprocess.CompletedProcess[str]:
    """Run the product's own edit program over one real file."""
    environment = dict(os.environ)
    environment.update(
        {
            "JHIN_EDIT_PATH": str(path),
            "JHIN_EDIT_OLD": old,
            "JHIN_EDIT_NEW": new,
            "JHIN_EDIT_EXPECTED": str(expected),
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _EDIT_PROGRAM],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _refusal(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stderr.splitlines():
        if line.startswith("JHIN_ERR="):
            return line.removeprefix("JHIN_ERR=").strip()
    return ""


class TestTheEditsOwnGuardIsAboutAmbiguity:
    def test_the_ambiguity_guard_is_the_one_that_stayed(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"x = 1\nx = 1\n")

        result = _edit(target, old="x = 1", new="x = 2")

        assert (result.returncode, _refusal(result)) == (65, "edit_count_mismatch")
        assert "JHIN_ACTUAL=2" in result.stderr
        assert target.read_bytes() == b"x = 1\nx = 1\n"

    def test_it_refuses_before_writing_anything(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"x = 1\nx = 1\n")
        before = target.read_bytes()

        assert _refusal(_edit(target, old="x = 1", new="x = 2")) == "edit_count_mismatch"
        assert target.read_bytes() == before
        # And it left nothing behind while deciding not to.
        assert list(tmp_path.iterdir()) == [target]

    def test_an_edit_whose_result_appears_elsewhere_still_applies(self, tmp_path: Path) -> None:
        """Changing one call site to match another is most of what editing
        code *is*, and no guard may treat it as suspicious."""
        target = tmp_path / "config.py"
        target.write_bytes(b"timeout = 30\nretries = 30\n")

        result = _edit(target, old="timeout = 30", new="timeout = 60")

        assert result.returncode == 0
        assert target.read_bytes() == b"timeout = 60\nretries = 30\n"

    def test_an_ordinary_anchor_insert_applies(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"import os\n\nprint(os.getcwd())\n")

        result = _edit(target, old="import os", new="import os\nimport sys")

        assert result.returncode == 0
        assert target.read_bytes() == b"import os\nimport sys\n\nprint(os.getcwd())\n"

    def test_a_multi_site_edit_applies_at_every_site(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"log()\nwork()\nlog()\n")

        result = _edit(target, old="log()", new="log()\nflush()", expected=2)

        assert result.returncode == 0
        assert target.read_bytes().count(b"flush()") == 2


class TestTheRegressionsOfGuessingFromContents:
    """The two shapes that made a content guard untenable.

    They are kept as tests rather than as a note, because the guard read as
    correct and would be written again. The first pair is what it broke; the
    third is what it never caught anyway.
    """

    def test_a_deletion_applies_on_the_first_attempt(self, tmp_path: Path) -> None:
        """``new_string`` has no minimum length, and ``data.count("")`` is
        ``len(data) + 1`` for every file there has ever been. So the guard's
        second condition was unconditionally true for every deletion, and the
        first real deletion an agent asked for was refused as one it had
        already made."""
        target = tmp_path / "app.py"
        target.write_bytes(b"import os\nimport sys\nwork()\n")

        result = _edit(target, old="import sys\n", new="")

        assert result.returncode == 0
        assert target.read_bytes() == b"import os\nwork()\n"

    def test_a_narrowing_edit_applies_on_the_first_attempt(self, tmp_path: Path) -> None:
        """The same failure without deleting anything: whenever ``new`` is a
        substring of ``old``, a file that contains ``old`` contains ``new``,
        so "the result is already present" was true before the edit ran."""
        target = tmp_path / "app.py"
        target.write_bytes(b"call(alpha, beta)\n")

        result = _edit(target, old="call(alpha, beta)", new="call(alpha)")

        assert result.returncode == 0
        assert target.read_bytes() == b"call(alpha)\n"

    def test_the_anagram_edit_defeats_every_content_guard(self, tmp_path: Path) -> None:
        """And this is why the answer is not a better content guard.

        ``call(alpha, alpha, beta)``, old ``alpha, beta``, new ``beta, alpha``.
        Applying it once gives ``call(alpha, beta, alpha)`` — in which
        ``alpha, beta`` still occurs exactly once, so the count agrees again,
        and which contains ``beta, alpha``, so "already applied" agrees too.
        Both guards pass and the file changes a second time.

        The test asserts the damage rather than a refusal, because the job is
        not where this is stopped: two applications of this edit are two
        dispatches of one invocation, and the runner hands the second the
        first one's job instead of a container.
        """
        target = tmp_path / "app.py"
        target.write_bytes(b"call(alpha, alpha, beta)\n")

        assert _edit(target, old="alpha, beta", new="beta, alpha").returncode == 0
        assert target.read_bytes() == b"call(alpha, beta, alpha)\n"

        assert _edit(target, old="alpha, beta", new="beta, alpha").returncode == 0
        assert target.read_bytes() == b"call(beta, alpha, alpha)\n"


class TestTheEditIsAllOrNothing:
    def test_the_file_keeps_its_mode(self, tmp_path: Path) -> None:
        """The write is staged in a fresh file and renamed over the target, so
        the mode has to be carried across deliberately — otherwise every
        script an agent edited would come back unexecutable."""
        if os.name == "nt":  # pragma: no cover - POSIX modes only
            pytest.skip("file modes are a POSIX notion")
        target = tmp_path / "run.sh"
        target.write_bytes(b"echo one\n")
        target.chmod(0o755)

        assert _edit(target, old="one", new="two").returncode == 0

        assert target.read_bytes() == b"echo two\n"
        assert oct(target.stat().st_mode)[-3:] == "755"

    def test_it_leaves_no_staging_file_behind_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"x = 1\n")

        assert _edit(target, old="x = 1", new="x = 2").returncode == 0

        assert [entry.name for entry in tmp_path.iterdir()] == ["app.py"]

    def test_a_file_that_is_not_text_is_refused_untouched(self, tmp_path: Path) -> None:
        target = tmp_path / "blob.bin"
        target.write_bytes(b"\xff\xfe\x00binary")

        result = _edit(target, old="binary", new="text")

        assert (result.returncode, _refusal(result)) == (65, "file_not_text")
        assert target.read_bytes() == b"\xff\xfe\x00binary"


class TestTheEditExecutorReportsTheRefusal:
    async def test_a_deletion_reaches_a_container(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The regression at the executor's own level: an edit that removes a
        line is an ordinary edit and must be dispatched like one."""
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)

        await cli_tools._file_edit(
            linked_context,
            FileEditInput(
                connection_id=str(cli.id),
                path="app.py",
                old_string="import sys\n",
                new_string="",
            ),
        )

        assert len(stub.payloads) == 1
        assert stub.payloads[0]["env"]["JHIN_EDIT_NEW"] == ""

    async def test_every_job_carries_the_invocation_it_belongs_to(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Which is the whole of this tool's redispatch story now. The
        executor's job is to say which call this is; the runner's is to
        recognise the second dispatch of it."""
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)

        await cli_tools._file_edit(
            linked_context,
            FileEditInput(
                connection_id=str(cli.id),
                path="app.py",
                old_string="a",
                new_string="b",
            ),
        )

        assert stub.payloads[0]["invocation_id"] == str(linked_context.tool_call_id)

    async def test_an_edit_that_would_change_nothing_never_reaches_a_container(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id),
                    path="app.py",
                    old_string="import os",
                    new_string="import os",
                ),
            )

        assert raised.value.code == "edit_changes_nothing"
        assert raised.value.side_effect_possible is False
        assert stub.payloads == []


class TestWhatTheExecutorTellsTheRunnerAboutTheInvocation:
    """The two facts the runner needs and cannot obtain for itself.

    Which call this is — so a second dispatch is recognisable as the same one
    — and when the first dispatch of it began, which is the only thing that
    tells a runner with no record of an invocation whether it is looking at a
    new call or at one of its own that it forgot when it restarted.
    """

    async def test_a_first_dispatch_names_no_earlier_one(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)

        await cli_tools._file_edit(
            linked_context,
            FileEditInput(connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"),
        )

        assert stub.payloads[0]["prior_dispatch_at"] == ""

    async def test_a_second_dispatch_names_the_first(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)
        payload = FileEditInput(
            connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
        )

        await cli_tools._file_edit(linked_context, payload)
        await cli_tools._file_edit(linked_context, payload)

        rows = list(
            await linked_context.session.scalars(
                select(SandboxJob).order_by(SandboxJob.started_at, SandboxJob.id)
            )
        )
        assert len(rows) == 2
        first_started = rows[0].started_at
        assert first_started is not None
        # The earliest job of this tool call, not the one being submitted.
        assert stub.payloads[1]["prior_dispatch_at"].startswith(first_started.isoformat()[:19])
        assert stub.payloads[1]["invocation_id"] == stub.payloads[0]["invocation_id"]


class TestARunnerThatWillNotVouchForTheEarlierAttempt:
    async def test_it_becomes_a_named_failure_that_stops_for_a_person(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing ran *this* time, and the previous time is unknowable. So
        the failure is finished but ``side_effect_possible`` — a claim about
        the earlier dispatch, not this one: it may have changed the workspace
        and nobody can say."""

        async def refuse(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict[str, Any]:
            raise SandboxInvocationUnknownError(
                "sandbox runner would not repeat this call: an earlier dispatch began "
                "before this sandbox runner started"
            )

        monkeypatch.setattr(cli_tools, "run_sandbox_job", refuse)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert raised.value.code == "redispatch_unprovable"
        assert raised.value.side_effect_possible is True

    async def test_a_read_says_the_earlier_attempt_changed_nothing(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The refusal is uniform at the boundary, because the runner cannot
        know which tool this is. What it *means* is not uniform, and this is
        the tool that shows why: an unaccounted-for earlier listing changed
        nothing, so this is a plain failure the agent can simply make again
        rather than a run that ends for a person to reconcile."""

        async def refuse(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict[str, Any]:
            raise SandboxInvocationUnknownError("no record of the earlier dispatch")

        monkeypatch.setattr(cli_tools, "run_sandbox_job", refuse)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_list(linked_context, FileListInput(connection_id=str(cli.id)))

        assert raised.value.code == "redispatch_unprovable"
        assert raised.value.side_effect_possible is False

    async def test_the_job_row_is_closed_rather_than_left_running(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """This refusal happens before any container exists, so unlike an
        abandoned job there is nothing for the sweep to find out later. The
        row is finished here."""

        async def refuse(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict[str, Any]:
            raise SandboxInvocationUnknownError("no record of the earlier dispatch")

        monkeypatch.setattr(cli_tools, "run_sandbox_job", refuse)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError):
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        [row] = list(await linked_context.session.scalars(select(SandboxJob)))
        assert row.status == SandboxJobStatus.FAILED.value
        assert row.error_code == "redispatch_unprovable"
        assert row.completed_at is not None


class _SessionsThatCannotBeOpened:
    """A session factory whose sessions never open.

    The exact shape that made the old lookup answer ``""`` while emitting
    nothing anywhere: the failure is in ``__aenter__``, so there is no session,
    no statement, and no driver error to read afterwards.
    """

    def __call__(self) -> _SessionsThatCannotBeOpened:
        return self

    async def __aenter__(self) -> None:
        raise OSError("connection refused by the database")

    async def __aexit__(self, *_exception: object) -> bool:
        return False


class _SessionThatCannotRead:
    """A session that is open and answers every read with a driver failure."""

    async def scalar(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("server closed the connection unexpectedly")


class TestTheAnswerThatDecidesWhatPriorDispatchAtSays:
    """Three answers, and the third one is a different type on purpose.

    ``prior_dispatch_at`` is the only interlock left when the runner has
    restarted: its ledger is empty by construction, so the empty string —
    "there was no earlier dispatch" — is what makes it start a container. A
    lookup that fails must therefore not produce that string, and the way it
    cannot is that the answer for "I could not find out" has no such field to
    read.
    """

    async def test_a_first_dispatch_is_answered_no(
        self, linked_context: ToolExecutionContext
    ) -> None:
        history = await cli_tools._dispatch_history(linked_context)

        assert isinstance(history, cli_tools._FirstDispatch)
        assert history.prior_dispatch_at == ""

    async def test_a_call_with_no_invocation_identity_is_answered_no(
        self, context: ToolExecutionContext
    ) -> None:
        """Nothing to recognise a repeat by, so the runner is offered nothing
        and never reads this field. That is an answer, not an unknown."""
        history = await cli_tools._dispatch_history(context)

        assert isinstance(history, cli_tools._FirstDispatch)
        assert "no invocation identity" in history.basis

    async def test_an_earlier_job_of_this_call_is_named_with_its_moment(
        self, workspace: Workspace, linked_context: ToolExecutionContext
    ) -> None:
        started = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
        linked_context.session.add(
            SandboxJob(
                id=new_uuid7(),
                workspace_id=workspace.id,
                run_id=linked_context.run_id,
                task_id=linked_context.task_id,
                tool_call_id=linked_context.tool_call_id,
                status=SandboxJobStatus.RUNNING.value,
                image="jhin-sandbox:latest",
                command="ls",
                network_policy="none",
                timeout_seconds=60,
                started_at=started,
            )
        )
        await linked_context.session.flush()

        history = await cli_tools._dispatch_history(linked_context)

        assert isinstance(history, cli_tools._EarlierDispatch)
        assert history.prior_dispatch_at.startswith(started.isoformat()[:19])

    async def test_a_factory_that_cannot_open_a_session_answers_neither(
        self, linked_context: ToolExecutionContext
    ) -> None:
        history = await cli_tools._dispatch_history(
            replace(linked_context, session_factory=cast(Any, _SessionsThatCannotBeOpened()))
        )

        assert isinstance(history, cli_tools._UnknownDispatchHistory)
        # The reason is carried, not swallowed: this is the failure that used
        # to leave no trace at any level.
        assert "connection refused by the database" in history.reason
        # And there is no way to spell it on the wire, which is the point.
        assert not hasattr(history, "prior_dispatch_at")

    async def test_a_read_that_fails_answers_neither(
        self, linked_context: ToolExecutionContext
    ) -> None:
        history = await cli_tools._dispatch_history(
            replace(linked_context, session=cast(Any, _SessionThatCannotRead()))
        )

        assert isinstance(history, cli_tools._UnknownDispatchHistory)
        assert "server closed the connection" in history.reason


class TestACallWhoseHistoryCannotBeRead:
    """The interlock fails closed.

    Inside one runner incarnation the ledger settles this and the lookup is
    redundant. Across a restart the ledger is empty and the lookup is the only
    thing left — so the moment it cannot answer is exactly the moment its
    answer matters, and answering "no earlier dispatch" there is how an edit
    gets applied twice.
    """

    @staticmethod
    def _unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
        async def unreadable(ctx: ToolExecutionContext) -> cli_tools._DispatchHistory:
            return cli_tools._UnknownDispatchHistory(reason="OSError: connection refused")

        monkeypatch.setattr(cli_tools, "_dispatch_history", unreadable)

    async def test_nothing_is_submitted_and_the_failure_says_why(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        self._unreadable(monkeypatch)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert raised.value.code == "redispatch_uncheckable"
        # About the *earlier* attempt, which may have written; this one
        # certainly did nothing.
        assert raised.value.side_effect_possible is True
        assert "connection refused" in raised.value.detail
        assert stub.payloads == []

    async def test_no_job_row_is_written_for_a_job_that_never_existed(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing was submitted, so there is nothing to describe — and the
        connection that would record it is the one that just failed."""
        monkeypatch.setattr(cli_tools, "run_sandbox_job", _CaptureRunner())
        self._unreadable(monkeypatch)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError):
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert list(await linked_context.session.scalars(select(SandboxJob))) == []

    async def test_a_read_only_tool_says_the_earlier_attempt_changed_nothing(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same refusal, different meaning — as with the runner's own. An
        unaccounted-for earlier listing changed no disk, so the agent may
        simply ask again."""
        monkeypatch.setattr(cli_tools, "run_sandbox_job", _CaptureRunner())
        self._unreadable(monkeypatch)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_list(linked_context, FileListInput(connection_id=str(cli.id)))

        assert raised.value.code == "redispatch_uncheckable"
        assert raised.value.side_effect_possible is False


def _integrity_error() -> IntegrityError:
    """What the database says when it refuses this row on a constraint.

    Deliberately anonymous, because the product cannot tell either:
    ``sandbox_job`` references a workspace, a run, a task and a tool call, and
    a foreign-key message is the only place the answer would be.
    """
    return IntegrityError(
        "INSERT INTO sandbox_job ...",
        {},
        Exception("FOREIGN KEY constraint failed"),
    )


class _SessionsThatCannotCommit:
    """A session factory whose sessions open and take rows, and whose commit
    then fails with whatever it was given.

    The two failures it is given below arrive differently and end the same
    way: one is the database understanding the statement and refusing it, the
    other is the connection dropping. Neither is a licence to write the record
    of a dispatch somewhere else.
    """

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.added: list[object] = []

    def __call__(self) -> _SessionsThatCannotCommit:
        return self

    async def __aenter__(self) -> _SessionsThatCannotCommit:
        return self

    async def __aexit__(self, *_exception: object) -> bool:
        return False

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        raise self.error


class TestWhereTheRecordOfADispatchIsAllowedToLive:
    """Two homes, and no third one that a reader can shrug at.

    The row is what a *later* dispatch of this call reads to learn that this
    one happened, so where it lives is not bookkeeping. It used to be a
    boolean set inside a bare ``except``, consulted only to pick where the
    next audit row went — so a dropped connection quietly moved the row into
    the caller's transaction, the job was dispatched anyway, and the gateway's
    rollback then erased the only evidence that a container had started.
    """

    @staticmethod
    def _row(ctx: ToolExecutionContext, workspace: Workspace) -> SandboxJob:
        return SandboxJob(
            id=new_uuid7(),
            workspace_id=workspace.id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            tool_call_id=ctx.tool_call_id,
            status=SandboxJobStatus.RUNNING.value,
            image="jhin-sandbox:latest",
            command="ls",
            network_policy="none",
            timeout_seconds=60,
            started_at=datetime.now(UTC),
        )

    async def test_no_isolated_connection_writes_into_the_call_and_says_why(
        self, workspace: Workspace, linked_context: ToolExecutionContext
    ) -> None:
        """The unit-test and no-factory shape, unchanged — and sound for a
        stated reason: the history read runs on this same transaction, so it
        cannot fail to see a row this transaction lost."""
        row = self._row(linked_context, workspace)

        home = await cli_tools._open_job_record(
            linked_context, row, network="none", shared={}, metadata={"timeout_seconds": 60}
        )

        assert isinstance(home, cli_tools._CallersTransaction)
        assert "no isolated session factory" in home.basis
        assert list(await linked_context.session.scalars(select(SandboxJob))) == [row]

    async def test_a_constraint_refusal_refuses_too_rather_than_naming_a_cause(
        self, workspace: Workspace, linked_context: ToolExecutionContext
    ) -> None:
        """This used to be the second home, and its reasoning was wrong twice.

        It read a constraint refusal as proof that the ``tool_call`` this row
        points at is uncommitted — but ``sandbox_job`` has four foreign keys,
        not three (``workspace_id`` is one, and it is ``NOT NULL``), plus a
        primary key, so an ``IntegrityError`` names none of them. And in
        production the branch could not be reached for a different reason than
        the one it gave: the gateway commits the ``tool_call`` row before an
        executor is entered, and everything else this row references is older
        still. What the fallback actually did was put the record of a dispatch
        somewhere the dispatch-history read — which uses this same factory —
        would read straight past.
        """
        row = self._row(linked_context, workspace)
        sessions = _SessionsThatCannotCommit(_integrity_error())

        with pytest.raises(cli_tools._UnrecordableDispatch) as raised:
            await cli_tools._open_job_record(
                replace(linked_context, session_factory=cast(Any, sessions)),
                row,
                network="none",
                shared={},
                metadata={"timeout_seconds": 60},
            )

        assert "IntegrityError" in raised.value.reason
        assert list(await linked_context.session.scalars(select(SandboxJob))) == []

    async def test_any_other_failure_refuses_instead_of_demoting_the_row(
        self, workspace: Workspace, linked_context: ToolExecutionContext
    ) -> None:
        """The regression this type exists for. A connection that dropped
        proves nothing about the row, and writing it into the caller's
        transaction makes the record of a dispatch conditional on a rollback
        that the same failing connection makes likely."""
        row = self._row(linked_context, workspace)
        sessions = _SessionsThatCannotCommit(OSError("server closed the connection"))

        with pytest.raises(cli_tools._UnrecordableDispatch) as raised:
            await cli_tools._open_job_record(
                replace(linked_context, session_factory=cast(Any, sessions)),
                row,
                network="none",
                shared={},
                metadata={"timeout_seconds": 60},
            )

        assert "server closed the connection" in raised.value.reason
        # Nothing was written anywhere, least of all the transaction that
        # would have carried it silently.
        assert list(await linked_context.session.scalars(select(SandboxJob))) == []


class TestADispatchThatCouldNotBeWrittenDown:
    """It is refused, and the refusal knows what it is refusing.

    The failure is on the way *in*: nothing has been submitted, so this
    attempt has certainly done nothing. What an earlier attempt did is not an
    open question here — the history read already succeeded — which is what
    lets this refusal be the cheap one when there was no earlier attempt.
    """

    @staticmethod
    def _unrecordable(monkeypatch: pytest.MonkeyPatch) -> None:
        async def unrecordable(
            ctx: ToolExecutionContext,
            row: SandboxJob,
            *,
            network: str,
            shared: Any,
            metadata: Any,
        ) -> cli_tools._EvidenceHome:
            raise cli_tools._UnrecordableDispatch("OSError: server closed the connection")

        monkeypatch.setattr(cli_tools, "_open_job_record", unrecordable)

    async def test_nothing_is_submitted_and_the_failure_says_why(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        self._unrecordable(monkeypatch)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert raised.value.code == "redispatch_uncheckable"
        assert "server closed the connection" in raised.value.detail
        assert stub.payloads == []

    async def test_a_first_dispatch_that_never_left_is_a_plain_retry(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An edit, and still ``side_effect_possible=False``. There was no
        earlier dispatch — the history read said so before this failed — and
        this one submitted nothing, so no container has ever run for this call
        and there is nothing for a person to reconcile."""
        monkeypatch.setattr(cli_tools, "run_sandbox_job", _CaptureRunner())
        self._unrecordable(monkeypatch)
        cli = await _cli_connection(make_connection, workspace)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert raised.value.side_effect_possible is False

    async def test_a_re_dispatch_stops_for_a_person_over_the_attempt_before_it(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An earlier dispatch of this call is on record, and refusing here
        means nobody ever finds out what its container did to the disk."""
        monkeypatch.setattr(cli_tools, "run_sandbox_job", _CaptureRunner())
        cli = await _cli_connection(make_connection, workspace)
        linked_context.session.add(
            TestWhereTheRecordOfADispatchIsAllowedToLive._row(linked_context, workspace)
        )
        await linked_context.session.flush()
        self._unrecordable(monkeypatch)

        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_edit(
                linked_context,
                FileEditInput(
                    connection_id=str(cli.id), path="app.py", old_string="a", new_string="b"
                ),
            )

        assert raised.value.side_effect_possible is True


@_needs_bash
class TestTheWriteScriptKeepsItsPromises:
    """The read token's promise, run rather than described.

    The token is a promise to a *person*: a write built on a partial read
    cannot silently drop the rest of a file. It is not what makes the write
    safe to re-dispatch — that is settled at the runner, one dispatch per
    invocation — and it never could be, because it says nothing about a write
    of the bytes already there. The last test here is that case, accepted.
    """

    async def _script(
        self,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        path: str,
        content: str,
        read_token: str,
    ) -> tuple[list[str], dict[str, str]]:
        stub = _CaptureRunner()
        monkeypatch.setattr(cli_tools, "run_sandbox_job", stub)
        cli = await _cli_connection(make_connection, workspace)
        await cli_tools._file_write(
            linked_context,
            FileWriteInput(
                connection_id=str(cli.id),
                path=path,
                content=content,
                read_token=read_token,
            ),
        )
        payload = stub.payloads[-1]
        return list(payload["command"]), dict(payload["env"])

    @staticmethod
    def _run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert _BASH is not None
        environment = dict(os.environ)
        environment.update(env)
        return subprocess.run(
            [_BASH, *argv[1:]],
            capture_output=True,
            text=True,
            env=environment,
            cwd=cwd,
            check=False,
        )

    async def test_a_second_write_of_a_changed_file_is_refused(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "app.py"
        target.write_bytes(b"old\n")
        token = hashlib.sha256(b"old\n").hexdigest()
        argv, env = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="app.py",
            content="new\n",
            read_token=token,
        )

        first = self._run(argv, env, tmp_path)
        assert first.returncode == 0
        assert target.read_bytes() == b"new\n"

        second = self._run(argv, env, tmp_path)
        assert (second.returncode, _refusal(second)) == (65, "file_changed")
        assert target.read_bytes() == b"new\n"

    async def test_a_second_create_is_refused_because_the_file_now_exists(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv, env = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="fresh.py",
            content="hello\n",
            read_token="",
        )

        assert self._run(argv, env, tmp_path).returncode == 0
        second = self._run(argv, env, tmp_path)

        assert (second.returncode, _refusal(second)) == (65, "file_exists_pass_read_token")
        assert (tmp_path / "fresh.py").read_bytes() == b"hello\n"

    async def test_the_one_repeat_that_is_accepted_writes_the_same_bytes(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Writing a file's own content back leaves the hash unchanged, so the
        token still matches and the second write is allowed. It is allowed
        because it is a repeat of nothing: the same bytes, in the same file."""
        target = tmp_path / "app.py"
        target.write_bytes(b"same\n")
        token = hashlib.sha256(b"same\n").hexdigest()
        argv, env = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="app.py",
            content="same\n",
            read_token=token,
        )

        assert self._run(argv, env, tmp_path).returncode == 0
        assert self._run(argv, env, tmp_path).returncode == 0
        assert target.read_bytes() == b"same\n"

    async def test_the_write_is_staged_and_renamed_rather_than_truncated(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``printf ... > file`` truncates and then writes up to 48,000
        characters through stdio, so a container killed between two flushes
        leaves the file cut off at a buffer boundary. Staging beside the file
        and renaming makes that impossible: rename is atomic, so the file is
        the old one entire or the new one entire at every instant.

        Asserted on the script itself, because the property is about an
        instant that a passing subprocess never visits.
        """
        argv, _ = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="app.py",
            content="x\n",
            read_token="",
        )
        script = argv[-1]

        assert "mktemp" in script
        assert 'mv -f -- "$jhin_staged" "$jhin_target"' in script
        # And nothing writes to the target directly any more.
        assert '> "$jhin_target"' not in script

    async def test_an_existing_file_keeps_its_mode(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``mktemp`` creates 0600 and the rename would carry that over, which
        would quietly un-execute every script an agent rewrote."""
        if os.name == "nt":  # pragma: no cover - POSIX modes only
            pytest.skip("file modes are a POSIX notion")
        target = tmp_path / "run.sh"
        target.write_bytes(b"echo one\n")
        target.chmod(0o755)
        token = hashlib.sha256(b"echo one\n").hexdigest()
        argv, env = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="run.sh",
            content="echo two\n",
            read_token=token,
        )

        assert self._run(argv, env, tmp_path).returncode == 0

        assert target.read_bytes() == b"echo two\n"
        assert oct(target.stat().st_mode)[-3:] == "755"

    async def test_a_successful_write_leaves_no_staging_file(
        self,
        tmp_path: Path,
        workspace: Workspace,
        linked_context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv, env = await self._script(
            workspace,
            linked_context,
            make_connection,
            monkeypatch,
            path="fresh.py",
            content="hello\n",
            read_token="",
        )

        assert self._run(argv, env, tmp_path).returncode == 0

        assert sorted(os.listdir(tmp_path)) == ["fresh.py"]
