"""A tiny fake GitHub REST API server (plan 32.2).

Stdlib-only (``http.server``) like the fake model provider, so it runs as a
pytest fixture, on a dev host, or as a compose service
(``python -m jhin_connectors.testing.fake_github``). It implements exactly
the endpoints the GitHub connector tools use, plus GitHub App token minting
and a ``/_state`` inspection endpoint for exit tests.

Auth model:

- PAT: ``Authorization: Bearer <FAKE_GITHUB_TOKEN>`` (default
  ``fake-github-pat``);
- GitHub App: any structurally JWT-shaped bearer is accepted at ``GET /app``
  and ``POST /app/installations/{id}/access_tokens``; the minted
  ``ghs_fake_…`` token is then accepted like a PAT.

State: repositories come from ``FAKE_GITHUB_REPOS`` (default
``octo/alpha,octo/beta``) with a ``main`` default branch, one seeded file,
and one seeded issue; branches/PRs/comments created through the API are held
in memory and visible via ``GET /_state``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_TOKEN = "fake-github-pat"
DEFAULT_REPOS = "octo/alpha,octo/beta"

_SEED_FILE_PATH = "README.md"
_SEED_FILE_CONTENT = "# Seeded fake repository\n\nUsed by Jhin integration tests.\n"
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")


def _sha(seed: str) -> str:
    import hashlib

    return hashlib.sha1(seed.encode()).hexdigest()


class FakeGitHubState:
    """In-memory GitHub: repos, branches, files, issues, PRs, workflow runs."""

    def __init__(self, *, token: str = DEFAULT_TOKEN, repos: str = DEFAULT_REPOS) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.minted_tokens: set[str] = set()
        self.mint_count = 0
        self.repos: dict[str, dict[str, Any]] = {}
        for full_name in (r.strip() for r in repos.split(",") if r.strip()):
            self.repos[full_name] = {
                "full_name": full_name,
                "default_branch": "main",
                "private": True,
                "description": f"Fake repository {full_name}",
                "branches": {"main": _sha(f"{full_name}:main")},
                "files": {_SEED_FILE_PATH: _SEED_FILE_CONTENT},
                "issues": {
                    1: {
                        "number": 1,
                        "title": f"Seeded issue in {full_name}",
                        "body": "Fix the login flow.",
                        "state": "open",
                        "user": {"login": "seeder"},
                        "labels": [{"name": "bug"}],
                        "comments": [],
                    }
                },
                "pulls": {},
                # Real GitHub shares one number space between issues and PRs;
                # start PRs at 100 so seeded issue numbers never collide.
                "next_pull": 100,
                "workflow_runs": [
                    {
                        "id": 1001,
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "run_number": 1,
                        "html_url": f"https://fake.github/{full_name}/actions/runs/1001",
                    }
                ],
                "dispatches": [],
            }

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        token = header.removeprefix("Bearer ").strip()
        with self.lock:
            return token == self.token or token in self.minted_tokens

    def is_app_jwt(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return bool(_JWT_SHAPE.match(header.removeprefix("Bearer ").strip()))

    def mint_installation_token(self, installation_id: str) -> dict[str, Any]:
        with self.lock:
            self.mint_count += 1
            token = f"ghs_fake_{installation_id}_{self.mint_count}"
            self.minted_tokens.add(token)
        expires = datetime.now(UTC) + timedelta(hours=1)
        return {"token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            copied: dict[str, Any] = json.loads(
                json.dumps({"repos": self.repos, "mint_count": self.mint_count})
            )
            return copied


def handle_request(
    state: FakeGitHubState,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any] | list[Any]]:
    """Pure request router, separated for direct unit testing."""
    auth = headers.get("Authorization")

    # --- App auth endpoints (JWT bearer) ---
    if method == "GET" and path == "/app":
        if not state.is_app_jwt(auth):
            return 401, {"message": "A JSON web token could not be decoded"}
        return 200, {"id": 1, "slug": "jhin-fake-app", "name": "Jhin Fake App"}
    mint = re.fullmatch(r"/app/installations/([^/]+)/access_tokens", path)
    if mint:
        if method != "POST":
            return 405, {"message": "method not allowed"}
        if not state.is_app_jwt(auth):
            return 401, {"message": "A JSON web token could not be decoded"}
        return 201, state.mint_installation_token(mint.group(1))

    # --- State inspection (tests) ---
    if method == "GET" and path == "/_state":
        return 200, state.snapshot()

    # --- Everything else requires a PAT or minted installation token ---
    if not state.authorized(auth):
        return 401, {"message": "Bad credentials"}

    if method == "GET" and path == "/user":
        return 200, {"login": "fake-user", "id": 1}

    match = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
    if not match:
        return 404, {"message": "Not Found"}
    full_name, rest = match.group(1), match.group(2) or ""
    with state.lock:
        repo = state.repos.get(full_name)
    if repo is None:
        return 404, {"message": "Not Found"}

    if rest == "" and method == "GET":
        return 200, {
            "full_name": repo["full_name"],
            "description": repo["description"],
            "default_branch": repo["default_branch"],
            "private": repo["private"],
            "html_url": f"https://fake.github/{full_name}",
            "open_issues_count": len(repo["issues"]),
            "forks_count": 0,
            "stargazers_count": 7,
        }

    if rest == "/branches" and method == "GET":
        with state.lock:
            return 200, [
                {"name": name, "commit": {"sha": sha}, "protected": name == "main"}
                for name, sha in repo["branches"].items()
            ]

    if rest.startswith("/contents/") and method == "GET":
        file_path = rest.removeprefix("/contents/")
        with state.lock:
            content = repo["files"].get(file_path)
        if content is None:
            return 404, {"message": "Not Found"}
        return 200, {
            "path": file_path,
            "content": base64.b64encode(content.encode()).decode(),
            "encoding": "base64",
            "size": len(content),
            "sha": _sha(f"{full_name}:{file_path}"),
        }

    ref_get = re.fullmatch(r"/git/ref/heads/(.+)", rest)
    if ref_get and method == "GET":
        branch = ref_get.group(1)
        with state.lock:
            sha = repo["branches"].get(branch)
        if sha is None:
            return 404, {"message": "Not Found"}
        return 200, {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}

    if rest == "/git/refs" and method == "POST":
        ref = str(body.get("ref", ""))
        sha = str(body.get("sha", ""))
        if not ref.startswith("refs/heads/") or not sha:
            return 422, {"message": "Reference name is not well-formed"}
        branch = ref.removeprefix("refs/heads/")
        with state.lock:
            if branch in repo["branches"]:
                return 422, {"message": "Reference already exists"}
            repo["branches"][branch] = sha
        return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}

    if rest == "/pulls" and method == "POST":
        head, base = str(body.get("head", "")), str(body.get("base", ""))
        with state.lock:
            if head not in repo["branches"] or base not in repo["branches"]:
                return 422, {"message": "Validation Failed: head or base branch missing"}
            number = repo["next_pull"]
            repo["next_pull"] += 1
            pull = {
                "number": number,
                "title": str(body.get("title", "")),
                "body": str(body.get("body", "")),
                "state": "open",
                "merged": False,
                "mergeable": True,
                "draft": bool(body.get("draft", False)),
                "user": {"login": "fake-user"},
                "head": {"ref": head},
                "base": {"ref": base},
                "html_url": f"https://fake.github/{full_name}/pull/{number}",
                "comments": [],
            }
            repo["pulls"][number] = pull
        return 201, pull

    pull_match = re.fullmatch(r"/pulls/(\d+)(/merge)?", rest)
    if pull_match:
        number = int(pull_match.group(1))
        with state.lock:
            pull = repo["pulls"].get(number)
        if pull is None:
            return 404, {"message": "Not Found"}
        if pull_match.group(2) is None and method == "GET":
            return 200, pull
        if pull_match.group(2) and method == "PUT":
            with state.lock:
                if pull["merged"]:
                    return 405, {"message": "Pull Request is not mergeable"}
                pull["merged"] = True
                pull["state"] = "closed"
            return 200, {
                "merged": True,
                "sha": _sha(f"{full_name}:merge:{number}"),
                "message": "Pull Request successfully merged",
            }

    issue_match = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
    if issue_match:
        number = int(issue_match.group(1))
        with state.lock:
            issue = repo["issues"].get(number)
            pull = repo["pulls"].get(number)
        if issue_match.group(2) and method == "POST":
            target = issue if issue is not None else pull
            if target is None:
                return 404, {"message": "Not Found"}
            comment_body = str(body.get("body", ""))
            if not comment_body:
                return 422, {"message": "Validation Failed: body is required"}
            with state.lock:
                comment_id = 1000 + sum(
                    len(item["comments"])
                    for collection in (repo["issues"], repo["pulls"])
                    for item in collection.values()
                )
                comment = {
                    "id": comment_id,
                    "body": comment_body,
                    "user": {"login": "fake-user"},
                    "html_url": f"https://fake.github/{full_name}/issues/{number}"
                    f"#comment-{comment_id}",
                }
                target["comments"].append(comment)
            return 201, comment
        if issue_match.group(2) is None and method == "GET":
            if issue is None:
                return 404, {"message": "Not Found"}
            return 200, {
                **{k: v for k, v in issue.items() if k != "comments"},
                "comments": len(issue["comments"]),
                "html_url": f"https://fake.github/{full_name}/issues/{number}",
            }

    check_match = re.fullmatch(r"/commits/([^/]+)/check-runs", rest)
    if check_match and method == "GET":
        runs = [
            {"name": "ci/tests", "status": "completed", "conclusion": "success"},
            {"name": "ci/lint", "status": "completed", "conclusion": "success"},
        ]
        return 200, {"total_count": len(runs), "check_runs": runs}

    dispatch_match = re.fullmatch(r"/actions/workflows/([^/]+)/dispatches", rest)
    if dispatch_match and method == "POST":
        ref = str(body.get("ref", ""))
        if not ref:
            return 422, {"message": "Validation Failed: ref is required"}
        with state.lock:
            repo["dispatches"].append({"workflow": dispatch_match.group(1), "ref": ref})
            run_id = 1001 + len(repo["workflow_runs"])
            repo["workflow_runs"].append(
                {
                    "id": run_id,
                    "name": dispatch_match.group(1),
                    "status": "queued",
                    "conclusion": None,
                    "head_branch": ref.removeprefix("refs/heads/"),
                    "run_number": len(repo["workflow_runs"]) + 1,
                    "html_url": f"https://fake.github/{full_name}/actions/runs/{run_id}",
                }
            )
        return 204, {}

    if rest == "/actions/runs" and method == "GET":
        with state.lock:
            runs = list(reversed(repo["workflow_runs"]))
        return 200, {"total_count": len(runs), "workflow_runs": runs}

    run_match = re.fullmatch(r"/actions/runs/(\d+)", rest)
    if run_match and method == "GET":
        run_id = int(run_match.group(1))
        with state.lock:
            for run in repo["workflow_runs"]:
                if run["id"] == run_id:
                    return 200, run
        return 404, {"message": "Not Found"}

    return 404, {"message": "Not Found"}


def _make_handler(state: FakeGitHubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            headers = {"Authorization": self.headers.get("Authorization", "")}
            status, payload = handle_request(state, method, self.path.split("?")[0], headers, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data and status != 204:
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep pytest output clean

    return Handler


class FakeGitHubServer:
    """Threaded fake GitHub; use as a context manager in tests."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        token: str = DEFAULT_TOKEN,
        repos: str = DEFAULT_REPOS,
    ) -> None:
        self.state = FakeGitHubState(token=token, repos=repos)
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}"

    def __enter__(self) -> FakeGitHubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    port = int(os.environ.get("FAKE_GITHUB_PORT", "8080"))
    state = FakeGitHubState(
        token=os.environ.get("FAKE_GITHUB_TOKEN", DEFAULT_TOKEN),
        repos=os.environ.get("FAKE_GITHUB_REPOS", DEFAULT_REPOS),
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    print(f"fake GitHub API listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
