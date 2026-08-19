"""Fail-closed projections for durable tool payloads returned by the API."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_SOURCE_BEARING_TOOLS = frozenset({"supabase.function.deploy"})
_DATABASE_TOOLS = frozenset(
    {
        "supabase.database.read",
        "supabase.database.write",
        "supabase.database.destructive",
    }
)
_LOSSLESS_MANIFEST_EVENT = "agent.step.tool_manifest"
_AGENT_ONLY_REASONING_EVENT = "agent.step.reasoning"


def public_tool_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a durable payload while omitting private execution input.

    Gateway storage remains lossless for approval digesting and replay.  This
    projection is applied only while serializing API response models.
    """

    projected = deepcopy(payload)
    if tool_name not in _SOURCE_BEARING_TOOLS | _DATABASE_TOOLS:
        return projected

    candidate: object = projected.get("input", projected)
    if not isinstance(candidate, dict):
        if "input" in projected:
            projected["input"] = {}
        return projected

    if tool_name in _DATABASE_TOOLS:
        candidate.pop("sql", None)
        candidate.pop("params", None)
        return projected

    files = candidate.get("files")
    if not isinstance(files, list):
        candidate.pop("files", None)
        return projected

    safe_files: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str):
            safe_files.append({"path": path})
    candidate["files"] = safe_files
    return projected


def public_run_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project a durable run event for public timeline responses.

    The step manifest intentionally stores canonical tool arguments so a
    workflow replay can bind the exact call set before effects. Public API and
    UI timelines need only the call order and binding status, never those
    lossless arguments.
    """

    if event_type == _AGENT_ONLY_REASONING_EVENT:
        return {}

    if event_type != _LOSSLESS_MANIFEST_EVENT:
        return deepcopy(payload)

    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        return {"manifest": {"count": 0, "calls": []}}
    calls = manifest.get("calls")
    if not isinstance(calls, list):
        return {"manifest": {"count": 0, "calls": []}}

    safe_calls: list[dict[str, Any]] = []
    for candidate in calls:
        if not isinstance(candidate, dict):
            continue
        safe: dict[str, Any] = {}
        ordinal = candidate.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            safe["ordinal"] = ordinal
        lossless = candidate.get("lossless")
        if isinstance(lossless, bool):
            safe["lossless"] = lossless
        tool_name = candidate.get("tool_name")
        if isinstance(tool_name, str) and len(tool_name) <= 200:
            safe["tool_name"] = tool_name
        safe_calls.append(safe)

    projected: dict[str, Any] = {"manifest": {"count": len(safe_calls), "calls": safe_calls}}
    step = payload.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        projected["step"] = step
    return projected
