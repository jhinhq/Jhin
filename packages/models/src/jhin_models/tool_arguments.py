"""Normalization of provider tool-call arguments before they enter the run.

OpenAI-compatible providers document ``function.arguments`` as a JSON string,
but the wire is not that tidy in practice: some gateways return the parsed
object, some models double-encode (a JSON string whose content is the JSON
object), and padding whitespace is common. The adapter canonicalizes the
*shape* here so the rest of the platform sees one thing — a string — while
leaving the *content* alone: anything that is not unambiguously a JSON
object is passed through untouched so the run's manifest, not the adapter,
decides what to do with it (it becomes a model-facing ``invalid_input``
observation, never a guessed call).
"""

from __future__ import annotations

import json
from typing import Any

EMPTY_ARGUMENTS = "{}"
_MAX_UNWRAP_DEPTH = 2


def _loads(text: str) -> Any:
    # Plain ``json.loads``: this is a *shape* check. Strictness (duplicate
    # keys, non-finite numbers) is enforced downstream by ``strict_json_loads``.
    return json.loads(text)


def normalize_tool_arguments(raw: object) -> str:
    """Return the provider's tool-call arguments as a JSON text.

    * ``None`` / empty / whitespace → ``"{}"``;
    * ``dict`` (provider already parsed it) → serialized;
    * ``str`` that decodes to a JSON string which itself decodes to an object
      (double-encoded) → the inner text;
    * any other ``str`` → returned as-is (surrounding whitespace stripped);
    * anything else → ``str(raw)`` so the manifest records it as invalid.
    """
    if raw is None:
        return EMPTY_ARGUMENTS
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, (list, tuple)):
        return json.dumps(list(raw), ensure_ascii=False)
    if not isinstance(raw, str):
        return str(raw)
    text = raw.strip()
    if not text:
        return EMPTY_ARGUMENTS
    # Unwrap double-encoding only when every layer is valid JSON and the
    # innermost value is an object; otherwise leave the text untouched.
    candidate = text
    for _ in range(_MAX_UNWRAP_DEPTH):
        try:
            decoded = _loads(candidate)
        except ValueError:
            return text
        if isinstance(decoded, dict):
            return candidate
        if isinstance(decoded, str):
            candidate = decoded.strip()
            continue
        return text
    return text


__all__ = ["EMPTY_ARGUMENTS", "normalize_tool_arguments"]
