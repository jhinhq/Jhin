"""What the checkout does to a real repository across two runs.

The question this file exists for is git's, not Jhin's: **can a second run
build on what the first one pushed?** That is the whole promise of a durable
per-agent workspace, and it was broken. The refresh ended in
``git checkout -B <branch> FETCH_HEAD``, which force-moved the working branch
back onto the base ref, so run two started by rewinding past run one's commit,
did its work on top of the base, and had its push rejected as a
non-fast-forward — with the run's changes stranded in the sandbox and nothing
published. Reproduced end to end, and no assertion about the script's text
would have caught it: every line in it was doing exactly what it said.

So the runner here is a real one. It takes the script the executor actually
submits, points ``/workspace`` and the clone URL at a directory and a bare
repository on disk, runs it through ``bash`` and ``git``, and answers with what
it printed — so the executor parses its own trailer, records the config sha it
really left behind, and the next checkout's reuse check is the product's, not a
fixture's. Pushing between runs is a plain
``git push <url> refs/heads/B:refs/heads/B``: the refspec
``cli.repository.push`` builds, and like it never forced, so a branch that has
been rewound underneath it is rejected exactly as it was in the sandbox.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jhin_connectors.cli import tools as cli_tools
from jhin_connectors.cli.schemas import RepositoryCheckoutInput, RepositoryCheckoutOutput
from jhin_db.models import Workspace
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext

pytestmark = pytest.mark.anyio

TOKEN = "ghp_sandbox_secret_token_9876"
# Nothing is served here: every job is answered by the sandbox below, and the
# origin exists only to be the URL the executor builds and the allow-list it
# checks against.
GITHUB_ORIGIN = "http://git-remote.invalid:8080"
CLONE_URL = f"{GITHUB_ORIGIN}/git/octo/alpha.git"


def _run_bash(bash: str, script: str) -> subprocess.CompletedProcess[str]:
    # Git Bash may inherit a Windows PATH without its own utilities. Give the
    # probe and runner the same POSIX paths, without sourcing user startup files.
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", 'export PATH="/usr/bin:/bin:$PATH"\n' + script],
        env={key: value for key, value in os.environ.items() if key not in {"BASH_ENV", "ENV"}},
        capture_output=True,
        text=True,
        check=False,
    )


def _working_bash() -> str | None:
    """A ``bash`` that can actually run a script.

    ``which`` is not enough on Windows, where the first ``bash`` on PATH is
    usually the WSL launcher: it resolves, and then fails to execute anything
    with a POSIX path. Git ships a real one beside itself, so the candidates
    are derived from wherever ``git`` is rather than from a machine's layout,
    and each one is asked to prove it works.
    """
    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git is not None:
        root = Path(git).resolve().parent.parent
        candidates += [str(root / "bin" / "bash.exe"), str(root / "usr" / "bin" / "bash.exe")]
    for candidate in candidates:
        if candidate is None or not Path(candidate).exists():
            continue
        probe = _run_bash(
            candidate,
            "set -e -o pipefail\n"
            "for utility in git awk sed grep head sha256sum cut wc tr find mkdir rm "
            "sort base64; do\n"
            '  command -v "$utility" >/dev/null || exit 127\n'
            "done\n"
            "printf 'ok\\n' | awk '{print $1}' | sed -n '1p'",
        )
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


_BASH = _working_bash()
_GIT = shutil.which("git")
_needs_shell_and_git = pytest.mark.skipif(
    _BASH is None or _GIT is None,
    reason="the checkout is a shell script over git; running it needs both",
)


def _git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False)


def _remove_tree(path: Path) -> None:
    """``rm -rf`` for a git tree on Windows, where pack and object files are
    read-only and refuse to be unlinked until they are not."""

    def force(function: Any, target: str, _exception: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=force)


async def _wired(make_connection, workspace: Workspace, monkeypatch):
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", GITHUB_ORIGIN)
    github = await make_connection(
        workspace,
        connector_type="github",
        name=f"GitHub {new_uuid7().hex[:6]}",
        credentials={"token": TOKEN},
        config={"base_url": GITHUB_ORIGIN},
    )
    return await make_connection(
        workspace,
        connector_type="cli",
        name=f"cli-{new_uuid7().hex[:6]}",
        auth_type="none",
        credentials={},
        config={
            "default_image": "jhin-sandbox:latest",
            "default_network": "none",
            "git_connection_id": str(github.id),
            "allowed_repositories": ["octo/*"],
        },
    )


class _Sandbox:
    """One agent workspace, its remote, and the runner that runs Jhin's script.

    Two substitutions stand in for the container, and nothing else is touched:
    ``/workspace`` becomes a directory, and the clone URL becomes a bare
    repository. Everything the executor sends — the reuse prologue, the branch
    selection, the trailer, the credential arguments — is what it would send to
    a container.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = self._seed(root)
        self.workspace = root / "ws"
        self.workspace.mkdir()

    @staticmethod
    def _seed(root: Path) -> Path:
        remote = root / "remote.git"
        seed = root / "seed"
        seed.mkdir()
        assert _git("init", "--bare", "-q", "-b", "main", str(remote), cwd=root).returncode == 0
        assert _git("init", "-q", "-b", "main", ".", cwd=seed).returncode == 0
        _git("config", "user.email", "seed@example.com", cwd=seed)
        _git("config", "user.name", "Seed", cwd=seed)
        (seed / "app.py").write_text("hello\n", encoding="utf-8")
        assert _git("add", "-A", cwd=seed).returncode == 0
        assert _git("commit", "-qm", "base", cwd=seed).returncode == 0
        assert _git("push", "-q", str(remote), "main", cwd=seed).returncode == 0
        return remote

    @property
    def checkout(self) -> Path:
        return self.workspace / "repo"

    async def __call__(
        self, payload: dict[str, Any], *, job_timeout_seconds: int
    ) -> dict[str, Any]:
        script = str(payload["command"][2])
        runnable = script.replace("/workspace", self.workspace.as_posix()).replace(
            CLONE_URL, shlex.quote(self.remote.as_posix())
        )
        assert _BASH is not None
        # Blocking on purpose: this stands in for a container the executor
        # waits on, and nothing else is running on this loop.
        result = _run_bash(_BASH, runnable)
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "duration_ms": 5,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    def commit(self, message: str) -> str:
        (self.checkout / "app.py").write_text(f"{message}\n", encoding="utf-8")
        assert _git("add", "-A", cwd=self.checkout).returncode == 0
        assert _git("commit", "-qm", message, cwd=self.checkout).returncode == 0
        return _git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip()

    def push(self, branch: str) -> subprocess.CompletedProcess[str]:
        return _git(
            "push",
            self.remote.as_posix(),
            f"refs/heads/{branch}:refs/heads/{branch}",
            cwd=self.checkout,
        )

    def remote_head(self, ref: str) -> str:
        return _git("rev-parse", f"refs/heads/{ref}", cwd=self.remote).stdout.strip()


