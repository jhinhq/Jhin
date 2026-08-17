"""GitHub webhook verification and normalization (plan 11.2, 19, 48.5).

Signature scheme: GitHub sends ``X-Hub-Signature-256: sha256=<hexdigest>``
where the digest is HMAC-SHA256 of the raw request body keyed with the
per-connection webhook secret. Verification uses a constant-time compare and
happens before *any* payload processing.

Normalization maps the five supported provider events (plan 11.2) to
canonical ``connector.github.<entity>.<action>`` domain events with compact,
display-safe data. Webhook payloads are untrusted input: unknown events and
malformed shapes normalize to [] — never an exception.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from jhin_connectors.base import NormalizedEvent, RawWebhookEvent

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

WEBHOOK_EVENTS: tuple[str, ...] = ("issues", "pull_request", "check_suite", "workflow_run", "push")

_ACTION_RE = re.compile(r"^[a-z0-9_]+$")
_MAX_TEXT = 300


def sign_payload(secret: str, body: bytes) -> str:
    """The exact header value GitHub would send for this body/secret."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time verification of ``X-Hub-Signature-256`` (plan 48.5)."""
    if not signature_header or not secret:
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature_header.strip())


def _repo_full_name(payload: dict[str, Any]) -> str:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        return str(repository.get("full_name", ""))
    return ""


def _login(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login", ""))
    return ""


def _action_token(payload: dict[str, Any], default: str) -> str | None:
    """The payload's ``action`` as a safe subject token, or None to skip."""
    action = str(payload.get("action", default) or default)
    return action if _ACTION_RE.match(action) else None


def normalize(raw: RawWebhookEvent) -> list[NormalizedEvent]:
    payload = raw.payload
    repository = _repo_full_name(payload)

    if raw.event == "issues":
        action = _action_token(payload, "updated")
        issue = payload.get("issue")
        if action is None or not isinstance(issue, dict):
            return []
        return [
            NormalizedEvent(
                event_type=f"connector.github.issue.{action}",
                data={
                    "repository": repository,
                    "number": int(issue.get("number", 0)),
                    "title": str(issue.get("title", ""))[:_MAX_TEXT],
                    "state": str(issue.get("state", "")),
                    "author": _login(issue.get("user")),
                    "sender": _login(payload.get("sender")),
                },
            )
        ]

    if raw.event == "pull_request":
        action = _action_token(payload, "updated")
        pull = payload.get("pull_request")
        if action is None or not isinstance(pull, dict):
            return []
        raw_head, raw_base = pull.get("head"), pull.get("base")
        head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
        base: dict[str, Any] = raw_base if isinstance(raw_base, dict) else {}
        return [
            NormalizedEvent(
                event_type=f"connector.github.pull_request.{action}",
                data={
                    "repository": repository,
                    "number": int(pull.get("number", 0)),
                    "title": str(pull.get("title", ""))[:_MAX_TEXT],
                    "state": str(pull.get("state", "")),
                    "head": str(head.get("ref", "")),
                    "base": str(base.get("ref", "")),
                    "merged": bool(pull.get("merged", False)),
                    "author": _login(pull.get("user")),
                    "sender": _login(payload.get("sender")),
                },
            )
        ]

    if raw.event == "push":
        commits = payload.get("commits")
        return [
            NormalizedEvent(
                event_type="connector.github.push",
                data={
                    "repository": repository,
                    "ref": str(payload.get("ref", "")),
                    "before": str(payload.get("before", "")),
                    "after": str(payload.get("after", "")),
                    "commit_count": len(commits) if isinstance(commits, list) else 0,
                    "pusher": _login(payload.get("sender"))
                    or str((payload.get("pusher") or {}).get("name", "")),
                },
            )
        ]

    if raw.event == "check_suite":
        action = _action_token(payload, "completed")
        suite = payload.get("check_suite")
        if action is None or not isinstance(suite, dict):
            return []
        return [
            NormalizedEvent(
                event_type=f"connector.github.check_suite.{action}",
                data={
                    "repository": repository,
                    "status": str(suite.get("status", "")),
                    "conclusion": str(suite.get("conclusion") or ""),
                    "head_branch": str(suite.get("head_branch") or ""),
                    "head_sha": str(suite.get("head_sha", "")),
                },
            )
        ]

    if raw.event == "workflow_run":
        action = _action_token(payload, "completed")
        run = payload.get("workflow_run")
        if action is None or not isinstance(run, dict):
            return []
        return [
            NormalizedEvent(
                event_type=f"connector.github.workflow_run.{action}",
                data={
                    "repository": repository,
                    "run_id": int(run.get("id", 0)),
                    "name": str(run.get("name", ""))[:_MAX_TEXT],
                    "status": str(run.get("status", "")),
                    "conclusion": str(run.get("conclusion") or ""),
                    "head_branch": str(run.get("head_branch") or ""),
                    "run_number": int(run.get("run_number", 0)),
                },
            )
        ]

    return []
