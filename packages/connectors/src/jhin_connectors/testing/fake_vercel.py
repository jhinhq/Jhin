"""Deterministic, stdlib-only Vercel API fake for development and tests."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

from jhin_connectors.vercel.webhook import WEBHOOK_EVENTS, sign_payload

DEFAULT_TOKEN = "fake-vercel-token"
_MUTATIONS = frozenset({"preview_create", "redeploy", "promote", "alias"})
_SCENARIOS = frozenset({"deployment_list_mixed_project", "deployment_list_pagination"})
_PAGINATION_SCENARIO_DEPLOYMENTS = 240


@dataclass(frozen=True)
class _FakeResponse:
    status: int
    payload: dict[str, Any] | list[Any] | None
    drop_connection: bool = False


class FakeVercelState:
    """In-memory Vercel resources plus mutation/request inspection state."""

    def __init__(self, *, token: str = DEFAULT_TOKEN) -> None:
        self.token = token
        self.lock = threading.RLock()
        self.projects: dict[str, dict[str, Any]] = {
            "prj_github": {
                "id": "prj_github",
                "name": "github-project",
                "framework": "nextjs",
                "createdAt": 1_700_000_000_000,
                "updatedAt": 1_700_000_010_000,
                "link": {
                    "type": "github",
                    "repoId": 101,
                    "org": "octo",
                    "repo": "alpha",
                },
                "providerSecret": "provider-project-secret",
            },
            "prj_gitlab": {
                "id": "prj_gitlab",
                "name": "gitlab-project",
                "framework": "sveltekit",
                "createdAt": 1_700_000_000_001,
                "updatedAt": 1_700_000_010_001,
                "link": {
                    "type": "gitlab",
                    "projectId": "gl-project-202",
                    "namespace": "octo",
                    "projectName": "beta",
                },
            },
            "prj_bitbucket": {
                "id": "prj_bitbucket",
                "name": "bitbucket-project",
                "framework": "nuxtjs",
                "createdAt": 1_700_000_000_002,
                "updatedAt": 1_700_000_010_002,
                "link": {
                    "type": "bitbucket",
                    "uuid": "{bb-repo-303}",
                    "workspaceUuid": "{bb-workspace}",
                },
            },
            "prj_other": {
                "id": "prj_other",
                "name": "other-project",
                "framework": "nextjs",
                "createdAt": 1_700_000_000_003,
                "updatedAt": 1_700_000_010_003,
                "link": {"type": "github", "repoId": 404},
            },
            "prj_unlinked": {
                "id": "prj_unlinked",
                "name": "unlinked-project",
                "framework": "nextjs",
                "createdAt": 1_700_000_000_004,
                "updatedAt": 1_700_000_010_004,
            },
            "prj_unknown_link": {
                "id": "prj_unknown_link",
                "name": "unknown-link-project",
                "framework": "nextjs",
                "createdAt": 1_700_000_000_005,
                "updatedAt": 1_700_000_010_005,
                "link": {"type": "vercel", "repoId": 101},
            },
            "prj_github_custom": {
                "id": "prj_github_custom",
                "name": "custom-github-project",
                "framework": "nextjs",
                "createdAt": 1_700_000_000_006,
                "updatedAt": 1_700_000_010_006,
                "link": {"type": "github-custom-host", "repoId": 101},
            },
        }
        self.deployments: dict[str, dict[str, Any]] = {
            "dpl_preview": self._deployment(
                "dpl_preview", "prj_github", "github-project", "preview"
            ),
            "dpl_production": self._deployment(
                "dpl_production", "prj_github", "github-project", "production"
            ),
            "dpl_other": self._deployment("dpl_other", "prj_other", "other-project", "preview"),
        }
        self.env_records: dict[str, list[dict[str, Any]]] = {
            "prj_github": [
                {
                    "id": "env_database_url",
                    "key": "DATABASE_URL",
                    "name": "Database URL",
                    "target": ["preview", "production"],
                    "type": "encrypted",
                    "createdAt": 1_700_000_000_000,
                    "updatedAt": 1_700_000_010_000,
                    "gitBranch": "feature/*",
                    "value": "must-never-leak",
                    "encryptedValue": "encrypted-must-never-leak",
                    "internalContent": "internal-must-never-leak",
                    "providerUnknown": "unknown-provider-secret",
                }
            ]
        }
        now_ms = int(time.time() * 1_000)
        self.events: dict[str, list[dict[str, Any]]] = {
            "dpl_preview": [
                {
                    "id": f"evt_{index}",
                    "created": now_ms - index * 1_000,
                    "type": "stdout",
                    "level": "info",
                    "text": ("build output " + str(index) + " ") * 120,
                    "providerSecret": "event-secret",
                }
                for index in range(300)
            ]
        }
        self.counters = dict.fromkeys(_MUTATIONS, 0)
        self.last_requests: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: set[str] = set()
        self.redirects: dict[str, str] = {}
        self.mixed_project_list_row = False
        self.repeat_pagination_cursor = False
        self.ignore_event_limit = False
        self.webhook_counter = 0
        self.scenarios: set[str] = set()
        self._seeded_projects = self._clone(self.projects)
        self._seeded_deployments = self._clone(self.deployments)
        self._seeded_env_records = self._clone(self.env_records)
        self._seeded_events = self._clone(self.events)

    @staticmethod
    def _clone(value: Any) -> Any:
        return json.loads(json.dumps(value))

    @staticmethod
    def _deployment(
        deployment_id: str,
        project_id: str,
        name: str,
        target: str,
    ) -> dict[str, Any]:
        return {
            "uid": deployment_id,
            "id": deployment_id,
            "projectId": project_id,
            "project": {"id": project_id},
            "name": name,
            "url": f"{deployment_id}.fake.vercel.app",
            "state": "READY",
            "readyState": "READY",
            "target": target,
            "created": 1_700_000_000_000,
            "ready": 1_700_000_010_000,
            "inspectorUrl": f"https://vercel.com/inspect/{deployment_id}",
            "providerSecret": "deployment-secret",
        }

    def authorized(self, authorization: str | None) -> bool:
        return authorization == f"Bearer {self.token}"

    def record_request(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "method": method,
            "path": path,
            "query": {key: list(values) for key, values in query.items()},
            "body": json.loads(json.dumps(body)),
        }
        with self.lock:
            self.requests.append(record)
        return record

    def requests_for(self, method: str, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [
                json.loads(json.dumps(request))
                for request in self.requests
                if request["method"] == method and request["path"] == path
            ]

    def seed_many_deployments(self, project_id: str, count: int) -> None:
        with self.lock:
            for index in range(count):
                deployment_id = f"dpl_bulk_{index:04d}"
                self.deployments[deployment_id] = self._deployment(
                    deployment_id,
                    project_id,
                    str(self.projects[project_id]["name"]),
                    "preview",
                )

    def seed_many_projects(self, count: int) -> None:
        with self.lock:
            for index in range(count):
                project_id = f"prj_bulk_{index:04d}"
                self.projects[project_id] = {
                    "id": project_id,
                    "name": f"bulk-{index:04d}",
                    "framework": "nextjs",
                    "createdAt": 1_700_000_000_000 + index,
                    "updatedAt": 1_700_000_010_000 + index,
                    "link": {"type": "github", "repoId": 10_000 + index},
                }

    def arm_scenario(self, scenario: str) -> bool:
        if scenario not in _SCENARIOS:
            return False
        with self.lock:
            if scenario == "deployment_list_mixed_project":
                self.mixed_project_list_row = True
            elif scenario == "deployment_list_pagination":
                self.seed_many_deployments("prj_github", _PAGINATION_SCENARIO_DEPLOYMENTS)
            self.scenarios.add(scenario)
        return True

    def arm_fault(self, mutation: str) -> bool:
        if mutation not in _MUTATIONS:
            return False
        with self.lock:
            self.faults.add(mutation)
        return True

    def record_mutation(self, action: str, request: dict[str, Any]) -> bool:
        with self.lock:
            self.counters[action] += 1
            self.last_requests[action] = json.loads(json.dumps(request))
            should_drop = action in self.faults
            self.faults.discard(action)
            return should_drop

    def reset(self) -> None:
        with self.lock:
            self.projects = self._clone(self._seeded_projects)
            self.deployments = self._clone(self._seeded_deployments)
            self.env_records = self._clone(self._seeded_env_records)
            self.events = self._clone(self._seeded_events)
            self.counters = dict.fromkeys(_MUTATIONS, 0)
            self.last_requests.clear()
            self.requests.clear()
            self.faults.clear()
            self.redirects.clear()
            self.mixed_project_list_row = False
            self.repeat_pagination_cursor = False
            self.ignore_event_limit = False
            self.webhook_counter = 0
            self.scenarios.clear()

    def emit_webhook(
        self,
        *,
        callback_url: str,
        secret: str,
        event: str,
        deployment_id: str,
    ) -> _FakeResponse:
        """Send one deterministic, correctly signed provider-shaped delivery.

        The callback URL and signing secret are deliberately call-local: neither
        is copied into request inspection state or returned to the caller.
        """
        destination = urlsplit(callback_url)
        if destination.scheme not in {"http", "https"} or not destination.netloc:
            return _FakeResponse(400, {"error": "invalid callback URL"})
        if not secret or len(secret) > 4_096:
            return _FakeResponse(400, {"error": "invalid webhook secret"})
        if event not in WEBHOOK_EVENTS:
            return _FakeResponse(400, {"error": "unsupported webhook event"})

        with self.lock:
            deployment_source = self.deployments.get(deployment_id)
            if deployment_source is None:
                return _FakeResponse(404, {"error": "deployment not found"})
            deployment = dict(deployment_source)
            project_id = str(deployment.get("projectId", ""))
            project_source = self.projects.get(project_id, {})
            self.webhook_counter += 1
            delivery_id = f"evt_webhook_{self.webhook_counter}"
            created_at = 1_700_000_100_000 + self.webhook_counter

        provider_payload = {
            "id": delivery_id,
            "type": event,
            "createdAt": created_at,
            "payload": {
                "deployment": {
                    "id": str(deployment.get("id", "")),
                    "name": str(deployment.get("name", "")),
                    "url": str(deployment.get("url", "")),
                    "target": str(deployment.get("target", "")),
                    "meta": {
                        "githubCommitRef": "main",
                        "githubCommitSha": "a" * 40,
                    },
                },
                "project": {
                    "id": project_id,
                    "name": str(project_source.get("name", "")),
                },
                "target": str(deployment.get("target", "")),
            },
        }
        raw_body = json.dumps(provider_payload, separators=(",", ":")).encode("utf-8")
        outbound = Request(
            callback_url,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "x-vercel-signature": sign_payload(secret, raw_body),
            },
            method="POST",
        )
        try:
            with urlopen(outbound, timeout=10) as response:
                response.read()
                status = response.status
        except (OSError, ValueError):
            return _FakeResponse(
                502,
                {"delivered": False, "delivery_id": delivery_id, "response": 0},
            )
        return _FakeResponse(
            200,
            {"delivered": True, "delivery_id": delivery_id, "response": status},
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return cast(
                "dict[str, Any]",
                json.loads(
                    json.dumps(
                        {
                            "counters": self.counters,
                            "last_requests": self.last_requests,
                            "requests": self.requests,
                            "scenarios": sorted(self.scenarios),
                        }
                    )
                ),
            )


def _integer_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(query.get(name, [str(default)])[0])
    except (ValueError, IndexError):
        return default


def _page(
    rows: list[dict[str, Any]],
    query: dict[str, list[str]],
    *,
    repeat_cursor: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = max(1, min(100, _integer_query(query, "limit", 100)))
    offset = max(0, _integer_query(query, "until", 0))
    selected = rows[offset : offset + limit]
    pagination: dict[str, Any] = {}
    if offset + limit < len(rows):
        next_cursor = offset if repeat_cursor and offset else offset + limit
        pagination = {"next": str(next_cursor)}
    return selected, pagination


def handle_request(
    state: FakeVercelState,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: dict[str, Any],
) -> _FakeResponse:
    """Pure-ish router shared by the threaded server and direct tests."""
    if method == "GET" and path == "/_state":
        return _FakeResponse(200, state.snapshot())
    if method == "POST" and path == "/_reset":
        state.reset()
        return _FakeResponse(200, {"ok": True})
    if method == "POST" and path == "/_scenario":
        scenario = body.get("scenario")
        if (
            set(body) != {"scenario"}
            or not isinstance(scenario, str)
            or not state.arm_scenario(scenario)
        ):
            return _FakeResponse(400, {"error": "unknown scenario"})
        return _FakeResponse(200, {"armed": scenario})
    if method == "POST" and path == "/_fault":
        mutation = body.get("mutation")
        if not isinstance(mutation, str) or not state.arm_fault(mutation):
            return _FakeResponse(400, {"error": "unknown mutation"})
        return _FakeResponse(200, {"armed": mutation})
    if method == "POST" and path == "/_admin/webhook":
        callback_url = body.get("url")
        secret = body.get("secret")
        event = body.get("event")
        deployment_id = body.get("deployment_id")
        if (
            not isinstance(callback_url, str)
            or not isinstance(secret, str)
            or not isinstance(event, str)
            or not isinstance(deployment_id, str)
        ):
            return _FakeResponse(400, {"error": "invalid webhook request"})
        return state.emit_webhook(
            callback_url=callback_url,
            secret=secret,
            event=event,
            deployment_id=deployment_id,
        )

    request = state.record_request(method, path, query, body)
    redirect = state.redirects.get(path)
    if redirect is not None:
        return _FakeResponse(302, {"location": redirect})
    if not state.authorized(headers.get("Authorization")):
        return _FakeResponse(401, {"error": {"code": "unauthorized"}})

    if method == "GET" and path == "/v2/user":
        return _FakeResponse(
            200,
            {
                "user": {
                    "id": "usr_fake",
                    "username": "fake-user",
                    "email": "fake@example.test",
                    "providerSecret": "user-secret",
                }
            },
        )

    if method == "GET" and path == "/v9/projects":
        with state.lock:
            rows = [dict(project) for _, project in sorted(state.projects.items())]
        page, pagination = _page(rows, query, repeat_cursor=state.repeat_pagination_cursor)
        return _FakeResponse(200, {"projects": page, "pagination": pagination})

    project_match = re.fullmatch(r"/v9/projects/([^/]+)", path)
    if method == "GET" and project_match:
        project_id = unquote(project_match.group(1))
        with state.lock:
            project = state.projects.get(project_id)
            payload = dict(project) if project is not None else None
        return _FakeResponse(200, payload) if payload else _FakeResponse(404, {"error": {}})

    env_match = re.fullmatch(r"/v9/projects/([^/]+)/env", path)
    if method == "GET" and env_match:
        project_id = unquote(env_match.group(1))
        with state.lock:
            if project_id not in state.projects:
                return _FakeResponse(404, {"error": {}})
            envs = json.loads(json.dumps(state.env_records.get(project_id, [])))
        return _FakeResponse(200, {"envs": envs})

    if method == "GET" and path == "/v6/deployments":
        project_id = query.get("projectId", [""])[0]
        with state.lock:
            rows = [
                dict(deployment)
                for _, deployment in sorted(state.deployments.items())
                if deployment.get("projectId") == project_id
            ]
            if state.mixed_project_list_row and state.deployments.get("dpl_other") is not None:
                rows.insert(0, dict(state.deployments["dpl_other"]))
        page, pagination = _page(rows, query, repeat_cursor=state.repeat_pagination_cursor)
        return _FakeResponse(200, {"deployments": page, "pagination": pagination})

    deployment_match = re.fullmatch(r"/v13/deployments/([^/]+)", path)
    if method == "GET" and deployment_match:
        deployment_id = unquote(deployment_match.group(1))
        with state.lock:
            deployment = state.deployments.get(deployment_id)
            payload = dict(deployment) if deployment is not None else None
        return _FakeResponse(200, payload) if payload else _FakeResponse(404, {"error": {}})

    event_match = re.fullmatch(r"/v3/deployments/([^/]+)/events", path)
    if method == "GET" and event_match:
        deployment_id = unquote(event_match.group(1))
        since = _integer_query(query, "since", 0)
        until = _integer_query(query, "until", 2**63 - 1)
        limit = max(1, min(200, _integer_query(query, "limit", 100)))
        returned_limit = limit + 25 if state.ignore_event_limit else limit
        with state.lock:
            events = [
                dict(event)
                for event in state.events.get(deployment_id, [])
                if since <= int(event.get("created", 0)) <= until
            ][:returned_limit]
        return _FakeResponse(200, events)

    if method == "POST" and path == "/v13/deployments":
        action = "redeploy" if query.get("forceNew") == ["1"] else "preview_create"
        with state.lock:
            deployment_id = f"dpl_created_{state.counters[action] + 1}"
            project_id = str(body.get("project", ""))
            if action == "redeploy":
                source_id = str(body.get("deploymentId", ""))
                source_deployment = state.deployments.get(source_id, {})
                project_id = str(source_deployment.get("projectId", ""))
            deployment = state._deployment(
                deployment_id,
                project_id,
                str(body.get("name", "deployment")),
                str(body.get("target", "preview")),
            )
            state.deployments[deployment_id] = deployment
        drop = state.record_mutation(action, request)
        return _FakeResponse(200, deployment, drop_connection=drop)

    promote_match = re.fullmatch(r"/v10/projects/([^/]+)/promote/([^/]+)", path)
    if method == "POST" and promote_match:
        deployment_id = unquote(promote_match.group(2))
        with state.lock:
            promote_source = state.deployments.get(deployment_id)
            if promote_source is None:
                return _FakeResponse(404, {"error": {}})
            promoted = dict(promote_source)
            promoted["target"] = "production"
            state.deployments[deployment_id] = promoted
        drop = state.record_mutation("promote", request)
        return _FakeResponse(200, promoted, drop_connection=drop)

    alias_match = re.fullmatch(r"/v2/deployments/([^/]+)/aliases", path)
    if method == "POST" and alias_match:
        deployment_id = unquote(alias_match.group(1))
        with state.lock:
            if deployment_id not in state.deployments:
                return _FakeResponse(404, {"error": {}})
        drop = state.record_mutation("alias", request)
        return _FakeResponse(
            200,
            {"uid": "alias_1", "alias": str(body.get("alias", ""))},
            drop_connection=drop,
        )

    return _FakeResponse(404, {"error": {"code": "not_found"}})


def _make_handler(state: FakeVercelState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                decoded = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                decoded = {}
            body = decoded if isinstance(decoded, dict) else {}
            split = urlsplit(self.path)
            query = parse_qs(split.query, keep_blank_values=True)
            result = handle_request(
                state,
                method,
                split.path,
                query,
                {"Authorization": self.headers.get("Authorization", "")},
                body,
            )
            if result.drop_connection:
                self.close_connection = True
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if result.status == 302:
                destination = state.redirects.get(split.path, "https://invalid.example")
                data = b""
                self.send_response(302)
                self.send_header("Location", destination)
            else:
                data = b"" if result.payload is None else json.dumps(result.payload).encode()
                self.send_response(result.status)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(data)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


class FakeVercelServer:
    """Threaded fake Vercel server usable as a pytest context manager."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        token: str = DEFAULT_TOKEN,
    ) -> None:
        self.state = FakeVercelState(token=token)
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}"

    def __enter__(self) -> FakeVercelServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    port = int(os.environ.get("FAKE_VERCEL_PORT", "8080"))
    state = FakeVercelState(token=os.environ.get("FAKE_VERCEL_TOKEN", DEFAULT_TOKEN))
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    print(f"fake Vercel API listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
