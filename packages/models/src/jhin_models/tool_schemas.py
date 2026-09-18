"""Bounded provider compatibility for local JSON Schema references.

Ollama's ToolProperty decoder omits $ref, so nested Pydantic models need
their shape inline. This changes advertisement only, never gateway validation.
"""

from typing import Any
from urllib.parse import unquote

from jhin_models.base import ModelProviderError

_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "dependentSchemas"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset(
    {
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    }
)
_ANNOTATIONS = frozenset({"title", "description", "default", "examples", "$comment"})
_MAX_NODES = 16_384
_MAX_DEPTH = 64
_MAX_TEXT_BYTES = 262_144


def inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local pointers without mutation, remote I/O, or unbounded growth.

    Cycles and incompatible sibling constraints fail before provider dispatch;
    advertising an empty or weaker schema would hide the input contract again.
    """
    remaining_nodes = _MAX_NODES
    remaining_text = _MAX_TEXT_BYTES

    def fail(reason: str) -> ModelProviderError:
        return ModelProviderError(
            f"ollama: tool schema {reason}", error_code="model_incompatible_request"
        )

    def resolve(reference: object) -> dict[str, Any]:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise fail("requires a supported local reference")
        current: Any = schema
        try:
            for part in unquote(reference[2:]).split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                current = current[key]
        except (KeyError, TypeError, IndexError):
            raise fail("contains an unresolved local reference") from None
        if not isinstance(current, dict):
            raise fail("contains a non-object reference target")
        return current

    def visit(value: Any, *, is_schema: bool, active: tuple[str, ...], depth: int) -> Any:
        nonlocal remaining_nodes, remaining_text
        remaining_nodes -= 1
        if remaining_nodes < 0 or depth > _MAX_DEPTH or remaining_text < 0:
            raise fail("exceeds the reference expansion limit")
        if isinstance(value, str):
            remaining_text -= len(value.encode("utf-8"))
            if remaining_text < 0:
                raise fail("exceeds the reference expansion limit")
            return value
        if isinstance(value, list):
            return [
                visit(item, is_schema=is_schema, active=active, depth=depth + 1) for item in value
            ]
        if not isinstance(value, dict):
            return value
        if is_schema and "$ref" in value:
            reference = value["$ref"]
            if isinstance(reference, str) and reference in active:
                raise fail("contains a recursive reference")
            target = resolve(reference)
            siblings = {
                key: item
                for key, item in value.items()
                if key not in {"$ref", "$defs", "definitions"}
            }
            if any(
                key in target and key not in _ANNOTATIONS and target[key] != item
                for key, item in siblings.items()
            ):
                raise fail("has incompatible reference sibling constraints")
            return visit(
                {**target, **siblings}, is_schema=True, active=(*active, reference), depth=depth + 1
            )
        result = {}
        for key, item in value.items():
            if is_schema and key in {"$defs", "definitions"}:
                continue
            remaining_text -= len(str(key).encode("utf-8"))
            if is_schema and key in _SCHEMA_MAPS and isinstance(item, dict):
                children = {}
                for name, child in item.items():
                    remaining_text -= len(str(name).encode("utf-8"))
                    children[name] = visit(child, is_schema=True, active=active, depth=depth + 1)
                result[key] = children
            else:
                result[key] = visit(
                    item,
                    is_schema=is_schema and key in _SCHEMA_LISTS | _SCHEMA_VALUES,
                    active=active,
                    depth=depth + 1,
                )
        return result

    result: dict[str, Any] = visit(schema, is_schema=True, active=(), depth=0)
    return result
