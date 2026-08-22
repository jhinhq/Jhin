"""Coordination routes under /api/v1/workspaces/{workspace_id}:

work-requests            list (viewer), create on behalf (member), detail
                         (viewer), accept/decline/clarify (admin)
review-policies          list (viewer), create/update/delete (admin)
reviews                  inbox (viewer), detail (viewer), decide (member;
                         AI-assigned reviews need admin)
agents/{id}/rollup       manager rollup (viewer)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from jhin_api.coordination import service
from jhin_api.coordination.schemas import (
    ReviewDecisionIn,
    ReviewPolicyIn,
    ReviewPolicyOut,
    ReviewPolicyUpdate,
    WorkRequestCreate,
    WorkRequestListOut,
    WorkRequestOut,
    WorkRequestResponseIn,
    WorkReviewListOut,
    WorkReviewOut,
)
from jhin_api.deps import AdminCtx, DbSession, MemberCtx, TemporalDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_domain import WorkRequestStatus, WorkReviewStatus
from jhin_tools.rollups import ManagerRollup

RequestId = Annotated[UUID, Depends(req_id)]
IpHash = Annotated[str, Depends(ip_hash)]

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["coordination"],
    dependencies=[Depends(csrf_protect)],
)


def _valid(value: str | None, allowed: set[str], label: str) -> str | None:
    if value is not None and value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown {label} '{value}'"
        )
    return value


# --- work requests ---


@router.get("/work-requests")
async def list_work_requests(
    ctx: ViewerCtx,
    db: DbSession,
    status: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> WorkRequestListOut:
    items, total = await service.list_work_requests(
        db,
        ctx.workspace_id,
        status_filter=_valid(status, {s.value for s in WorkRequestStatus}, "status"),
        agent_id=agent_id,
        limit=limit,
        offset=offset,
    )
    return WorkRequestListOut(items=items, total=total)


@router.post("/work-requests", status_code=status.HTTP_201_CREATED)
async def create_work_request(
    ctx: MemberCtx,
    db: DbSession,
    body: WorkRequestCreate,
    response: Response,
    request_id: RequestId,
    ip: IpHash,
) -> WorkRequestOut:
    request, created = await service.create_work_request(
        db, ctx, body, request_id=request_id, ip_hash=ip
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return await service.project_work_request(db, ctx.workspace_id, request)


@router.get("/work-requests/{work_request_id}")
async def get_work_request(ctx: ViewerCtx, db: DbSession, work_request_id: UUID) -> WorkRequestOut:
    request = await service.get_work_request(db, ctx.workspace_id, work_request_id)
    return await service.project_work_request(db, ctx.workspace_id, request)


async def _respond(
    ctx: AdminCtx,
    db: DbSession,
    temporal: TemporalDep,
    work_request_id: UUID,
    body: WorkRequestResponseIn,
    decision: str,
    request_id: UUID,
    ip: str,
) -> WorkRequestOut:
    request = await service.respond_work_request(
        db,
        ctx,
        temporal,
        work_request_id,
        decision=decision,
        response=body.response,
        request_id=request_id,
        ip_hash=ip,
    )
    return await service.project_work_request(db, ctx.workspace_id, request)


@router.post("/work-requests/{work_request_id}/accept")
async def accept_work_request(
    ctx: AdminCtx,
    db: DbSession,
    temporal: TemporalDep,
    work_request_id: UUID,
    body: WorkRequestResponseIn,
    request_id: RequestId,
    ip: IpHash,
) -> WorkRequestOut:
    return await _respond(ctx, db, temporal, work_request_id, body, "accept", request_id, ip)


@router.post("/work-requests/{work_request_id}/decline")
async def decline_work_request(
    ctx: AdminCtx,
    db: DbSession,
    temporal: TemporalDep,
    work_request_id: UUID,
    body: WorkRequestResponseIn,
    request_id: RequestId,
    ip: IpHash,
) -> WorkRequestOut:
    return await _respond(ctx, db, temporal, work_request_id, body, "decline", request_id, ip)


@router.post("/work-requests/{work_request_id}/clarify")
async def clarify_work_request(
    ctx: AdminCtx,
    db: DbSession,
    temporal: TemporalDep,
    work_request_id: UUID,
    body: WorkRequestResponseIn,
    request_id: RequestId,
    ip: IpHash,
) -> WorkRequestOut:
    return await _respond(ctx, db, temporal, work_request_id, body, "clarify", request_id, ip)


# --- review policies ---


@router.get("/review-policies")
async def list_review_policies(ctx: ViewerCtx, db: DbSession) -> list[ReviewPolicyOut]:
    rows = await service.list_review_policies(db, ctx.workspace_id)
    return [ReviewPolicyOut.model_validate(r) for r in rows]


@router.post("/review-policies", status_code=status.HTTP_201_CREATED)
async def create_review_policy(
    ctx: AdminCtx,
    db: DbSession,
    body: ReviewPolicyIn,
    request_id: RequestId,
    ip: IpHash,
    temporal: TemporalDep,
) -> ReviewPolicyOut:
    policy = await service.create_review_policy(
        db, ctx, body, request_id=request_id, ip_hash=ip, temporal=temporal
    )
    return ReviewPolicyOut.model_validate(policy)


@router.get("/review-policies/{policy_id}")
async def get_review_policy(ctx: ViewerCtx, db: DbSession, policy_id: UUID) -> ReviewPolicyOut:
    return ReviewPolicyOut.model_validate(
        await service.get_review_policy(db, ctx.workspace_id, policy_id)
    )


@router.patch("/review-policies/{policy_id}")
async def update_review_policy(
    ctx: AdminCtx,
    db: DbSession,
    policy_id: UUID,
    body: ReviewPolicyUpdate,
    request_id: RequestId,
    ip: IpHash,
    temporal: TemporalDep,
) -> ReviewPolicyOut:
    policy = await service.update_review_policy(
        db, ctx, policy_id, body, request_id=request_id, ip_hash=ip, temporal=temporal
    )
    return ReviewPolicyOut.model_validate(policy)


@router.delete("/review-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review_policy(
    ctx: AdminCtx,
    db: DbSession,
    policy_id: UUID,
    request_id: RequestId,
    ip: IpHash,
    temporal: TemporalDep,
) -> Response:
    await service.delete_review_policy(
        db, ctx, policy_id, request_id=request_id, ip_hash=ip, temporal=temporal
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- reviews ---


@router.get("/reviews")
async def list_reviews(
    ctx: ViewerCtx,
    db: DbSession,
    status: str | None = None,
    reviewer: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> WorkReviewListOut:
    items, total, pending = await service.list_reviews(
        db,
        ctx.workspace_id,
        status_filter=_valid(status, {s.value for s in WorkReviewStatus}, "status"),
        reviewer=_valid(reviewer, {"human", "agent"}, "reviewer"),
        limit=limit,
        offset=offset,
    )
    return WorkReviewListOut(items=items, total=total, pending_count=pending)


@router.get("/reviews/{review_id}")
async def get_review(ctx: ViewerCtx, db: DbSession, review_id: UUID) -> WorkReviewOut:
    review = await service.get_review(db, ctx.workspace_id, review_id)
    return (await service.project_reviews(db, ctx.workspace_id, [review]))[0]


@router.post("/reviews/{review_id}/decide")
async def decide_review(
    ctx: MemberCtx,
    db: DbSession,
    review_id: UUID,
    body: ReviewDecisionIn,
    request_id: RequestId,
    ip: IpHash,
    temporal: TemporalDep,
) -> WorkReviewOut:
    review = await service.decide_review(
        db,
        ctx,
        review_id,
        verdict=body.verdict.value,
        feedback=body.feedback,
        request_id=request_id,
        ip_hash=ip,
        temporal=temporal,
    )
    return (await service.project_reviews(db, ctx.workspace_id, [review]))[0]


# --- rollups ---


@router.get("/agents/{agent_id}/rollup")
async def get_manager_rollup(ctx: ViewerCtx, db: DbSession, agent_id: UUID) -> ManagerRollup:
    return await service.manager_rollup(db, ctx.workspace_id, agent_id)
