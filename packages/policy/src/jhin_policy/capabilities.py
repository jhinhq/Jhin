"""Tool definitions and the capability registry (plan 12.1, 12.3).

Capabilities are dotted hierarchical names (``system.echo``,
``github.pull_request.create``). A :class:`ToolDefinition` binds one tool to
the capability required to call it, its risk level, and strict input/output
schemas — model output is validated against these schemas and is never
authorization by itself (plan 52).

The registry is deliberately connector-agnostic: Phase 5 connectors register
more tools the same way the Phase 4 built-ins do.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from jhin_policy.risk import RiskLevel

_CAPABILITY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_SCOPE_KEY_RE = re.compile(r"^[a-z0-9_]+$")

# Capabilities agents must never hold: anything that would let an agent grant
# itself (or another agent) power, alter policy, or read stored credentials
# (plan 21 controls 10-12, invariant 48.15). Registration of a tool under
# these prefixes fails loudly.
FORBIDDEN_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "agent.permission",
    "agent.grant",
    "agent.policy",
    "workspace.member",
    "capability",
    "policy",
    "approval",
    "secret",
    "auth",
)


def is_valid_capability(name: str) -> bool:
    """A capability is a non-empty dotted path of lowercase tokens."""
    return bool(_CAPABILITY_RE.match(name))


def capability_matches(pattern: str, capability: str) -> bool:
    """Match a grant pattern against a concrete capability.

    Supported forms (plan 12.3 hierarchy):

    - exact: ``system.echo`` matches only ``system.echo``;
    - subtree: ``system.*`` matches ``system.echo`` and ``system.note.append``;
    - everything: ``*`` matches any capability.

    No other wildcard positions are supported — patterns stay auditable.
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return is_valid_capability(prefix) and capability.startswith(prefix + ".")
    return pattern == capability


def is_forbidden_capability(capability: str) -> bool:
    return any(
        capability == prefix or capability.startswith(prefix + ".")
        for prefix in FORBIDDEN_CAPABILITY_PREFIXES
    )


def grant_pattern_problem(pattern: str) -> str | None:
    """Why ``pattern`` can never be a grant's capability, or ``None``.

    A grant names a capability exactly, a subtree (``github.*``) or
    everything (``*``); anything else is not a pattern, and a pattern in a
    forbidden namespace is refused whatever its shape. Every grant writer —
    the HTTP schema and the service the console drives — asks this one
    function, so all of them refuse in the same words.
    """
    base = pattern.removesuffix(".*") if pattern.endswith(".*") else pattern
    if pattern != "*" and not is_valid_capability(base):
        return "not a valid dotted capability name or pattern"
    if is_forbidden_capability(base):
        return "capabilities in this namespace can never be granted to agents"
    return None


class ToolDefinition(BaseModel):
    """One callable tool (plan 12.1)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    risk: RiskLevel
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    required_capability: str
    supports_approval: bool = False
    # Input fields that form the call's authorization scope (plan 6.6), e.g.
    # ("repository", "branch") for a Phase 5 GitHub tool. The gateway extracts
    # these from the validated input and matches them against grant scopes.
    # Phase 4 system tools are unscoped.
    scope_keys: tuple[str, ...] = ()
    # Some provider operations must never be authorized by an unscoped
    # exact or wildcard capability grant. Every matching allow grant must
    # carry these keys, and they must cover the requested values.
    required_grant_scope_keys: tuple[str, ...] = ()
    # True for tools whose grant scopes carry semantics the generic fnmatch
    # matcher cannot express (e.g. organization.delegate relationship scopes,
    # plan 7.5). The generic evaluator then checks capability + rules only
    # and a tool-specific validator registered with the catalog owns scope
    # enforcement — still policy code, never model output (plan 52).
    defers_scope: bool = False

    @field_validator("name", "required_capability")
    @classmethod
    def _validate_capability_form(cls, value: str) -> str:
        if not is_valid_capability(value):
            raise ValueError(f"not a valid dotted capability name: {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_scope_contract(self) -> ToolDefinition:
        for field_name, keys in (
            ("scope_keys", self.scope_keys),
            ("required_grant_scope_keys", self.required_grant_scope_keys),
        ):
            display_name = (
                "required grant scope keys"
                if field_name == "required_grant_scope_keys"
                else "scope keys"
            )
            if len(keys) != len(set(keys)):
                raise ValueError(f"{display_name} must contain unique keys")
            invalid = [key for key in keys if not _SCOPE_KEY_RE.fullmatch(key)]
            if invalid:
                raise ValueError(f"{display_name} contain invalid scope key(s): {invalid!r}")
        missing = set(self.required_grant_scope_keys) - set(self.scope_keys)
        if missing:
            raise ValueError(
                "required grant scope keys must be a subset of scope keys; "
                f"missing {sorted(missing)!r}"
            )
        if self.defers_scope and self.required_grant_scope_keys:
            raise ValueError(
                "defers_scope tools cannot declare required grant scope keys; "
                "their validator owns the complete scope contract"
            )
        return self

    def input_json_schema(self) -> dict[str, object]:
        """JSON schema advertised to the model as the function signature."""
        return self.input_model.model_json_schema()


class RegistryError(Exception):
    pass


class CapabilityRegistry:
    """Lookup table of registered tools, keyed by tool name.

    Deny-by-default flows from here: a tool that is not registered cannot be
    described, validated, or executed, and an agent without a grant matching
    ``required_capability`` is denied by the evaluator.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise RegistryError(f"tool already registered: {definition.name}")
        if is_forbidden_capability(definition.required_capability) or is_forbidden_capability(
            definition.name
        ):
            raise RegistryError(
                f"refusing to register self-modification capability: {definition.name} "
                f"(requires {definition.required_capability})"
            )
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)