@_needs_shell_and_git
class TestASecondRunBuildsOnTheFirst:
    """Two runs of one task, on the workspace that task keeps."""

    async def _checkout(
        self,
        sandbox: _Sandbox,
        ctx: ToolExecutionContext,
        connection_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> RepositoryCheckoutOutput:
        monkeypatch.setattr(cli_tools, "run_sandbox_job", sandbox)
        output = await cli_tools._repository_checkout(
            ctx,
            RepositoryCheckoutInput(connection_id=connection_id, repository="octo/alpha"),
        )
        assert isinstance(output, RepositoryCheckoutOutput)
        return output

    async def test_the_branch_is_continued_and_the_second_push_is_accepted(
        self,
        tmp_path: Path,
        workspace: Workspace,
        context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rewind is what this pins: before the fix, run two's checkout put
        the branch back on ``main`` and its push came back
        ``! [rejected] ... (non-fast-forward)``."""
        cli = await _wired(make_connection, workspace, monkeypatch)
        sandbox = _Sandbox(tmp_path)
        # One task, two runs. The branch name is the task's, so both runs of it
        # ask for the same branch.
        task = replace(context, tool_call_id=new_uuid7())

        first = await self._checkout(sandbox, task, str(cli.id), monkeypatch)
        assert (first.started_from, first.reused) == ("base", False)
        run_one = sandbox.commit("run one")
        assert sandbox.push(first.branch).returncode == 0

        second = await self._checkout(
            sandbox, replace(task, tool_call_id=new_uuid7()), str(cli.id), monkeypatch
        )

        assert second.branch == first.branch
        assert second.reused is True
        assert second.started_from == "workspace_branch"
        # Run two starts where run one left the branch, not at the base.
        assert second.head_sha == run_one
        assert second.base_ref == "main"
        run_two = sandbox.commit("run two")
        pushed = sandbox.push(second.branch)
        assert pushed.returncode == 0, pushed.stderr
        assert sandbox.remote_head(second.branch) == run_two

    async def test_the_branch_is_resumed_even_when_the_disk_is_not(
        self,
        tmp_path: Path,
        workspace: Workspace,
        context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The branch lives on the remote, so resuming it cannot depend on the
        workspace surviving: eviction, a purge and a re-clone all leave the
        published branch exactly where it was."""
        cli = await _wired(make_connection, workspace, monkeypatch)
        sandbox = _Sandbox(tmp_path)
        task = replace(context, tool_call_id=new_uuid7())

        first = await self._checkout(sandbox, task, str(cli.id), monkeypatch)
        run_one = sandbox.commit("run one")
        assert sandbox.push(first.branch).returncode == 0
        _remove_tree(sandbox.checkout)

        second = await self._checkout(
            sandbox, replace(task, tool_call_id=new_uuid7()), str(cli.id), monkeypatch
        )

        assert (second.reused, second.started_from) == (False, "remote_branch")
        assert second.head_sha == run_one
        sandbox.commit("run two")
        assert sandbox.push(second.branch).returncode == 0

    async def test_work_a_run_never_pushed_is_kept_not_rewound(
        self,
        tmp_path: Path,
        workspace: Workspace,
        context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run that died after committing and before pushing leaves the only
        copy of its work on this disk. The next checkout keeps it."""
        cli = await _wired(make_connection, workspace, monkeypatch)
        sandbox = _Sandbox(tmp_path)
        task = replace(context, tool_call_id=new_uuid7())

        await self._checkout(sandbox, task, str(cli.id), monkeypatch)
        unpushed = sandbox.commit("never pushed")

        second = await self._checkout(
            sandbox, replace(task, tool_call_id=new_uuid7()), str(cli.id), monkeypatch
        )

        assert second.started_from == "workspace_branch"
        assert second.head_sha == unpushed

    async def test_a_diverged_local_branch_loses_to_the_published_one(
        self,
        tmp_path: Path,
        workspace: Workspace,
        context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Local commits that are not on top of what the remote holds cannot be
        pushed without rewriting somebody else's history, so the published tip
        wins — and the head that was abandoned is recorded rather than lost
        silently."""
        cli = await _wired(make_connection, workspace, monkeypatch)
        sandbox = _Sandbox(tmp_path)
        task = replace(context, tool_call_id=new_uuid7())

        first = await self._checkout(sandbox, task, str(cli.id), monkeypatch)
        sandbox.commit("run one")
        assert sandbox.push(first.branch).returncode == 0
        published = sandbox.remote_head(first.branch)
        assert _git("reset", "-q", "--hard", "HEAD~1", cwd=sandbox.checkout).returncode == 0
        diverged = sandbox.commit("a different run one")

        second = await self._checkout(
            sandbox, replace(task, tool_call_id=new_uuid7()), str(cli.id), monkeypatch
        )

        assert second.started_from == "remote_branch"
        assert second.head_sha == published
        record = await cli_tools._last_checkout(context, await cli_tools._binding(context))
        assert record["discarded_head"] == diverged
        assert record["started_from"] == "remote_branch"

    async def test_a_first_checkout_still_cuts_the_branch_from_the_base(
        self,
        tmp_path: Path,
        workspace: Workspace,
        context: ToolExecutionContext,
        make_connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing about resuming a branch changes where a new one starts."""
        cli = await _wired(make_connection, workspace, monkeypatch)
        sandbox = _Sandbox(tmp_path)
        task = replace(context, tool_call_id=new_uuid7())

        output = await self._checkout(sandbox, task, str(cli.id), monkeypatch)

        assert output.started_from == "base"
        assert output.base_ref == "main"
        assert output.head_sha == sandbox.remote_head("main")
        assert output.top_level == ["app.py"]


class TestTheDefaultBranchNameIsTheTasksOwn:
    def test_two_tasks_started_in_the_same_minute_get_different_branches(self) -> None:
        """The collision, at the shape it actually occurred: a uuid7's first
        eight hex characters are the top 32 bits of its millisecond timestamp,
        so they only change every 65.536 seconds. Two tasks a second apart —
        one person asking for two things, a trigger fanning out — took the same
        name, and the second task's checkout resumed the first task's branch.
        """
        one = new_uuid7()
        two = new_uuid7()

        first = cli_tools._default_branch(one, "octo/alpha")
        second = cli_tools._default_branch(two, "octo/alpha")

        assert str(one)[:8] == str(two)[:8]
        assert first != second
        # Still legible, and still the handle an operator pastes back into Jhin
        # to find the task that made the branch.
        assert first == f"agent/alpha-{one}"

    def test_one_task_gets_a_branch_per_repository(self) -> None:
        task = new_uuid7()

        assert cli_tools._default_branch(task, "octo/alpha") != cli_tools._default_branch(
            task, "octo/beta"
        )
