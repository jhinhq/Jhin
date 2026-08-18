"""Strict request and display-safe response models for Vercel tools."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Environment = Literal["preview", "production"]
GitProvider = Literal["github", "gitlab", "bitbucket"]
ShortTarget = Annotated[str, Field(min_length=1, max_length=50)]


class VercelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VercelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1, max_length=100)


class ScopedProjectInput(VercelInput):
    project_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:{}-]+$")


class ScopedDeploymentInput(ScopedProjectInput):
    deployment_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:{}-]+$")


class ProjectListInput(VercelInput):
    limit: int = Field(default=50, ge=1, le=200)


class ProjectInfo(VercelOutput):
    project_id: str = Field(max_length=200)
    name: str = Field(max_length=256)
    framework: str = Field(default="", max_length=100)
    created_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    updated_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    git_provider: str = Field(default="", max_length=50)
    repository_id: str = Field(default="", max_length=200)


class ProjectListOutput(VercelOutput):
    projects: list[ProjectInfo] = Field(max_length=200)
    truncated: bool = False


class ProjectReadInput(ScopedProjectInput):
    pass


class ProjectReadOutput(ProjectInfo):
    pass


class DeploymentListInput(ScopedProjectInput):
    limit: int = Field(default=20, ge=1, le=200)


class DeploymentInfo(VercelOutput):
    deployment_id: str = Field(max_length=200)
    project_id: str = Field(max_length=200)
    name: str = Field(max_length=256)
    url: str = Field(default="", max_length=2_048)
    state: str = Field(default="", max_length=50)
    target: str = Field(default="", max_length=50)
    created_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    ready_at: int | None = Field(default=None, ge=0, le=2**63 - 1)


class DeploymentListItem(VercelOutput):
    deployment_id: str = Field(max_length=200)
    project_id: str = Field(max_length=200)
    state: str = Field(default="", max_length=50)
    target: str = Field(default="", max_length=50)
    created_at: int | None = Field(default=None, ge=0, le=2**63 - 1)


class DeploymentListOutput(VercelOutput):
    deployments: list[DeploymentListItem] = Field(max_length=200)
    truncated: bool = False


class DeploymentReadInput(ScopedDeploymentInput):
    pass


class DeploymentReadOutput(DeploymentInfo):
    inspector_url: str = Field(default="", max_length=2_048)


class DeploymentLogsInput(ScopedDeploymentInput):
    limit: int = Field(default=100, ge=1, le=200)
    since: int | None = Field(
        default=None, ge=0, le=2**63 - 1, description="Unix epoch milliseconds."
    )
    until: int | None = Field(
        default=None, ge=0, le=2**63 - 1, description="Unix epoch milliseconds."
    )

    @model_validator(mode="after")
    def _validate_time_window(self) -> Self:
        if self.since is not None and self.until is not None:
            if self.until < self.since:
                raise ValueError("until must not be earlier than since")
            if self.until - self.since > 86_400_000:
                raise ValueError("deployment build-log window cannot exceed 24 hours")
        return self


class DeploymentLogEvent(VercelOutput):
    event_id: str = Field(default="", max_length=200)
    timestamp: int = Field(ge=0, le=2**63 - 1)
    event_type: str = Field(default="", max_length=100)
    level: str = Field(default="", max_length=50)
    message: str = Field(default="", max_length=4_000)


class DeploymentLogsOutput(VercelOutput):
    events: list[DeploymentLogEvent] = Field(max_length=200)
    truncated: bool = False


class EnvironmentMetadataInput(ScopedProjectInput):
    pass


class EnvironmentVariableMetadata(VercelOutput):
    environment_id: str = Field(default="", max_length=200)
    key: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=256)
    targets: list[ShortTarget] = Field(default_factory=list, max_length=10)
    variable_type: str = Field(default="", max_length=50)
    created_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    updated_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    git_branch: str = Field(default="", max_length=250)


class EnvironmentMetadataOutput(VercelOutput):
    variables: list[EnvironmentVariableMetadata] = Field(max_length=200)
    truncated: bool = False


class PreviewCreateInput(ScopedProjectInput):
    environment: Literal["preview"] = "preview"
    git_provider: GitProvider
    repository_id: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=250, pattern=r"^[^\x00-\x1f\x7f]+$")


class RedeployInput(ScopedDeploymentInput):
    environment: Environment


class PromoteInput(ScopedDeploymentInput):
    environment: Literal["production"] = "production"


class AliasAssignInput(ScopedDeploymentInput):
    environment: Literal["production"] = "production"
    alias: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9.-]+$")

    @model_validator(mode="after")
    def _validate_dns_labels(self) -> Self:
        labels = self.alias.split(".")
        if any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in labels
        ):
            raise ValueError("alias contains an invalid DNS label")
        return self


class DeploymentMutationOutput(DeploymentInfo):
    action: str = Field(max_length=50)


class AliasAssignOutput(VercelOutput):
    deployment_id: str = Field(max_length=200)
    project_id: str = Field(max_length=200)
    alias: str = Field(max_length=253)
    assigned: bool
