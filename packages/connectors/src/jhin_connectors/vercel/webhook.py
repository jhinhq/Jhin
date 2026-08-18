"""Vercel webhook authentication and deployment-event normalization.

Vercel signs the exact request bytes with a bare lowercase HMAC-SHA1 digest
in ``x-vercel-signature``.  The signature is verified before JSON parsing.

The normalized event is deliberately much smaller than the provider payload:
only documented deployment identity plus explicitly selected Git metadata may
cross into Jhin's canonical event stream.  Provider metadata maps, environment
values, user/team records, and dashboard links are never copied wholesale.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from jhin_connectors.base import NormalizedEvent, RawWebhookEvent, WebhookVerificationError
from jhin_tools.sanitize import strict_json_loads

SIGNATURE_HEADER = "x-vercel-signature"

# Vercel's current account webhooks use ``deployment.succeeded`` while
# integration/check workflows still emit ``deployment.ready``.  Both map to
# one stable Jhin canonical success concept so trigger authors do not have to
# configure two equivalent automations.
WEBHOOK_EVENTS: tuple[str, ...] = (
    "deployment.created",
    "deployment.ready",
    "deployment.succeeded",
    "deployment.error",
    "deployment.canceled",
    "deployment.promoted",
)

CANONICAL_EVENTS: tuple[str, ...] = (
    "connector.vercel.deployment.created",
    "connector.vercel.deployment.ready",
    "connector.vercel.deployment.error",
    "connector.vercel.deployment.canceled",
    "connector.vercel.deployment.promoted",
)

_EVENT_CONTRACT: dict[str, tuple[str, str]] = {
    "deployment.created": ("connector.vercel.deployment.created", "BUILDING"),
    "deployment.ready": ("connector.vercel.deployment.ready", "READY"),
    "deployment.succeeded": ("connector.vercel.deployment.ready", "READY"),
    "deployment.error": ("connector.vercel.deployment.error", "ERROR"),
    "deployment.canceled": ("connector.vercel.deployment.canceled", "CANCELED"),
    "deployment.promoted": ("connector.vercel.deployment.promoted", "READY"),
}

MAX_DELIVERY_ID_CHARS = 200
MAX_EVENT_TYPE_CHARS = 100
MAX_JSON_NESTING_DEPTH = 64
MAX_RESOURCE_ID_CHARS = 200
MAX_PROJECT_NAME_CHARS = 200
MAX_DEPLOYMENT_HOST_CHARS = 253
MAX_GIT_REF_CHARS = 512
MAX_GIT_SHA_CHARS = 128

_LOWER_HEX_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TARGETS = frozenset({"", "preview", "production", "staging"})
_GIT_META_PAIRS: tuple[tuple[str, str], ...] = (
    ("githubCommitRef", "githubCommitSha"),
    ("gitlabCommitRef", "gitlabCommitSha"),
    ("bitbucketCommitRef", "bitbucketCommitSha"),
)


class _MalformedPayload(ValueError):
    """Internal sentinel; provider-controlled details never enter errors."""


def sign_payload(secret: str, body: bytes) -> str:
    """Return Vercel's bare lowercase HMAC-SHA1 signature for exact bytes."""
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    direct = headers.get(name)
    if direct is not None:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate a bare lowercase digest with constant-time comparison."""
    if not secret or not isinstance(signature_header, str):
        return False
    if _LOWER_HEX_SHA1_RE.fullmatch(signature_header) is None:
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature_header)


def parse_webhook(headers: Mapping[str, str], body: bytes, secret: str) -> RawWebhookEvent:
    """Verify exact bytes, then extract bounded root delivery identity."""
    if not verify_signature(secret, body, _header(headers, SIGNATURE_HEADER)):
        raise WebhookVerificationError(f"invalid or missing {SIGNATURE_HEADER} signature")
    try:
        parsed = strict_json_loads(body.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise WebhookVerificationError("payload is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise WebhookVerificationError("payload must be a JSON object")
    if _exceeds_json_nesting_limit(parsed):
        raise WebhookVerificationError("payload JSON nesting is too deep")
    if _contains_non_utf8_text(parsed):
        raise WebhookVerificationError("payload contains invalid Unicode text")

    delivery_id = parsed.get("id")
    event = parsed.get("type")
    if (
        not isinstance(delivery_id, str)
        or not delivery_id
        or len(delivery_id) > MAX_DELIVERY_ID_CHARS
        or _has_ascii_control(delivery_id)
    ):
        raise WebhookVerificationError("payload delivery id is missing or invalid")
    if (
        not isinstance(event, str)
        or not event
        or len(event) > MAX_EVENT_TYPE_CHARS
        or _has_ascii_control(event)
    ):
        raise WebhookVerificationError("payload event type is missing or invalid")
    return RawWebhookEvent(event=event, delivery_id=delivery_id, payload=parsed)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _MalformedPayload
    return value


def _exceeds_json_nesting_limit(value: Any) -> bool:
    """Inspect the entire parsed tree without recursive Python calls.

    Provider-only branches remain part of the raw ingress envelope, so their
    depth must be bounded before Pydantic serializes that envelope.
    """
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > MAX_JSON_NESTING_DEPTH:
            return True
        children = current.values() if isinstance(current, dict) else current
        pending.extend((child, depth + 1) for child in children)
    return False


def _contains_non_utf8_text(value: Any) -> bool:
    """Reject escaped lone surrogates in every provider key and value."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return True
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _has_ascii_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _text(value: Any, *, maximum: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > maximum:
        raise _MalformedPayload
    if _has_ascii_control(value):
        raise _MalformedPayload
    return value


def _deployment_host(value: Any) -> str:
    host = _text(value, maximum=MAX_DEPLOYMENT_HOST_CHARS)
    if len(host) > MAX_DEPLOYMENT_HOST_CHARS:
        raise _MalformedPayload
    labels = host.split(".")
    if len(labels) < 2 or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        raise _MalformedPayload
    return host


def _target(value: Any, *, event: str) -> str:
    if value is None:
        return "production" if event == "deployment.promoted" else ""
    target = _text(value, maximum=len("production"), required=False)
    if target not in _TARGETS:
        raise _MalformedPayload
    return target


def _created_at(root: dict[str, Any], deployment: dict[str, Any]) -> int:
    value = root.get("createdAt", deployment.get("createdAt"))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise _MalformedPayload
    return value


def _git_fields(meta: dict[str, Any]) -> tuple[str, str]:
    for ref_key, sha_key in _GIT_META_PAIRS:
        if ref_key not in meta and sha_key not in meta:
            continue
        git_ref = _text(meta.get(ref_key), maximum=MAX_GIT_REF_CHARS, required=False)
        git_sha = _text(meta.get(sha_key), maximum=MAX_GIT_SHA_CHARS, required=False)
        if git_sha and _HEX_RE.fullmatch(git_sha) is None:
            raise _MalformedPayload
        return git_ref, git_sha
    return "", ""


def normalize(raw: RawWebhookEvent) -> list[NormalizedEvent]:
    """Map one verified deployment event to its fixed allowlisted DTO."""
    event_contract = _EVENT_CONTRACT.get(raw.event)
    if event_contract is None:
        return []
    root = raw.payload
    if root.get("type") != raw.event:
        return []
    try:
        payload = _object(root.get("payload"))
        deployment = _object(payload.get("deployment"))
        project_value = payload.get("project")
        project = project_value if isinstance(project_value, dict) else {}

        deployment_id = _text(deployment.get("id"), maximum=MAX_RESOURCE_ID_CHARS)
        project_id = _text(
            project.get("id", payload.get("projectId")),
            maximum=MAX_RESOURCE_ID_CHARS,
        )
        project_name = _text(
            deployment.get("name", project.get("name")),
            maximum=MAX_PROJECT_NAME_CHARS,
        )
        url = _deployment_host(deployment.get("url"))
        target = _target(payload.get("target", deployment.get("target")), event=raw.event)
        created_at = _created_at(root, deployment)
        meta_value = deployment.get("meta")
        meta = meta_value if isinstance(meta_value, dict) else {}
        git_ref, git_sha = _git_fields(meta)
    except _MalformedPayload:
        return []

    canonical_event, state = event_contract
    return [
        NormalizedEvent(
            event_type=canonical_event,
            data={
                "deployment_id": deployment_id,
                "project_id": project_id,
                "project_name": project_name,
                "url": url,
                "target": target,
                "state": state,
                "created_at": created_at,
                "git_ref": git_ref,
                "git_sha": git_sha,
            },
        )
    ]


__all__ = [
    "CANONICAL_EVENTS",
    "MAX_DELIVERY_ID_CHARS",
    "MAX_EVENT_TYPE_CHARS",
    "MAX_JSON_NESTING_DEPTH",
    "SIGNATURE_HEADER",
    "WEBHOOK_EVENTS",
    "normalize",
    "parse_webhook",
    "sign_payload",
    "verify_signature",
]
