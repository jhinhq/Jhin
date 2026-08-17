"""The trigger filter DSL: validation and pure evaluation (plan 10.2, 10.4).

Shape
-----
A filter is a *group* — ``{"all": [...]}`` (every child must pass) or
``{"any": [...]}`` (at least one child must pass) — whose children are
either nested groups or *conditions*::

    {"path": "data.team.key", "op": "eq", "value": "ENG"}

``{}`` (or a group with no children under ``all``) matches every event.
Paths are dotted lookups into the normalized event mapping; integer
segments index into lists. There is deliberately no way to express code,
regexes, or cross-event state (plan 52).

Transition matching (plan 10.4)
-------------------------------
Connectors normalize "what changed" into a ``changed_from`` object that
mirrors the shape of ``data`` but holds *previous* values, populated only
for fields that actually changed. The first-class ``transitioned_to`` op
builds on that convention. ``{"path": "data.state.name", "op":
"transitioned_to", "value": "Todo"}`` passes when:

1. the current value at ``data.state.name`` equals ``"Todo"``, and
2. the mirrored branch ``data.changed_from.state`` exists — i.e. the state
   field actually changed in this event (created events and unrelated edits
   have no such branch), and
3. the exact previous value at ``data.changed_from.state.name``, *when
   present*, does not equal ``"Todo"``.

Rule 3 is tolerant of connectors that only know the previous foreign key
(Linear's ``updatedFrom.stateId`` gives a previous state id, not a name):
the branch existing proves a change happened, and rule 1 anchors where it
landed. The equivalent long form — current ``eq`` + previous ``neq`` —
remains expressible with plain conditions for connectors with full mirrors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

OPS: frozenset[str] = frozenset(
    {
        "eq",
        "neq",
        "in",
        "not_in",
        "contains",
        "exists",
        "gt",
        "gte",
        "lt",
        "lte",
        "transitioned_to",
    }
)

_LIST_OPS = frozenset({"in", "not_in"})
_ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})

MAX_DEPTH = 8
MAX_CONDITIONS = 50
_CHANGED_FROM = "changed_from"


class FilterError(ValueError):
    """The filter document is structurally invalid (rejected at write time)."""


class _MissingType:
    """Sentinel distinguishing 'path not present' from a literal None."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING = _MissingType()


@dataclass(frozen=True)
class ConditionResult:
    """One condition's outcome plus the evidence, for UI explanations."""

    path: str
    op: str
    value: Any
    passed: bool
    actual: Any = None
    actual_present: bool = False
    previous: Any = None
    previous_present: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "op": self.op,
            "value": self.value,
            "passed": self.passed,
            "actual": self.actual if self.actual_present else None,
            "actual_present": self.actual_present,
            "previous": self.previous if self.previous_present else None,
            "previous_present": self.previous_present,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EvaluationResult:
    matched: bool
    conditions: list[ConditionResult] = field(default_factory=list)


def validate_filter(document: Any) -> None:
    """Reject malformed filters with a human-readable reason (plan 10.3).

    Called at trigger create/update time so bad filters never reach the
    matcher; evaluation itself also degrades safely (a broken condition
    simply fails).
    """
    count = _validate_group(document, depth=0)
    if count > MAX_CONDITIONS:
        raise FilterError(f"filter has {count} conditions; the maximum is {MAX_CONDITIONS}")


def _validate_group(node: Any, *, depth: int) -> int:
    if depth > MAX_DEPTH:
        raise FilterError(f"filter nesting exceeds the maximum depth of {MAX_DEPTH}")
    if not isinstance(node, Mapping):
        raise FilterError("each filter node must be a JSON object")
    if not node:
        return 0
    if "path" in node or "op" in node:
        _validate_condition(node)
        return 1
    keys = set(node.keys())
    if keys not in ({"all"}, {"any"}):
        raise FilterError(
            f"a filter group must have exactly one of 'all' or 'any' (got keys: {sorted(keys)})"
        )
    kind = "all" if "all" in node else "any"
    children = node[kind]
    if not isinstance(children, Sequence) or isinstance(children, str | bytes):
        raise FilterError(f"'{kind}' must be a list of conditions or groups")
    return sum(_validate_group(child, depth=depth + 1) for child in children)


def _validate_condition(node: Mapping[str, Any]) -> None:
    unknown = set(node.keys()) - {"path", "op", "value"}
    if unknown:
        raise FilterError(f"unknown condition keys: {sorted(unknown)}")
    path = node.get("path")
    if not isinstance(path, str) or not path.strip():
        raise FilterError("condition 'path' must be a non-empty string")
    op = node.get("op")
    if op not in OPS:
        raise FilterError(f"unknown op {op!r}; valid ops: {sorted(OPS)}")
    value = node.get("value")
    if op in _LIST_OPS and not isinstance(value, list):
        raise FilterError(f"op {op!r} requires 'value' to be a list")
    if op == "exists" and value is not None and not isinstance(value, bool):
        raise FilterError("op 'exists' takes a boolean 'value' (or omit it for true)")
    if op in _ORDER_OPS and not _is_number(value) and not isinstance(value, str):
        raise FilterError(f"op {op!r} requires a numeric or string 'value'")
    if op != "exists" and "value" not in node:
        raise FilterError(f"op {op!r} requires a 'value'")


def evaluate_filter(document: Any, event: Mapping[str, Any]) -> EvaluationResult:
    """Evaluate a validated filter against one normalized event payload.

    Pure and total: any structural surprise makes the affected condition
    fail rather than raising, so a stored filter can never crash the
    event worker.
    """
    conditions: list[ConditionResult] = []
    matched = _eval_node(document, event, conditions, depth=0)
    return EvaluationResult(matched=matched, conditions=conditions)


