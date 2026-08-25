"""The committed OpenAPI snapshot and the compatibility diff that guards it.

``docs/api/openapi.v1.json`` is the published shape of ``/api/v1`` as of the
last commit. Every test run regenerates the document from the running app and
compares it against that file. The comparison is **not** equality: adding an
endpoint, an optional request field, a response field, or an enum value in a
response is exactly what a stable API is allowed to do, and firing on those
would train everyone to update the snapshot without reading the diff. What
fails the build is a change an existing integration could notice — the list in
``docs/architecture/api-versioning.md``, implemented here.

Usage::

    uv run python scripts/openapi_snapshot.py            # check (what CI runs)
    uv run python scripts/openapi_snapshot.py --update   # accept the current surface
    uv run python scripts/openapi_snapshot.py --print    # write the document to stdout

The document is generated from fixed settings, never from the ambient
environment, so the snapshot is a property of the code and not of whoever ran
it. ``servers`` is the relative ``/`` for the same reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "docs" / "api" / "openapi.v1.json"

UPDATE_HINT = (
    "If every change above is intentional and backwards compatible, refresh the "
    "snapshot with:\n\n    uv run python scripts/openapi_snapshot.py --update\n\n"
    "If any of them is breaking, do not update the snapshot: keep the old shape "
    "working and add the new one beside it, or take it through the deprecation "
    "process in docs/architecture/api-versioning.md."
)

BREAKING_HINT = (
    "These changes would break an existing integration against /api/v1. The "
    "contract is additive: see docs/architecture/api-versioning.md for what is "
    "allowed and how to introduce a change that is not.\n\n"
    "If this is a deliberate, announced break — a removal whose deprecation "
    "window has expired, or the introduction of /api/v2 — update the snapshot "
    "and say so in CHANGELOG.md."
)

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# A schema that refers to itself (an org tree, a task tree) would recurse
# forever; the branch stops at the repeat and records that it did.
_RECURSION_MARKER = ("<recursive>",)


# --------------------------------------------------------------------------
# Generating the document
# --------------------------------------------------------------------------


def generate() -> dict[str, Any]:
    """The OpenAPI document of the app as this working tree defines it."""
    from jhin_api.main import create_app
    from jhin_api.settings import Settings

    settings = Settings(
        app_env="test",
        app_name="Jhin",
        app_url="http://localhost:3000",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    document: dict[str, Any] = create_app(settings).openapi()
    return document


def render(document: dict[str, Any]) -> str:
    """Stable text for the snapshot: sorted keys, so a reordered router or a
    renamed module produces no diff at all."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


