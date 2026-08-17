"""Structured agent-to-agent message content (plan 29, 7.6).

Messages between agents are structured records, not prose: the persisted
``content_json`` of every plan-29 message type follows one shape so managers,
workflows, and the UI can consume it without parsing free text.

Shape (all keys always present)::

    {
      "summary": str,                    # one/two sentence human-readable gist
      "artifacts": [                     # concrete outputs referenced
        {"type": str, "id": str, "url_ref": str}
      ],
      "risks": [str],
      "recommended_next_action": str,
      ... message-type specific extras (task_id, status, blocking, ...)
    }

Dependency-light on purpose (stdlib only) so every package can build and
read these without pulling in Pydantic.
"""

from __future__ import annotations

from typing import Any

_MAX_SUMMARY_CHARS = 4_000
_MAX_ARTIFACTS = 20
_MAX_RISKS = 20


def artifact(type_: str, *, id: str = "", url_ref: str = "") -> dict[str, str]:
    """One artifact reference, e.g. ``artifact("github_pull_request", id="381")``."""
    return {"type": str(type_)[:100], "id": str(id)[:300], "url_ref": str(url_ref)[:1000]}


def _clean_artifacts(raw: Any) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return cleaned
    for item in raw[:_MAX_ARTIFACTS]:
        if isinstance(item, dict):
            cleaned.append(
                artifact(
                    str(item.get("type", "") or ""),
                    id=str(item.get("id", "") or ""),
                    url_ref=str(item.get("url_ref", "") or ""),
                )
            )
    return cleaned


def structured_content(
    summary: str,
    *,
    artifacts: Any = None,
    risks: Any = None,
    recommended_next_action: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build the canonical plan-29 content dict, normalizing loose input.

    ``extra`` carries message-type specifics (``task_id``, ``status``,
    ``blocking``…); the canonical keys are named parameters, so extras cannot
    shadow them.
    """
    content: dict[str, Any] = {
        "summary": str(summary)[:_MAX_SUMMARY_CHARS],
        "artifacts": _clean_artifacts(artifacts),
        "risks": [str(risk)[:500] for risk in risks[:_MAX_RISKS]]
        if isinstance(risks, list)
        else [],
        "recommended_next_action": str(recommended_next_action)[:500],
    }
    content.update(extra)
    return content
