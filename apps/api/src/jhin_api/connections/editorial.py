"""Read-only editorial evidence. Only the designated agent can publish."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import GhostEditorialReview
from jhin_db.models.editorial import EditorialAssignment, EditorialReviewPackage
from jhin_secrets import get_redactor
from jhin_tools.sanitize import sanitize_payload

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/connections/{connection_id}/editorial-reviews",
    tags=["editorial-reviews"],
)


class EditorialReviewOut(BaseModel):
    review_id: UUID
    connection_id: UUID
    post_id: str
    revision: str
    status: str
    publisher_agent_id: UUID
    author_agent_id: UUID
    title: str
    provider_updated_at: str
    feedback: str
    url: str
    work_request_id: UUID | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    published_at: datetime | None
    html: str | None = None
    assignment_id: UUID | None = None
    package_id: UUID | None = None
    release_intent: str = "draft_only"
    revision_round: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    package: dict[str, Any] | None = None
    complete: bool = True
    html_total_chars: int = 0


class EditorialReviewListOut(BaseModel):
    items: list[EditorialReviewOut]
    next_before_id: UUID | None


def _out(row: GhostEditorialReview, *, detail: bool = False) -> EditorialReviewOut:
    safe = sanitize_payload(
        row.snapshot_json, max_string_chars=2_000_000, max_document_bytes=4_194_304
    )
    return EditorialReviewOut(
        review_id=row.id,
        connection_id=row.connection_id,
        post_id=row.post_id,
        revision=row.revision,
        status=row.status,
        publisher_agent_id=row.publisher_agent_id,
        author_agent_id=row.author_agent_id,
        title=str(safe.get("title", "")),
        provider_updated_at=row.provider_updated_at,
        feedback=get_redactor().redact_text(row.feedback)[:4000],
        url=str(safe.get("url", "")),
        work_request_id=row.work_request_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        decided_at=row.decided_at,
        published_at=row.published_at,
        html=str(safe.get("html", "")) if detail else None,
        assignment_id=row.assignment_id,
        package_id=row.package_id,
        release_intent=row.release_intent,
        revision_round=row.revision_round,
        metadata={key: value for key, value in safe.items() if key not in {"html", "lexical"}}
        if detail
        else {},
        html_total_chars=len(str(row.snapshot_json.get("html", ""))),
        complete=not safe.get("truncated", False) and "…[truncated]" not in str(safe),
    )


class AssignmentOut(BaseModel):
    id: UUID
    connection_id: UUID
    writer_agent_id: UUID
    publisher_agent_id: UUID
    release_intent: str
    phase: str
    brief: dict[str, Any]
    version: int
    editorial_version: int
    brief_version: int
    post_id: str | None


class AssignmentReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    release_intent: Literal["draft_only", "publish_after_ashley_review"]


def _assignment_out(row: EditorialAssignment) -> AssignmentOut:
    return AssignmentOut(
        id=row.id,
        connection_id=row.connection_id,
        writer_agent_id=row.writer_agent_id,
        publisher_agent_id=row.publisher_agent_id,
        release_intent=row.release_intent,
        phase=row.phase,
        brief=sanitize_payload(row.brief_json),
        version=row.version,
        editorial_version=row.editorial_version,
        brief_version=row.brief_version,
        post_id=row.post_id,
    )


@router.get("/assignments/{assignment_id}")
async def get_editorial_assignment(
    ctx: ViewerCtx, db: DbSession, connection_id: UUID, assignment_id: UUID, response: Response
) -> AssignmentOut:
    response.headers["Cache-Control"] = "private, no-store"
    row = await db.scalar(
        select(EditorialAssignment).where(
            EditorialAssignment.workspace_id == ctx.workspace_id,
            EditorialAssignment.connection_id == connection_id,
            EditorialAssignment.id == assignment_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Editorial assignment not found")
    return _assignment_out(row)


@router.put("/assignments/{assignment_id}/release-intent", dependencies=[Depends(csrf_protect)])
async def set_editorial_release_intent(
    ctx: AdminCtx,
    db: DbSession,
    connection_id: UUID,
    assignment_id: UUID,
    data: AssignmentReleaseIn,
) -> AssignmentOut:
    row = await db.scalar(
        select(EditorialAssignment)
        .where(
            EditorialAssignment.workspace_id == ctx.workspace_id,
            EditorialAssignment.connection_id == connection_id,
            EditorialAssignment.id == assignment_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Editorial assignment not found")
    if row.phase == "cancelled" or row.version != data.expected_version:
        raise HTTPException(409, "Assignment cancelled or version changed")
    if row.release_intent != data.release_intent:
        row.release_intent = data.release_intent
        row.editorial_version += 1
        row.version += 1
        row.decision_provenance = {
            **row.decision_provenance,
            "release_intent": {
                "user_id": str(ctx.user.id),
                "authorized_at": datetime.now().isoformat(),
                "intent": data.release_intent,
            },
        }
    await db.commit()
    return _assignment_out(row)


@router.get("")
async def list_editorial_reviews(
    ctx: ViewerCtx,
    db: DbSession,
    connection_id: UUID,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: UUID | None = None,
) -> EditorialReviewListOut:
    response.headers["Cache-Control"] = "private, no-store"
    query = select(GhostEditorialReview).where(
        GhostEditorialReview.workspace_id == ctx.workspace_id,
        GhostEditorialReview.connection_id == connection_id,
    )
    if before_id is not None:
        query = query.where(GhostEditorialReview.id < before_id)
    rows = list(await db.scalars(query.order_by(GhostEditorialReview.id.desc()).limit(limit + 1)))
    page = rows[:limit]
    return EditorialReviewListOut(
        items=[_out(row) for row in page], next_before_id=page[-1].id if len(rows) > limit else None
    )


@router.get("/{review_id}")
async def get_editorial_review(
    ctx: ViewerCtx,
    db: DbSession,
    connection_id: UUID,
    review_id: UUID,
    response: Response,
) -> EditorialReviewOut:
    response.headers["Cache-Control"] = "private, no-store"
    row = await db.scalar(
        select(GhostEditorialReview).where(
            GhostEditorialReview.workspace_id == ctx.workspace_id,
            GhostEditorialReview.connection_id == connection_id,
            GhostEditorialReview.id == review_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Editorial review not found")
    result = _out(row, detail=True)
    package = await db.get(EditorialReviewPackage, row.package_id) if row.package_id else None
    if package is not None and package.workspace_id == ctx.workspace_id:
        result.package = sanitize_payload(
            package.manifest_json, max_string_chars=2_000_000, max_document_bytes=4_194_304
        )
    return result