# --------------------------------------------------------------------------
# Flattening a schema into comparable fields
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One leaf of a request or response body, resolved through ``$ref``."""

    types: tuple[str, ...]
    required: bool
    enum: tuple[str, ...] | None


def _deref(schema: dict[str, Any], components: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema, None
    name = ref.rsplit("/", 1)[-1]
    resolved = components.get(name)
    return (resolved if isinstance(resolved, dict) else {}), ref


def _type_names(schema: dict[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list):
        return tuple(sorted(str(item) for item in declared))
    for combinator in ("anyOf", "oneOf"):
        members = schema.get(combinator)
        if isinstance(members, list):
            names: set[str] = set()
            for member in members:
                if isinstance(member, dict):
                    names.update(_type_names(member) or ("unknown",))
            if names:
                return tuple(sorted(names))
    if "properties" in schema:
        return ("object",)
    if "$ref" in schema:
        return ("object",)
    return ("any",)


def _enum_values(schema: dict[str, Any], components: dict[str, Any]) -> tuple[str, ...] | None:
    """Enum values, looking through ``anyOf`` so ``Literal[...] | None`` counts."""
    direct = schema.get("enum")
    if isinstance(direct, list):
        return tuple(sorted(json.dumps(value, sort_keys=True) for value in direct))
    const = schema.get("const")
    if const is not None:
        return (json.dumps(const, sort_keys=True),)
    collected: set[str] = set()
    found = False
    for combinator in ("anyOf", "oneOf"):
        members = schema.get(combinator)
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            resolved, _ = _deref(member, components)
            values = _enum_values(resolved, components)
            if values is not None:
                found = True
                collected.update(values)
    return tuple(sorted(collected)) if found else None


def flatten(
    schema: dict[str, Any] | None,
    components: dict[str, Any],
    *,
    prefix: str = "",
    required: bool = True,
    seen: frozenset[str] = frozenset(),
) -> dict[str, Field]:
    """Every leaf of ``schema`` as ``dotted.path -> Field``.

    Object properties become ``parent.child``, array items become ``parent[]``,
    free-form maps become ``parent{}``. Composed schemas (``allOf``, ``anyOf``)
    are merged, which is how Pydantic writes an optional model field.
    """
    if not isinstance(schema, dict):
        return {}

    resolved, ref = _deref(schema, components)
    if ref is not None:
        if ref in seen:
            return {prefix: Field(_RECURSION_MARKER, required, None)}
        seen = seen | {ref}
        schema = resolved

    fields: dict[str, Field] = {
        prefix: Field(_type_names(schema), required, _enum_values(schema, components))
    }

    properties = schema.get("properties")
    if isinstance(properties, dict):
        required_names = schema.get("required")
        required_set = set(required_names) if isinstance(required_names, list) else set()
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            fields.update(
                flatten(
                    child,
                    components,
                    prefix=f"{prefix}.{name}" if prefix else str(name),
                    required=name in required_set,
                    seen=seen,
                )
            )

    items = schema.get("items")
    if isinstance(items, dict):
        fields.update(flatten(items, components, prefix=f"{prefix}[]", required=True, seen=seen))

    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        fields.update(flatten(extra, components, prefix=f"{prefix}{{}}", required=False, seen=seen))

    for combinator in ("allOf", "anyOf", "oneOf"):
        members = schema.get(combinator)
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            # A union member contributes its leaves; whether a leaf is required
            # is only meaningful for allOf, so unions relax it.
            merged = flatten(
                member,
                components,
                prefix=prefix,
                required=required and combinator == "allOf",
                seen=seen,
            )
            for key, value in merged.items():
                if key == prefix:
                    continue
                existing = fields.get(key)
                fields[key] = (
                    value
                    if existing is None
                    else Field(
                        tuple(sorted(set(existing.types) | set(value.types))),
                        existing.required and value.required,
                        _merge_enums(existing.enum, value.enum),
                    )
                )

    return fields


def _merge_enums(
    left: tuple[str, ...] | None, right: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    if left is None:
        return right
    if right is None:
        return left
    return tuple(sorted(set(left) | set(right)))


# --------------------------------------------------------------------------
# The operation surface
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    parameters: dict[str, Field]
    request: dict[str, Field]
    responses: dict[str, dict[str, Field]]
    security: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...]
    operation_id: str | None
    deprecated: bool


def _json_schema(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, dict):
        return None
    for media_type, media in content.items():
        if not isinstance(media, dict):
            continue
        if media_type == "application/json" or str(media_type).endswith("+json"):
            schema = media.get("schema")
            return schema if isinstance(schema, dict) else None
    return None


def _security(raw: Any) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], ...]:
    if not isinstance(raw, list):
        return ()
    alternatives: list[tuple[tuple[str, tuple[str, ...]], ...]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        alternatives.append(
            tuple(
                sorted(
                    (
                        name,
                        tuple(sorted(str(s) for s in scopes)) if isinstance(scopes, list) else (),
                    )
                    for name, scopes in entry.items()
                )
            )
        )
    return tuple(sorted(alternatives))


def describe_operation(operation: dict[str, Any], components: dict[str, Any]) -> Operation:
    parameters: dict[str, Field] = {}
    raw_parameters = operation.get("parameters")
    if isinstance(raw_parameters, list):
        for parameter in raw_parameters:
            if not isinstance(parameter, dict):
                continue
            name = f"{parameter.get('in')}:{parameter.get('name')}"
            parameters.update(
                flatten(
                    parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {},
                    components,
                    prefix=name,
                    required=bool(parameter.get("required")),
                )
            )

    body_schema = _json_schema(operation.get("requestBody"))
    request = flatten(body_schema, components) if body_schema is not None else {}

    responses: dict[str, dict[str, Field]] = {}
    raw_responses = operation.get("responses")
    if isinstance(raw_responses, dict):
        for status, response in raw_responses.items():
            schema = _json_schema(response)
            responses[str(status)] = flatten(schema, components) if schema is not None else {}

    return Operation(
        parameters=parameters,
        request=request,
        responses=responses,
        security=_security(operation.get("security")),
        operation_id=(
            operation["operationId"] if isinstance(operation.get("operationId"), str) else None
        ),
        deprecated=bool(operation.get("deprecated")),
    )


def surface(document: dict[str, Any]) -> dict[tuple[str, str], Operation]:
    """``(method, path) -> Operation`` for every published operation."""
    components = document.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    if not isinstance(schemas, dict):
        schemas = {}
    paths = document.get("paths", {})
    result: dict[tuple[str, str], Operation] = {}
    if not isinstance(paths, dict):
        return result
    for path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            result[(method.lower(), str(path))] = describe_operation(operation, schemas)
    return result


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    breaking: bool
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.message}"


def _type_change(old: Field, new: Field) -> bool:
    if old.types == _RECURSION_MARKER or new.types == _RECURSION_MARKER:
        return False
    # Widening (adding null, adding a union member) keeps every old value valid.
    return not set(old.types).issubset(set(new.types))


def _compare_fields(
    old: dict[str, Field],
    new: dict[str, Field],
    *,
    where: str,
    kind: str,
    direction: str,
) -> list[Change]:
    """Compare one body or parameter set.

    ``direction`` is ``"request"`` (the integrator sends it) or ``"response"``
    (the integrator reads it). It decides which way an enum may move and what a
    field losing ``required`` means.
    """
    changes: list[Change] = []
    for name, field in sorted(old.items()):
        if not name:
            continue
        current = new.get(name)
        if current is None:
            changes.append(Change(True, where, f"{kind} {name!r} was removed"))
            continue
        if _type_change(field, current):
            changes.append(
                Change(
                    True,
                    where,
                    f"{kind} {name!r} changed type from "
                    f"{'|'.join(field.types)} to {'|'.join(current.types)}",
                )
            )
        if not field.required and current.required:
            changes.append(
                Change(
                    True,
                    where,
                    f"{kind} {name!r} became required; callers that omit it now fail",
                )
            )
        if direction == "response" and field.required and not current.required:
            changes.append(
                Change(
                    True,
                    where,
                    f"{kind} {name!r} is no longer always present; "
                    "callers that read it unconditionally now break",
                )
            )
        if field.enum is not None and current.enum is not None:
            removed = sorted(set(field.enum) - set(current.enum))
            if removed:
                changes.append(
                    Change(
                        True,
                        where,
                        f"{kind} {name!r} no longer accepts {', '.join(removed)}",
                    )
                )
            added = sorted(set(current.enum) - set(field.enum))
            if added and direction == "request":
                changes.append(
                    Change(False, where, f"{kind} {name!r} also accepts {', '.join(added)}")
                )
            elif added:
                changes.append(
                    Change(False, where, f"{kind} {name!r} may also return {', '.join(added)}")
                )
        elif field.enum is not None and current.enum is None:
            changes.append(Change(False, where, f"{kind} {name!r} is no longer a closed set"))
        elif field.enum is None and current.enum is not None:
            changes.append(
                Change(
                    True,
                    where,
                    f"{kind} {name!r} became a closed set "
                    f"({', '.join(current.enum)}); previously valid values may now be refused",
                )
            )

    for name, field in sorted(new.items()):
        if not name or name in old:
            continue
        if field.required and direction == "request":
            changes.append(Change(True, where, f"new required {kind} {name!r}"))
        else:
            changes.append(Change(False, where, f"new {kind} {name!r}"))
    return changes


def _compare_security(old: Operation, new: Operation, where: str) -> list[Change]:
    if old.security == new.security:
        return []
    if old.security == () and new.security != ():
        return [
            Change(True, where, "now requires authentication where it previously required none")
        ]
    old_set = set(old.security)
    new_set = set(new.security)
    changes: list[Change] = []
    for removed in sorted(old_set - new_set):
        rendered = " + ".join(
            name if not scopes else f"{name}({', '.join(scopes)})" for name, scopes in removed
        )
        changes.append(
            Change(True, where, f"no longer accepts {rendered or 'an anonymous caller'}")
        )
    for added in sorted(new_set - old_set):
        rendered = " + ".join(
            name if not scopes else f"{name}({', '.join(scopes)})" for name, scopes in added
        )
        changes.append(Change(False, where, f"also accepts {rendered}"))
    return changes


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Every difference between two documents, each marked breaking or not."""
    before = surface(old)
    after = surface(new)
    changes: list[Change] = []

    for key in sorted(before.keys() - after.keys()):
        method, path = key
        where = f"{method.upper()} {path}"
        changes.append(Change(True, where, "was removed"))

    for key in sorted(after.keys() - before.keys()):
        method, path = key
        changes.append(Change(False, f"{method.upper()} {path}", "is new"))

    for key in sorted(before.keys() & after.keys()):
        method, path = key
        where = f"{method.upper()} {path}"
        old_op, new_op = before[key], after[key]

        if old_op.operation_id != new_op.operation_id:
            changes.append(
                Change(
                    True,
                    where,
                    f"operationId changed from {old_op.operation_id!r} to "
                    f"{new_op.operation_id!r}; generated SDKs name their method after it",
                )
            )
        if old_op.deprecated and not new_op.deprecated:
            changes.append(Change(False, where, "is no longer marked deprecated"))
        elif new_op.deprecated and not old_op.deprecated:
            changes.append(Change(False, where, "is now marked deprecated"))

        changes.extend(
            _compare_fields(
                old_op.parameters,
                new_op.parameters,
                where=where,
                kind="parameter",
                direction="request",
            )
        )
        changes.extend(
            _compare_fields(
                old_op.request,
                new_op.request,
                where=where,
                kind="request field",
                direction="request",
            )
        )

        for status in sorted(old_op.responses.keys() - new_op.responses.keys()):
            changes.append(Change(True, where, f"no longer documents a {status} response"))
        for status in sorted(new_op.responses.keys() - old_op.responses.keys()):
            changes.append(Change(False, where, f"documents a new {status} response"))
        for status in sorted(old_op.responses.keys() & new_op.responses.keys()):
            changes.extend(
                _compare_fields(
                    old_op.responses[status],
                    new_op.responses[status],
                    where=f"{where} ({status})",
                    kind="response field",
                    direction="response",
                )
            )

        changes.extend(_compare_security(old_op, new_op, where))

    return changes


