"""Human-admin controls for prospective, revocable memory capture."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_api.deps import AdminCtx, DbSession, WorkspaceContext
from jhin_api.security.csrf import csrf_protect
from jhin_db.memberships import active_team_ids
from jhin_db.models import Agent, AuditEvent, Conversation, Message, Team
from jhin_db.models.memory_capture import MemoryCapturePolicy
from jhin_domain import WorkspaceRole
from jhin_memory.types import CaptureClass

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/memory-capture-policies",
    tags=["memory"],
    dependencies=[Depends(csrf_protect)],
)


class CapturePolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["team", "workspace", "company"]
    scope_id: UUID
    actor_ids: list[UUID] = Field(min_length=1, max_length=100)
    allowed_source_agent_ids: list[UUID] = Field(default_factory=list, max_length=100)
    allowed_classes: list[CaptureClass] = Field(min_length=1, max_length=4)
    source_conversation_id: UUID | None = None
    source_after_message_id: UUID | None = None
    expires_at: datetime | None = None


class CapturePolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    scope: str
    scope_id: UUID
    granted_by_user_id: UUID
    source_user_id: UUID
    actor_ids_json: list[str]
    allowed_source_agent_ids_json: list[str]
    allowed_classes_json: list[str]
    source_conversation_id: UUID | None
    source_after_message_id: UUID | None
    effective_from: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


def _human_admin(ctx: WorkspaceContext) -> None:
    if ctx.role not in {WorkspaceRole.OWNER, WorkspaceRole.ADMIN} or ctx.api_key is not None:
        raise HTTPException(403, "A signed-in human admin must manage capture authority")


@router.get("")
async def list_capture_policies(ctx: AdminCtx, db: DbSession) -> list[CapturePolicyOut]:
    _human_admin(ctx)
    rows = await db.scalars(
        select(MemoryCapturePolicy)
        .where(MemoryCapturePolicy.workspace_id == ctx.workspace_id)
        .order_by(MemoryCapturePolicy.created_at.desc())
        .limit(200)
    )
    return [CapturePolicyOut.model_validate(row) for row in rows]


@router.post("", status_code=201)
async def create_capture_policy(
    payload: CapturePolicyCreate,
    ctx: AdminCtx,
    db: DbSession,
) -> CapturePolicyOut:
    _human_admin(ctx)
    now = datetime.now(UTC)
    scope = "workspace" if payload.scope == "company" else payload.scope
    if scope == "workspace" and payload.scope_id != ctx.workspace_id:
        raise HTTPException(422, "Company destination must be this workspace")
    if (
        scope == "team"
        and await db.scalar(
            select(Team.id).where(
                Team.id == payload.scope_id, Team.workspace_id == ctx.workspace_id
            )
        )
        is None
    ):
        raise HTTPException(404, "Team not found")
    for actor_id in {*payload.actor_ids, *payload.allowed_source_agent_ids}:
        if (
            await db.scalar(
                select(Agent.id).where(Agent.id == actor_id, Agent.workspace_id == ctx.workspace_id)
            )
            is None
        ):
            raise HTTPException(404, "Agent not found")
        if scope == "team" and payload.scope_id not in await active_team_ids(
            db, ctx.workspace_id, actor_id
        ):
            raise HTTPException(422, "Every permitted agent must currently belong to the team")
    if payload.source_conversation_id is not None and (
        await db.scalar(
            select(Conversation.id).where(
                Conversation.id == payload.source_conversation_id,
                Conversation.workspace_id == ctx.workspace_id,
                Conversation.created_by_user_id == ctx.user.id,
            )
        )
        is None
    ):
        raise HTTPException(404, "Your source conversation was not found")
    if payload.source_after_message_id is not None:
        if payload.source_conversation_id is None:
            raise HTTPException(422, "A source message boundary requires its conversation")
        boundary = await db.scalar(
            select(Message).where(
                Message.id == payload.source_after_message_id,
                Message.workspace_id == ctx.workspace_id,
                Message.conversation_id == payload.source_conversation_id,
                Message.sender_type == "user",
                Message.sender_id == ctx.user.id,
                Message.visibility == "visible",
            )
        )
        if boundary is None:
            raise HTTPException(404, "Your visible source message was not found")
    if payload.expires_at is not None and (
        payload.expires_at.tzinfo is None or payload.expires_at <= now
    ):
        raise HTTPException(422, "Expiry must be a future timezone-aware timestamp")
    row = MemoryCapturePolicy(
        workspace_id=ctx.workspace_id,
        scope=scope,
        scope_id=payload.scope_id,
        granted_by_user_id=ctx.user.id,
        source_user_id=ctx.user.id,
        actor_ids_json=list(dict.fromkeys(str(value) for value in payload.actor_ids)),
        allowed_source_agent_ids_json=[str(value) for value in payload.allowed_source_agent_ids],
        allowed_classes_json=list(dict.fromkeys(payload.allowed_classes)),
        source_conversation_id=payload.source_conversation_id,
        source_after_message_id=payload.source_after_message_id,
        effective_from=now,
        expires_at=payload.expires_at,
    )
    db.add(row)
    await db.flush()
    db.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type="user",
            actor_id=ctx.user.id,
            action="memory.capture.granted",
            target_type="memory_capture_policy",
            target_id=row.id,
            metadata_json={
                "scope": scope,
                "scope_id": str(row.scope_id),
                "allowed_classes": row.allowed_classes_json,
            },
        )
    )
    await db.commit()
    return CapturePolicyOut.model_validate(row)


@router.post("/{policy_id}/revoke")
async def revoke_capture_policy(
    policy_id: UUID,
    ctx: AdminCtx,
    db: DbSession,
) -> CapturePolicyOut:
    _human_admin(ctx)
    row = await db.scalar(
        select(MemoryCapturePolicy)
        .where(
            MemoryCapturePolicy.id == policy_id,
            MemoryCapturePolicy.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "Capture policy not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.add(
            AuditEvent(
                workspace_id=ctx.workspace_id,
                actor_type="user",
                actor_id=ctx.user.id,
                action="memory.capture.revoked",
                target_type="memory_capture_policy",
                target_id=row.id,
                metadata_json={},
            )
        )
    await db.commit()
    return CapturePolicyOut.model_validate(row)
