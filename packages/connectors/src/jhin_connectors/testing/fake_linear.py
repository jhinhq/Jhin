"""A tiny fake Linear GraphQL API + webhook emitter (plan 32.2).

Stdlib-only (``http.server``) like the fake GitHub server, so it runs as a
pytest fixture, on a dev host, or as a compose service
(``python -m jhin_connectors.testing.fake_linear``). It implements exactly
the GraphQL operations the Linear connector tools use, plus admin endpoints
that *simulate a human working in Linear* — the crucial one fires a
properly-signed webhook (``Linear-Signature`` HMAC-SHA256 of the raw body,
``Linear-Delivery``/``Linear-Event`` headers, fresh ``webhookTimestamp``)
at a configurable target URL. Integration tests drive the Phase 7 trigger
slice through these endpoints.

Auth model: ``Authorization: <FAKE_LINEAR_API_KEY>`` verbatim (Linear's
personal-API-key scheme; default ``fake-linear-api-key``).

Seed: team ENG (Backlog/Todo/In Progress/Done) with fixture issue ENG-142
whose description instructs the code fix matching the fake GitHub seeded
repository (octo/alpha: make ``app.py`` pass ``run_tests.sh``).

Admin endpoints (never part of the real Linear surface):

- ``GET  /_state`` — full state inspection;
- ``POST /_admin/webhook`` ``{"url": ..., "secret": ...}`` — configure the
  webhook target (the URL + signing secret Jhin issued for the connection);
- ``POST /_admin/issues/{identifier}/transition`` ``{"state": "Todo"}`` —
  move the issue and fire an ``Issue update`` webhook whose ``updatedFrom``
  carries the previous ``stateId`` (transition metadata, plan 10.4);
- ``POST /_admin/issues/{identifier}/edit`` ``{"title"/"description": …}``
  — edit fields and fire a webhook whose ``updatedFrom`` has *no* state
  change (the no-transition case);
- ``POST /_admin/redeliver`` ``{"delivery_id": ...}`` — resend a recorded
  delivery byte-for-byte (same delivery id, same signature);
- ``POST /_admin/refire`` ``{"delivery_id": ...}`` — resend the same payload
  as a *new* delivery (fresh delivery id + timestamp, re-signed): a
  semantically identical duplicate event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

DEFAULT_API_KEY = "fake-linear-api-key"
_ORG_ID = "f1a7e000-0000-4000-8000-00000000c0de"
_WEBHOOK_ID = "f1a7e000-0000-4000-8000-0000000000fe"

_SEED_STATES = (
    ("Backlog", "backlog"),
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("Done", "completed"),
)

_ENG_142_DESCRIPTION = """The unit tests in the octo/alpha repository are failing.

