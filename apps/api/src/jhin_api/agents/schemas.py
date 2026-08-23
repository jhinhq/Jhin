"""Request/response contracts for agents (plan 6.5)."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from jhin_api.media.urls import avatar_url_for
from jhin_domain import AVATAR_COLORS, AVATAR_SHAPES, AgentStatus, AutonomyLevel, AvatarKind

Discoverability = Literal["discoverable", "hidden"]
Availability = Literal["available", "unavailable"]
MembershipState = Literal["active", "inactive"]
RelationshipKind = Literal["close_collaborator", "advisor", "preferred_reviewer"]
RelationshipStatus = Literal["active", "inactive"]
ExpertiseTag = Annotated[str, Field(min_length=1, max_length=64)]


class AgentMembershipOut(BaseModel):
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    team_id: UUID
    is_primary: bool
    role_label: str
    joined_at: datetime
    left_at: datetime | None
    state: MembershipState


class MembershipReplace(BaseModel):
    primary_team_id: UUID | None
    secondary_team_ids: list[UUID] = Field(max_length=100)


class RelationshipCreate(BaseModel):
    target_agent_id: UUID
    kind: RelationshipKind
    purpose: str = Field(default="", max_length=1000)


class AgentRelationshipOut(BaseModel):
    id: UUID
    workspace_id: UUID
    source_agent_id: UUID
    target_agent_id: UUID
    kind: RelationshipKind
    purpose: str
    status: RelationshipStatus
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    system_prompt: str = Field(default="", max_length=100_000)
    team_id: UUID | None = None
    secondary_team_ids: list[UUID] = Field(default_factory=list, max_length=100)
    manager_agent_id: UUID | None = None
    public_purpose: str = Field(default="", max_length=1000)
    expertise_json: list[ExpertiseTag] = Field(default_factory=list, max_length=20)
    discoverability: Discoverability = "discoverable"
    availability: Availability = "available"
    status: AgentStatus = AgentStatus.ACTIVE
    autonomy_level: AutonomyLevel = AutonomyLevel.SUPERVISED
    # Null = inherit the workspace default profile (plan 15.2).
    model_profile_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_steps: int = Field(default=20, ge=1, le=500)
    max_run_minutes: int = Field(default=30, ge=1, le=24 * 60)
    # Concurrency admission (plan 30): active runs beyond this queue visibly.
    max_concurrent_runs: int = Field(default=1, ge=1, le=50)
    monthly_budget_cents: int | None = Field(default=None, ge=0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    # Free brand-cube avatar at creation (both or neither). When set, the
    # agent starts with ``avatar_kind == "shape"`` instead of initials.
    avatar_shape: str | None = None
    avatar_color: str | None = None

    @field_validator("expertise_json")
    @classmethod
    def expertise_tags_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("expertise_json tags must be unique")
        return value

    @field_validator("avatar_shape")
    @classmethod
    def shape_is_known(cls, value: str | None) -> str | None:
        if value is not None and value not in AVATAR_SHAPES:
            raise ValueError(f"avatar_shape must be one of: {', '.join(AVATAR_SHAPES)}")
        return value

    @field_validator("avatar_color")
    @classmethod
    def color_is_in_palette(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in AVATAR_COLORS:
            raise ValueError("avatar_color must be one of the fixed palette hex values")
        return normalized

    @model_validator(mode="after")
    def shape_and_color_together(self) -> Self:
        if (self.avatar_shape is None) != (self.avatar_color is None):
            raise ValueError("avatar_shape and avatar_color must be set together")
        return self


class AgentUpdate(BaseModel):
    """PATCH semantics: omitted fields are untouched; explicit nulls clear."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    team_id: UUID | None = None
    secondary_team_ids: list[UUID] = Field(default_factory=list, max_length=100)
    manager_agent_id: UUID | None = None
    public_purpose: str = Field(default="", max_length=1000)
    expertise_json: list[ExpertiseTag] = Field(default_factory=list, max_length=20)
    discoverability: Discoverability = "discoverable"
    availability: Availability = "available"
    status: AgentStatus | None = None
    autonomy_level: AutonomyLevel | None = None
    model_profile_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_steps: int | None = Field(default=None, ge=1, le=500)
    max_run_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_concurrent_runs: int | None = Field(default=None, ge=1, le=50)
    monthly_budget_cents: int | None = Field(default=None, ge=0)
    metadata_json: dict[str, Any] | None = None

    @field_validator("expertise_json")
    @classmethod
    def expertise_tags_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("expertise_json tags must be unique")
        return value


class AgentOut(BaseModel):
    id: UUID
    workspace_id: UUID
    team_id: UUID | None
    manager_agent_id: UUID | None
    name: str
    slug: str
    role_title: str
    description: str
    system_prompt: str
    public_purpose: str
    expertise_json: list[str]
    discoverability: Discoverability
    availability: Availability
    status: AgentStatus
    autonomy_level: AutonomyLevel
    model_profile_id: UUID | None
    temperature: float | None
    max_output_tokens: int | None
    max_steps: int
    max_run_minutes: int
    max_concurrent_runs: int
    monthly_budget_cents: int | None
    metadata_json: dict[str, Any]
    memberships: list[AgentMembershipOut] = Field(default_factory=list)
    relationships: list[AgentRelationshipOut] = Field(default_factory=list)
    # Avatar (experience design: media). ``avatar_url`` is a relative,
    # authenticated path (append ``?size=64|128|256``) or null for initials.
    avatar_kind: AvatarKind = AvatarKind.INITIALS
    active_avatar_asset_id: UUID | None = None
    avatar_url: str | None = None
    # Free brand-cube avatar (kind == "shape"); both null otherwise.
    avatar_shape: str | None = None
    avatar_color: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def derive_avatar_url(self) -> Self:
        if self.avatar_url is None and self.active_avatar_asset_id is not None:
            self.avatar_url = avatar_url_for(self.workspace_id, self.active_avatar_asset_id)
        return self