def breaking(changes: list[Change]) -> list[Change]:
    return [change for change in changes if change.breaking]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def check(document: dict[str, Any] | None = None) -> tuple[list[Change], bool]:
    """Compare the live document with the snapshot.

    Returns the changes and whether the snapshot text is byte-identical.
    """
    live = document if document is not None else generate()
    snapshot = load_snapshot()
    return compare(snapshot, live), render(snapshot) == render(live)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite the snapshot")
    parser.add_argument("--print", action="store_true", help="write the document to stdout")
    args = parser.parse_args(argv)

    live = generate()
    if args.print:
        sys.stdout.write(render(live))
        return 0
    if args.update:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(render(live), encoding="utf-8")
        print(f"wrote {SNAPSHOT_PATH.relative_to(ROOT)}")
        return 0

    changes, identical = check(live)
    hard = breaking(changes)
    if hard:
        print("Breaking changes against docs/api/openapi.v1.json:\n")
        for change in hard:
            print(f"  - {change.render()}")
        print(f"\n{BREAKING_HINT}")
        return 1
    if not identical:
        print("The API surface changed compatibly; the snapshot is stale:\n")
        for change in changes:
            print(f"  - {change.render()}")
        if not changes:
            print("  - (documentation or metadata only)")
        print(f"\n{UPDATE_HINT}")
        return 1
    print("openapi snapshot is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
