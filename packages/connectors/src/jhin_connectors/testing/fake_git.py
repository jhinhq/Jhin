"""Git smart-HTTP support for the fake GitHub server (plan 45 Phase 6).

The Phase 6 exit test needs real ``git clone``/``git push`` against the fake
environment. The least complex reliable option is a thin CGI bridge to the
stock ``git http-backend`` binary: no extra service, no nginx/fcgiwrap — the
existing stdlib HTTP server execs git per request, exactly as a classic CGI
web server would.

Layout: bare repositories live under ``<git_root>/<owner>/<name>.git`` and
are initialized at startup from the same repo list the REST layer seeds,
with a README, a deliberately failing test target (``app.py`` VALUE=1 +
``run_tests.sh`` asserting VALUE=2), and ``http.receivepack`` enabled.

Auth mirrors the REST layer: HTTP Basic where the *password* must be the
configured PAT or a minted installation token (the username is free-form,
GitHub-style ``x-access-token``). Unauthenticated requests get 401 +
``WWW-Authenticate`` so real git clients retry with credentials.
"""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Callable
from pathlib import Path

SEED_README = "# Seeded fake repository\n\nUsed by Jhin integration tests.\n"
SEED_APP = 'VALUE = 1\n"""Fix me: the test suite expects VALUE == 2."""\n'
SEED_TEST_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
python3 -c "import app; assert app.VALUE == 2, f'expected VALUE == 2, got {app.VALUE}'"
echo "tests passed"
"""

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Jhin Seeder",
    "GIT_AUTHOR_EMAIL": "seed@jhin.local",
    "GIT_COMMITTER_NAME": "Jhin Seeder",
    "GIT_COMMITTER_EMAIL": "seed@jhin.local",
    "HOME": "/tmp",
}


def _git(*args: str, cwd: str | Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**_GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def bare_repo_path(git_root: str, full_name: str) -> Path:
    return Path(git_root) / f"{full_name}.git"


def init_git_root(git_root: str, full_names: list[str]) -> None:
    """Create + seed one bare repository per configured repo (idempotent)."""
    for full_name in full_names:
        bare = bare_repo_path(git_root, full_name)
        if bare.exists():
            continue
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--initial-branch=main", str(bare))
        # Anonymous-CGI push needs this even with REMOTE_USER set.
        _git("config", "http.receivepack", "true", cwd=bare)

        work = bare.parent / f".seed-{bare.name}"
        _git("clone", str(bare), str(work))
        (work / "README.md").write_text(SEED_README)
        (work / "app.py").write_text(SEED_APP)
        script = work / "run_tests.sh"
        script.write_text(SEED_TEST_SCRIPT)
        script.chmod(0o755)
        _git("add", "-A", cwd=work)
        _git("commit", "-m", "Seed repository with failing test", cwd=work)
        _git("push", "origin", "main", cwd=work)
        subprocess.run(["rm", "-rf", str(work)], check=True)


def list_git_branches(git_root: str, full_name: str) -> dict[str, str]:
    """branch name → head sha from the bare repository."""
    bare = bare_repo_path(git_root, full_name)
    if not bare.exists():
        return {}
    output = _git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads", cwd=bare)
    branches: dict[str, str] = {}
    for line in output.splitlines():
        name, _, sha = line.partition(" ")
        if name and sha:
            branches[name] = sha
    return branches


def _authorized_basic(header: str | None, token_check: Callable[[str], bool]) -> bool:
    """GitHub-over-git auth: Basic where the password is a valid token."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
    except Exception:
        return False
    _, _, password = decoded.partition(":")
    return token_check(password)


def handle_git_http(
    git_root: str,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
    token_check: Callable[[str], bool],
) -> tuple[int, dict[str, str], bytes]:
    """Bridge one ``/git/...`` request to ``git http-backend`` (CGI).

    ``path`` is the part after the ``/git`` prefix, e.g.
    ``/octo/alpha.git/info/refs``. Returns (status, headers, body).
    """
    if not _authorized_basic(headers.get("Authorization"), token_check):
        return (
            401,
            {"WWW-Authenticate": 'Basic realm="fake-github"', "Content-Type": "text/plain"},
            b"authentication required",
        )

    cgi_env = {
        **_GIT_ENV,
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_PROJECT_ROOT": git_root,
        "GIT_HTTP_EXPORT_ALL": "1",
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REMOTE_USER": "x-access-token",
        "REMOTE_ADDR": "127.0.0.1",
        "CONTENT_TYPE": headers.get("Content-Type", ""),
        "CONTENT_LENGTH": str(len(body)),
    }
    # git clients may gzip request bodies; http-backend inflates them itself.
    if headers.get("Content-Encoding"):
        cgi_env["HTTP_CONTENT_ENCODING"] = headers["Content-Encoding"]

    result = subprocess.run(
        ["git", "http-backend"],
        input=body,
        env=cgi_env,
        capture_output=True,
        check=False,
    )
    raw = result.stdout
    header_blob, separator, payload = raw.partition(b"\r\n\r\n")
    if not separator:
        header_blob, _, payload = raw.partition(b"\n\n")

    status = 200
    response_headers: dict[str, str] = {}
    for line in header_blob.decode(errors="replace").splitlines():
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name.lower() == "status":
            try:
                status = int(value.split()[0])
            except (ValueError, IndexError):
                status = 500
        elif name:
            response_headers[name] = value
    if result.returncode != 0 and status == 200:
        status = 500
        payload = result.stderr or b"git http-backend failed"
    return status, response_headers, payload
