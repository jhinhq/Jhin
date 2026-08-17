"""Routes for the approvals inbox and approve/reject actions (plan 17.11).

Reading the inbox is viewer+; deciding is member+ (plan 20.2: members
operate agents day-to-day; both decisions are audited).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from jhin_api.approvals import service
from jhin_api.approvals.schemas import ApprovalListItem, ApprovalListOut, ApprovalOut
from jhin_api.deps import DbSession, MemberCtx, TemporalDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/approvals",
    tags=["approvals"],
    dependencies=[Depends(csrf_protect)],
)


@router.get("")
async def list_approvals(
    ctx: ViewerCtx,
    db: DbSession,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApprovalListOut:
    items, total, pending_count = await service.list_approvals(
        db, ctx.workspace_id, status_filter=status, limit=limit, offset=offset
    )
    return ApprovalListOut(
        items=[
            ApprovalListItem.model_validate(
                {
                    **ApprovalOut.model_validate(approval, from_attributes=True).model_dump(),
                    "agent_name": agent_name,
                    "task_title": task_title,
                }
            )
            for approval, agent_name, task_title in items
        ],
        total=total,
        pending_count=pending_count,
    )


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: UUID, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> ApprovalOut:
    approval = await service.decide(
        db,
        ctx,
        temporal,
        approval_id,
        decision="approved",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ApprovalOut.model_validate(approval, from_attributes=True)


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: UUID, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> ApprovalOut:
    approval = await service.decide(
        db,
        ctx,
        temporal,
        approval_id,
        decision="rejected",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ApprovalOut.model_validate(approval, from_attributes=True)
