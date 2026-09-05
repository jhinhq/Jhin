"""Schemas for capability grants, approval policies, and the tool catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_api.connections.schemas import ConnectionOut
from jhin_policy import grant_pattern_problem


class GrantCreate(BaseModel):
    capability: str = Field(min_length=1, max_length=200)
    scope: dict[str, str] = Field(default_factory=dict)
    effect: Literal["allow", "deny"] = "allow"

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        # Grant patterns may end in ".*" or be "*" (plan 12.3). The same
        # check runs again in the service, so a writer that never sees this
        # schema (the console) refuses in the same words.
        problem = grant_pattern_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    capability: str
    scope_json: dict[str, Any]
    effect: str
    created_at: datetime
    # What is wrong with this row, as sentences an admin can act on (empty
    # when the row can work as written). A grant that pins a connection that
    # was deleted, or that lacks a key its tool requires, is a grant the
    # gateway refuses on every call; this is where that becomes visible.
    problems: list[str] = []
    # The name of the pinned connection when it still exists in the
    # workspace, so the UI never has to show a bare id.
    connection_name: str | None = None


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
    scope_keys: tuple[str, ...]
    required_grant_scope_keys: tuple[str, ...]
    input_schema: dict[str, Any]


# --- Capability bundles (docs/operations/agent-access.md) ---


class BundleToolOut(BaseModel):
    name: str
    capability: str
    # The bundle's fixed scope values; connection ids are filled at apply time.
    scope: dict[str, str]


class BundleNeedChoiceOut(BaseModel):
    id: UUID
    name: str
    status: str
    allowed_repositories: list[str] | None = None


class BundleNeedOut(BaseModel):
    """A question the operator has to answer before the bundle can be
    written: connect an app, choose between two, create a sandbox, or a tool
    this workspace's catalog does not offer."""

    kind: Literal["connect", "choose", "create_sandbox", "catalog"]
    connector_type: str
    choices: list[BundleNeedChoiceOut] = []
    detail: str = ""


class BundleReadinessOut(BaseModel):
    state: Literal["ready", "needs", "unavailable"]
    needs: list[BundleNeedOut] = []
    missing_tools: list[str] = []


class BundleOut(BaseModel):
    id: str
    label: str
    summary: str
    description: str
    tools: list[BundleToolOut]
    rules: list[PolicyRuleIn]
    not_included: list[str]
    readiness: BundleReadinessOut


class BundleGrantProblemOut(BaseModel):
    grant_id: UUID
    capability: str
    problems: list[str]


class BundleStatusOut(BundleOut):
    """A bundle as it stands on one agent."""

    state: Literal["on", "partial", "off"]
    granted_capabilities: list[str]
    missing_capabilities: list[str]
    problems: list[BundleGrantProblemOut] = []


class SandboxCreate(BaseModel):
    """A CLI Sandbox connection to create alongside the Code editing bundle,
    pointing at the GitHub connection whose credential it borrows."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=200)
    git_connection_id: UUID
    allowed_repositories: list[str] = Field(default=["*"], max_length=50)


class BundleApply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # connector type -> connection id, for the types that need choosing.
    connections: dict[str, UUID] = Field(default_factory=dict)
    repositories: list[str] = Field(default=["*"], max_length=50)
    base: str | None = Field(default=None, max_length=200)
    sandbox: SandboxCreate | None = None
    dry_run: bool = False


class BundleApplyOut(BaseModel):
    """What applying the bundle wrote — or, under ``dry_run``, would write.

    Preview rows in ``grants_created`` carry a nil id and the current time,
    since they do not exist yet; ``grants_existing`` are real rows.
    """

    bundle_id: str
    dry_run: bool
    created_connection: ConnectionOut | None = None
    grants_created: list[GrantOut] = []
    grants_existing: list[GrantOut] = []
    rules_added: list[PolicyRuleIn] = []
    rules_kept: list[PolicyRuleIn] = []
    callable_tools: list[str] = []
    needs: list[BundleNeedOut] = []
    warnings: list[str] = []


class BundleRemoveOut(BaseModel):
    bundle_id: str
    dry_run: bool
    revoked: list[GrantOut] = []
    # Rows among ``revoked`` whose scope is not one the bundle itself writes,
    # so the confirmation can name what an admin added by hand.
    hand_made: list[GrantOut] = []