Check out octo/alpha, run `bash ./run_tests.sh` to see the failure, fix
`app.py` so the tests pass, push your fix on an agent branch, and open a
pull request into main.
"""


def _uuid() -> str:
    return str(uuid4())


def sign(secret: str, body: bytes) -> str:
    """Bare hex HMAC-SHA256 — the exact Linear-Signature value."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeLinearState:
    """In-memory Linear: teams, workflow states, issues, comments, plus the
    webhook target and a log of fired deliveries."""

    def __init__(self, *, api_key: str = DEFAULT_API_KEY) -> None:
        self.api_key = api_key
        self.lock = threading.Lock()
        self.webhook_url: str = os.environ.get("FAKE_LINEAR_WEBHOOK_URL", "")
        self.webhook_secret: str = os.environ.get("FAKE_LINEAR_WEBHOOK_SECRET", "")
        self.teams: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, dict[str, Any]] = {}  # by identifier
        self.comments: dict[str, list[dict[str, Any]]] = {}  # identifier -> comments
        # delivery_id -> {"headers": ..., "body": bytes as str, "response": int}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.delivery_order: list[str] = []
        self._issue_counter = 900

        team_id = _uuid()
        states = [
            {"id": _uuid(), "name": name, "type": state_type, "position": index}
            for index, (name, state_type) in enumerate(_SEED_STATES)
        ]
        self.teams["ENG"] = {
            "id": team_id,
            "key": "ENG",
            "name": "Engineering",
            "states": states,
        }
        self._add_issue(
            identifier="ENG-142",
            title="Fix the failing unit test in octo/alpha",
            description=_ENG_142_DESCRIPTION,
            team_key="ENG",
            state_name="Backlog",
        )

    # --- state helpers ---

    def _add_issue(
        self, *, identifier: str, title: str, description: str, team_key: str, state_name: str
    ) -> dict[str, Any]:
        team = self.teams[team_key]
        state = self._state_by_name(team, state_name)
        issue = {
            "id": _uuid(),
            "identifier": identifier,
            "title": title,
            "description": description,
            "priority": 0,
            "teamKey": team_key,
            "stateId": state["id"],
            "url": f"https://linear.fake/jhin/issue/{identifier}",
        }
        self.issues[identifier] = issue
        self.comments[identifier] = []
        return issue

    @staticmethod
    def _state_by_name(team: dict[str, Any], name: str) -> dict[str, Any]:
        for state in team["states"]:
            if str(state["name"]).lower() == name.lower():
                return dict(state)
        raise KeyError(name)

    def _state_by_id(self, team: dict[str, Any], state_id: str) -> dict[str, Any]:
        for state in team["states"]:
            if state["id"] == state_id:
                return dict(state)
        raise KeyError(state_id)

    def find_issue(self, ref: str) -> dict[str, Any] | None:
        """Lookup by identifier (ENG-142) or issue UUID."""
        issue = self.issues.get(ref)
        if issue is not None:
            return issue
        for candidate in self.issues.values():
            if candidate["id"] == ref:
                return candidate
        return None

    def issue_graphql(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Issue in the GraphQL response shape the connector queries."""
        team = self.teams[issue["teamKey"]]
        state = self._state_by_id(team, issue["stateId"])
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "description": issue["description"],
            "priority": issue["priority"],
            "url": issue["url"],
            "state": {"id": state["id"], "name": state["name"], "type": state["type"]},
            "team": {"id": team["id"], "key": team["key"], "name": team["name"]},
            "assignee": None,
            "labels": {"nodes": []},
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            copied: dict[str, Any] = json.loads(
                json.dumps(
                    {
                        "teams": self.teams,
                        "issues": self.issues,
                        "comments": self.comments,
                        "webhook": {
                            "url": self.webhook_url,
                            "secret_configured": bool(self.webhook_secret),
                        },
                        "deliveries": [
                            {
                                "delivery_id": delivery_id,
                                "event": self.deliveries[delivery_id]["event"],
                                "action": self.deliveries[delivery_id]["action"],
                                "response": self.deliveries[delivery_id]["response"],
                            }
                            for delivery_id in self.delivery_order
                        ],
                    }
                )
            )
            return copied

    # --- webhook firing ---

    def fire_webhook(
        self,
        *,
        event: str,
        payload: dict[str, Any],
        delivery_id: str | None = None,
        raw_body: bytes | None = None,
    ) -> dict[str, Any]:
        """Sign and POST one delivery at the configured target. Records the
        delivery so it can be redelivered byte-for-byte or refired."""
        with self.lock:
            url, secret = self.webhook_url, self.webhook_secret
        if not url or not secret:
            return {"delivered": False, "reason": "webhook target not configured"}
        delivery = delivery_id or _uuid()
        body = raw_body if raw_body is not None else json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Linear-Event": event,
            "Linear-Delivery": delivery,
            "Linear-Signature": sign(secret, body),
            "User-Agent": "Linear-Webhook",
        }
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            status = 0
            with self.lock:
                self._record_delivery(delivery, event, payload, body, status, error=str(exc))
            return {"delivered": False, "delivery_id": delivery, "reason": str(exc)}
        with self.lock:
            self._record_delivery(delivery, event, payload, body, status)
        return {"delivered": True, "delivery_id": delivery, "response": status}

    def _record_delivery(
        self,
        delivery_id: str,
        event: str,
        payload: dict[str, Any],
        body: bytes,
        response: int,
        error: str = "",
    ) -> None:
        self.deliveries[delivery_id] = {
            "event": event,
            "action": str(payload.get("action", "")),
            "payload": payload,
            "body": body.decode(),
            "response": response,
            "error": error,
        }
        self.delivery_order.append(delivery_id)

    def issue_webhook_payload(
        self, issue: dict[str, Any], *, action: str, updated_from: dict[str, Any] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": action,
            "type": "Issue",
            "organizationId": _ORG_ID,
            "webhookId": _WEBHOOK_ID,
            "webhookTimestamp": int(time.time() * 1000),
            "url": issue["url"],
            "data": self._issue_webhook_data(issue),
        }
        if updated_from is not None:
            payload["updatedFrom"] = updated_from
        return payload

    def _issue_webhook_data(self, issue: dict[str, Any]) -> dict[str, Any]:
        team = self.teams[issue["teamKey"]]
        state = self._state_by_id(team, issue["stateId"])
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "description": issue["description"],
            "priority": issue["priority"],
            "teamId": team["id"],
            "team": {"id": team["id"], "key": team["key"], "name": team["name"]},
            "stateId": state["id"],
            "state": {
                "id": state["id"],
                "name": state["name"],
                "type": state["type"],
                "color": "#888888",
            },
            "labels": [],
            "url": issue["url"],
        }


# --- GraphQL handling (crude keyword dispatch, enough for the connector) ---


def _graphql(state: FakeLinearState, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    query = str(body.get("query", ""))
    variables = body.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}

    if "issueCreate" in query:
        return _issue_create(state, variables)
    if "issueUpdate" in query:
        return _issue_update(state, variables)
    if "commentCreate" in query:
        return _comment_create(state, variables)
    if "viewer" in query:
        return 200, {
            "data": {
                "viewer": {"id": _uuid(), "name": "Fake Linear User", "email": "fake@linear.dev"}
            }
        }
    if "teams" in query:
        with state.lock:
            teams = [
                {
                    "id": team["id"],
                    "key": team["key"],
                    "name": team["name"],
                    "states": {"nodes": [dict(item) for item in team["states"]]},
                }
                for team in state.teams.values()
            ]
        return 200, {"data": {"teams": {"nodes": teams}}}
    if "issues(" in query or "issues (" in query:
        return _issue_search(state, variables)
    if "issue(" in query or "issue (" in query:
        ref = str(variables.get("id", ""))
        with state.lock:
            issue = state.find_issue(ref)
            if issue is None:
                return 200, {"errors": [{"message": f"Entity not found: Issue - {ref}"}]}
            return 200, {"data": {"issue": state.issue_graphql(issue)}}
    return 200, {"errors": [{"message": "unsupported query in fake Linear"}]}


def _issue_search(state: FakeLinearState, variables: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    conditions = variables.get("filter") or {}
    if not isinstance(conditions, dict):
        conditions = {}
    first = int(variables.get("first") or 10)
    title_filter = conditions.get("title") or {}
    text = str(title_filter.get("containsIgnoreCase", "")).lower()
    team_filter = (conditions.get("team") or {}).get("key", {})
    team_key = str(team_filter.get("eq", ""))
    state_filter = (conditions.get("state") or {}).get("name", {})
    state_name = str(state_filter.get("eq", ""))

    with state.lock:
        results = []
        for issue in state.issues.values():
            node = state.issue_graphql(issue)
            if text and text not in str(node["title"]).lower():
                continue
            if team_key and node["team"]["key"] != team_key:
                continue
            if state_name and node["state"]["name"] != state_name:
                continue
            results.append(node)
            if len(results) >= first:
                break
    return 200, {"data": {"issues": {"nodes": results}}}


def _issue_create(state: FakeLinearState, variables: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    input_object = variables.get("input") or {}
    team_id = str(input_object.get("teamId", ""))
    with state.lock:
        team = next((t for t in state.teams.values() if t["id"] == team_id), None)
        if team is None:
            return 200, {"errors": [{"message": f"Entity not found: Team - {team_id}"}]}
        state._issue_counter += 1
        identifier = f"{team['key']}-{state._issue_counter}"
        issue = state._add_issue(
            identifier=identifier,
            title=str(input_object.get("title", "")),
            description=str(input_object.get("description", "")),
            team_key=str(team["key"]),
            state_name=str(team["states"][0]["name"]),
        )
        state_id = str(input_object.get("stateId", ""))
        if state_id:
            issue["stateId"] = state_id
        node = state.issue_graphql(issue)
    payload = state.issue_webhook_payload(issue, action="create", updated_from=None)
    state.fire_webhook(event="Issue", payload=payload)
    return 200, {
        "data": {
            "issueCreate": {
                "success": True,
                "issue": {
                    "id": node["id"],
                    "identifier": node["identifier"],
                    "url": node["url"],
                    "state": {"name": node["state"]["name"]},
                },
            }
        }
    }


def _issue_update(state: FakeLinearState, variables: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    ref = str(variables.get("id", ""))
    input_object = variables.get("input") or {}
    with state.lock:
        issue = state.find_issue(ref)
        if issue is None:
            return 200, {"errors": [{"message": f"Entity not found: Issue - {ref}"}]}
        updated_from: dict[str, Any] = {"updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        if "title" in input_object:
            updated_from["title"] = issue["title"]
            issue["title"] = str(input_object["title"])
        if "description" in input_object:
            updated_from["description"] = issue["description"]
            issue["description"] = str(input_object["description"])
        if "stateId" in input_object and input_object["stateId"] != issue["stateId"]:
            updated_from["stateId"] = issue["stateId"]
            issue["stateId"] = str(input_object["stateId"])
        node = state.issue_graphql(issue)
    payload = state.issue_webhook_payload(issue, action="update", updated_from=updated_from)
    state.fire_webhook(event="Issue", payload=payload)
    return 200, {
        "data": {
            "issueUpdate": {
                "success": True,
                "issue": {
                    "id": node["id"],
                    "identifier": node["identifier"],
                    "title": node["title"],
                    "url": node["url"],
                    "state": node["state"],
                },
            }
        }
    }


def _comment_create(
    state: FakeLinearState, variables: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    input_object = variables.get("input") or {}
    issue_id = str(input_object.get("issueId", ""))
    body_text = str(input_object.get("body", ""))
    with state.lock:
        issue = state.find_issue(issue_id)
        if issue is None:
            return 200, {"errors": [{"message": f"Entity not found: Issue - {issue_id}"}]}
        comment = {
            "id": _uuid(),
            "body": body_text,
            "issueId": issue["id"],
            "url": f"{issue['url']}#comment-{len(state.comments[issue['identifier']]) + 1}",
        }
        state.comments[issue["identifier"]].append(comment)
        identifier = issue["identifier"]
    payload: dict[str, Any] = {
        "action": "create",
        "type": "Comment",
        "organizationId": _ORG_ID,
        "webhookId": _WEBHOOK_ID,
        "webhookTimestamp": int(time.time() * 1000),
        "url": comment["url"],
        "data": {
            "id": comment["id"],
            "body": body_text,
            "issueId": issue["id"],
            "issue": {"id": issue["id"], "identifier": identifier},
            "userId": _uuid(),
        },
    }
    state.fire_webhook(event="Comment", payload=payload)
    return 200, {
        "data": {
            "commentCreate": {
                "success": True,
                "comment": {"id": comment["id"], "url": comment["url"]},
            }
        }
    }


# --- admin endpoints ---


def _admin(
    state: FakeLinearState, method: str, path: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any]] | None:
    if method == "POST" and path == "/_admin/webhook":
        url, secret = str(body.get("url", "")), str(body.get("secret", ""))
        if not url or not secret:
            return 400, {"error": "url and secret are required"}
        with state.lock:
            state.webhook_url = url
            state.webhook_secret = secret
        return 200, {"configured": True, "url": url}

    transition = re.fullmatch(r"/_admin/issues/([^/]+)/transition", path)
    if transition and method == "POST":
        target_state = str(body.get("state", ""))
        with state.lock:
            issue = state.find_issue(transition.group(1))
            if issue is None:
                return 404, {"error": "issue not found"}
            team = state.teams[issue["teamKey"]]
            try:
                new_state = state._state_by_name(team, target_state)
            except KeyError:
                return 400, {"error": f"unknown state: {target_state}"}
            previous_state_id = issue["stateId"]
            if previous_state_id == new_state["id"]:
                return 409, {"error": f"issue is already in {target_state}"}
            issue["stateId"] = new_state["id"]
        updated_from = {
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "stateId": previous_state_id,
        }
        payload = state.issue_webhook_payload(issue, action="update", updated_from=updated_from)
        return 200, state.fire_webhook(event="Issue", payload=payload)

    edit = re.fullmatch(r"/_admin/issues/([^/]+)/edit", path)
    if edit and method == "POST":
        with state.lock:
            issue = state.find_issue(edit.group(1))
            if issue is None:
                return 404, {"error": "issue not found"}
            updated_from = {"updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for field in ("title", "description"):
                if field in body:
                    updated_from[field] = issue[field]
                    issue[field] = str(body[field])
        payload = state.issue_webhook_payload(issue, action="update", updated_from=updated_from)
        return 200, state.fire_webhook(event="Issue", payload=payload)

    if method == "POST" and path == "/_admin/redeliver":
        delivery_id = str(body.get("delivery_id", ""))
        with state.lock:
            recorded = state.deliveries.get(delivery_id)
        if recorded is None:
            return 404, {"error": "delivery not found"}
        # Byte-for-byte redelivery: same delivery id, same body, same
        # signature — exactly what a provider retry looks like.
        return 200, state.fire_webhook(
            event=recorded["event"],
            payload=recorded["payload"],
            delivery_id=delivery_id,
            raw_body=recorded["body"].encode(),
        )

    if method == "POST" and path == "/_admin/refire":
        delivery_id = str(body.get("delivery_id", ""))
        with state.lock:
            recorded = state.deliveries.get(delivery_id)
        if recorded is None:
            return 404, {"error": "delivery not found"}
        payload = json.loads(json.dumps(recorded["payload"]))
        payload["webhookTimestamp"] = int(time.time() * 1000)
        # Same semantic content, brand-new delivery id + signature.
        return 200, state.fire_webhook(event=recorded["event"], payload=payload)

    return None


def handle_request(
    state: FakeLinearState,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Pure request router, separated for direct unit testing."""
    if method == "GET" and path == "/_state":
        return 200, state.snapshot()

    admin = _admin(state, method, path, body)
    if admin is not None:
        return admin

    if method == "POST" and path == "/graphql":
        auth = headers.get("Authorization", "")
        if auth.removeprefix("Bearer ").strip() != state.api_key:
            return 401, {"errors": [{"message": "Authentication required"}]}
        return _graphql(state, body)

    return 404, {"error": "not found"}


def _make_handler(state: FakeLinearState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

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
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep pytest output clean

    return Handler


class FakeLinearServer:
    """Threaded fake Linear; use as a context manager in tests."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, *, api_key: str = DEFAULT_API_KEY
    ) -> None:
        self.state = FakeLinearState(api_key=api_key)
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}"

    def __enter__(self) -> FakeLinearServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    port = int(os.environ.get("FAKE_LINEAR_PORT", "8080"))
    state = FakeLinearState(api_key=os.environ.get("FAKE_LINEAR_API_KEY", DEFAULT_API_KEY))
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    print(f"fake Linear API listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
