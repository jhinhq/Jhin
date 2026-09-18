"""Typed Ghost tools. No generic status update or arbitrary API path exists."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GhostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str


class PostReadInput(GhostInput):
    post_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=6000)


class PostListInput(GhostInput):
    limit: int = Field(default=10, ge=1, le=30)
    page: int = Field(default=1, ge=1, le=1000)
    status: Literal["draft", "published", "all"] = "all"


class DraftCreateInput(GhostInput):
    assignment_id: str
    expected_editorial_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    html: str = Field(min_length=1, max_length=100_000)
    slug: str = Field(min_length=1, max_length=190, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    excerpt: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=15)
    feature_image: str | None = Field(default=None, max_length=2000)
    feature_image_alt: str = Field(default="", max_length=300)
    feature_image_caption: str = Field(default="", max_length=1000)
    meta_title: str = Field(default="", max_length=300)
    meta_description: str = Field(default="", max_length=500)
    authors: list[str] = Field(default_factory=list, max_length=10)
    canonical_url: str | None = Field(default=None, max_length=2000)


class DraftUpdateInput(DraftCreateInput):
    post_id: str
    expected_updated_at: str = Field(min_length=1, max_length=50)


class ReviewRequestInput(PostReadInput):
    assignment_id: str
    expected_editorial_version: int = Field(ge=1)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=1, max_length=3000)


class ReviewDecisionInput(GhostInput):
    review_id: str
    verdict: Literal["approved", "changes_requested"]
    feedback: str = Field(min_length=1, max_length=4000)


class PublishInput(GhostInput):
    review_id: str


class ReviewReadInput(PublishInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=6000)


class ReviewPackageOutput(BaseModel):
    review_id: str
    package_id: str
    revision: str
    package_json: str
    offset: int
    total_chars: int
    next_offset: int | None
    complete: bool


class PostOutput(BaseModel):
    post_id: str
    title: str
    slug: str
    status: str
    updated_at: str
    revision: str
    url: str = ""
    html: str = ""
    feature_image: str | None = None
    feature_image_alt: str = ""
    feature_image_caption: str = ""
    tags: list[dict[str, Any]] = Field(default_factory=list)
    authors: list[dict[str, Any]] = Field(default_factory=list)
    custom_excerpt: str = ""
    meta_title: str = ""
    meta_description: str = ""
    canonical_url: str | None = None
    visibility: str = "public"
    html_total_chars: int = 0
    html_offset: int = 0
    next_offset: int | None = None
    complete: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    assignment_id: str | None = None
    editorial_version: int | None = None


class PostListOutput(BaseModel):
    posts: list[PostOutput]
    page: int
    next_page: int | None = None


class ReviewOutput(BaseModel):
    review_id: str
    post_id: str
    revision: str
    status: str
    publisher_agent_id: str
    work_request_id: str | None = None
    feedback: str = ""
    created_task_id: str | None = None
    agent_id: str | None = None
    activated: bool = False
    detail: str = ""
    assignment_id: str | None = None
    package_id: str | None = None
    release_intent: str = "draft_only"
    revision_round: int = 1
