"""Schemas for capability grants, approval policies, and the tool catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_policy import is_forbidden_capability, is_valid_capability


class GrantCreate(BaseModel):
    capability: str = Field(min_length=1, max_length=200)
    scope: dict[str, str] = Field(default_factory=dict)
    effect: Literal["allow", "deny"] = "allow"

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        # Grant patterns may end in ".*" or be "*" (plan 12.3).
        base = value.removesuffix(".*") if value.endswith(".*") else value
        if value != "*" and not is_valid_capability(base):
            raise ValueError("not a valid dotted capability name or pattern")
        if is_forbidden_capability(base):
            raise ValueError("capabilities in this namespace can never be granted to agents")
        return value


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    capability: str
    scope_json: dict[str, Any]
    effect: str
    created_at: datetime


class PolicyRuleIn(BaseModel):
    capability: str = Field(default="*", max_length=200)
    risk: Literal["read", "write", "elevated", "destructive"] | None = None
    action: Literal["auto", "approval", "forbid"]


class PolicyUpdate(BaseModel):
    """Set the agent's approval policy: a preset name or explicit rules.

    A preset is only a shortcut — it expands to explicit rules which are what
    gets persisted (plan 42)."""

    preset: Literal["autonomous", "balanced", "restricted"] | None = None
    rules: list[PolicyRuleIn] | None = None


class PolicyOut(BaseModel):
    rules: list[PolicyRuleIn]
    # The preset whose expansion equals the current rules, if any.
    preset: str | None
    autonomy_level: str


class ToolOut(BaseModel):
    name: str
    description: str
    risk: str
    required_capability: str
    supports_approval: bool
    input_schema: dict[str, Any]
