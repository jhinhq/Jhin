"""The fake GitHub git smart-HTTP layer: real clone/branch/push round trip
with a real git client, plus auth and REST-state sync (plan 45 Phase 6)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from jhin_connectors.testing.fake_git import list_git_branches
from jhin_connectors.testing.fake_github import FakeGitHubServer

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TOKEN = "fake-github-pat"


@pytest.fixture
def server(tmp_path: Path):
    with FakeGitHubServer(token=TOKEN, repos="octo/alpha", git_root=str(tmp_path / "git")) as srv:
        yield srv


def run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd or "/tmp"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def authed_url(server: FakeGitHubServer, repo: str = "octo/alpha") -> str:
    host = server.base_url.removeprefix("http://")
    return f"http://x-access-token:{TOKEN}@{host}/git/{repo}.git"


class TestGitSmartHttp:
    def test_clone_requires_valid_token(self, server: FakeGitHubServer, tmp_path: Path) -> None:
        host = server.base_url.removeprefix("http://")
        bad = run_git(
            "clone", f"http://x:wrong-token@{host}/git/octo/alpha.git", str(tmp_path / "denied")
        )
        assert bad.returncode != 0
        assert "401" in bad.stderr or "Authentication" in bad.stderr

    def test_clone_edit_push_roundtrip_updates_rest_state(
        self, server: FakeGitHubServer, tmp_path: Path
    ) -> None:
        work = tmp_path / "clone"
        assert run_git("clone", authed_url(server), str(work)).returncode == 0

        # Seeded content: failing test target.
        assert (work / "app.py").read_text().startswith("VALUE = 1")
        assert (work / "run_tests.sh").exists()

        assert run_git("checkout", "-b", "agent/t1-fix", cwd=work).returncode == 0
        (work / "app.py").write_text("VALUE = 2\n")
        assert run_git("commit", "-am", "fix value", cwd=work).returncode == 0
        push = run_git("push", "origin", "agent/t1-fix", cwd=work)
        assert push.returncode == 0, push.stderr

        # The pushed branch is visible in the bare repo...
        branches = list_git_branches(server.state.git_root or "", "octo/alpha")
        assert "agent/t1-fix" in branches

        # ...and immediately usable as a PR head through the REST API.
        response = httpx.post(
            f"{server.base_url}/repos/octo/alpha/pulls",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"title": "Fix", "head": "agent/t1-fix", "base": "main", "body": ""},
        )
        assert response.status_code == 201, response.text
        assert response.json()["head"]["ref"] == "agent/t1-fix"

        # _state exposes the git-synced branches for exit-test assertions.
        state = httpx.get(f"{server.base_url}/_state").json()
        assert state["git_enabled"] is True
        assert "agent/t1-fix" in state["repos"]["octo/alpha"]["branches"]

    def test_seeded_test_script_fails_then_passes(
        self, server: FakeGitHubServer, tmp_path: Path
    ) -> None:
        work = tmp_path / "clone2"
        assert run_git("clone", authed_url(server), str(work)).returncode == 0
        failing = subprocess.run(["bash", "./run_tests.sh"], cwd=work, capture_output=True)
        assert failing.returncode != 0
        (work / "app.py").write_text("VALUE = 2\n")
        passing = subprocess.run(
            ["bash", "./run_tests.sh"], cwd=work, capture_output=True, text=True
        )
        assert passing.returncode == 0
        assert "tests passed" in passing.stdout