def _eval_node(
    node: Any, event: Mapping[str, Any], out: list[ConditionResult], *, depth: int
) -> bool:
    if depth > MAX_DEPTH or not isinstance(node, Mapping):
        return False
    if not node:
        return True
    if "path" in node or "op" in node:
        result = _eval_condition(node, event)
        out.append(result)
        return result.passed
    if "all" in node and isinstance(node["all"], Sequence):
        # Evaluate every child (no short-circuit) so explanations are complete.
        results = [_eval_node(child, event, out, depth=depth + 1) for child in node["all"]]
        return all(results)
    if "any" in node and isinstance(node["any"], Sequence):
        results = [_eval_node(child, event, out, depth=depth + 1) for child in node["any"]]
        return any(results)
    return False


def resolve_path(event: Mapping[str, Any], path: str) -> Any:
    """Safe dotted-path lookup; returns :data:`MISSING` when absent."""
    cursor: Any = event
    for segment in path.split("."):
        if isinstance(cursor, Mapping):
            if segment not in cursor:
                return MISSING
            cursor = cursor[segment]
        elif isinstance(cursor, Sequence) and not isinstance(cursor, str | bytes):
            if not segment.isdigit() or int(segment) >= len(cursor):
                return MISSING
            cursor = cursor[int(segment)]
        else:
            return MISSING
    return cursor


def changed_from_paths(path: str) -> tuple[str, str] | None:
    """(mirror branch, exact previous path) for a ``data.``-rooted path.

    ``data.state.name`` → (``data.changed_from.state``,
    ``data.changed_from.state.name``). Returns None for paths where
    transition semantics are undefined (not under ``data.``).
    """
    segments = path.split(".")
    if len(segments) < 2 or segments[0] != "data" or _CHANGED_FROM in segments:
        return None
    branch = f"data.{_CHANGED_FROM}.{segments[1]}"
    exact = ".".join(["data", _CHANGED_FROM, *segments[1:]])
    return branch, exact


def _eval_condition(node: Mapping[str, Any], event: Mapping[str, Any]) -> ConditionResult:
    path = str(node.get("path", ""))
    op = str(node.get("op", ""))
    value = node.get("value")
    actual = resolve_path(event, path)
    present = actual is not MISSING

    if op == "transitioned_to":
        return _eval_transitioned_to(path, value, actual, present, event)

    passed, detail = _apply_op(op, value, actual, present)
    return ConditionResult(
        path=path,
        op=op,
        value=value,
        passed=passed,
        actual=actual if present else None,
        actual_present=present,
        detail=detail,
    )


def _eval_transitioned_to(
    path: str, value: Any, actual: Any, present: bool, event: Mapping[str, Any]
) -> ConditionResult:
    paths = changed_from_paths(path)
    if paths is None:
        return ConditionResult(
            path=path,
            op="transitioned_to",
            value=value,
            passed=False,
            actual=actual if present else None,
            actual_present=present,
            detail="transitioned_to requires a path under 'data.'",
        )
    branch_path, exact_path = paths
    branch = resolve_path(event, branch_path)
    exact = resolve_path(event, exact_path)
    exact_present = exact is not MISSING
    # Previous evidence for explanations and idempotency fingerprints: the
    # exact prior value when the connector mirrors it, else the whole
    # changed branch (e.g. Linear only knows the previous state *id*).
    previous = exact if exact_present else branch
    previous_present = previous is not MISSING

    if not present or actual != value:
        passed, detail = False, f"current value is not {value!r}"
    elif branch is MISSING:
        passed, detail = False, "field did not change in this event"
    elif exact_present and exact == value:
        passed, detail = False, f"previous value was already {value!r}"
    else:
        passed, detail = True, f"changed to {value!r}"
    return ConditionResult(
        path=path,
        op="transitioned_to",
        value=value,
        passed=passed,
        actual=actual if present else None,
        actual_present=present,
        previous=previous if previous_present else None,
        previous_present=previous_present,
        detail=detail,
    )


def _apply_op(op: str, value: Any, actual: Any, present: bool) -> tuple[bool, str]:
    if op == "exists":
        expected = True if value is None else bool(value)
        return present is expected, "present" if present else "absent"
    if op == "eq":
        return (present and actual == value), "" if present else "path absent"
    if op == "neq":
        # An absent value is "not equal" — documented DSL semantics.
        return (not present or actual != value), ""
    if op == "in":
        return (present and isinstance(value, list) and actual in value), (
            "" if present else "path absent"
        )
    if op == "not_in":
        return (not present or not isinstance(value, list) or actual not in value), ""
    if op == "contains":
        if not present:
            return False, "path absent"
        if isinstance(actual, str) and isinstance(value, str):
            return value in actual, ""
        if isinstance(actual, Sequence) and not isinstance(actual, str | bytes):
            return value in actual, ""
        return False, "contains requires a string or list on the left"
    if op in _ORDER_OPS:
        return _apply_order_op(op, value, actual, present)
    return False, f"unknown op {op!r}"


def _apply_order_op(op: str, value: Any, actual: Any, present: bool) -> tuple[bool, str]:
    if not present:
        return False, "path absent"
    comparable = (_is_number(actual) and _is_number(value)) or (
        isinstance(actual, str) and isinstance(value, str)
    )
    if not comparable:
        return False, "values are not comparable"
    if op == "gt":
        return actual > value, ""
    if op == "gte":
        return actual >= value, ""
    if op == "lt":
        return actual < value, ""
    return actual <= value, ""


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
