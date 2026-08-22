"""Routes for conversations, the company activity feed, and attention.

/api/v1/workspaces/{workspace_id}/conversations   list/create/detail/update/delete,
                                                  messages, turns, activity
/api/v1/workspaces/{workspace_id}/activity        company-wide activity feed
/api/v1/workspaces/{workspace_id}/attention       what needs the user now
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from nats.js import JetStreamContext

from jhin_api.conversations import service
from jhin_api.conversations.schemas import (
    ActivityListOut,
    AttentionOut,
    ConversationCreate,
    ConversationDetailOut,
    ConversationListOut,
    ConversationMessageOut,
    ConversationOut,
    ConversationUpdate,
    TurnIn,
    TurnOut,
)
from jhin_api.deps import (
    AdminCtx,
    DbSession,
    MemberCtx,
    ObservabilityRuntimeDep,
    TemporalDep,
    ViewerCtx,
    get_jetstream,
)
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_events import EventPublisher

conversations_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/conversations",
    tags=["conversations"],
    dependencies=[Depends(csrf_protect)],
)
workspace_feed_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["activity"],
    dependencies=[Depends(csrf_protect)],
)


async def get_optional_publisher(
    request: Request, runtime: ObservabilityRuntimeDep
) -> EventPublisher | None:
    """Event publishing is best-effort here: the database already holds the
    fact, so an unreachable backbone must never fail a chat turn."""
    try:
        js: JetStreamContext = await get_jetstream(request)
    except HTTPException:
        return None
    return EventPublisher(js, tracer=runtime.tracer)


PublisherDep = Annotated[EventPublisher | None, Depends(get_optional_publisher)]


async def _turn_out(db: DbSession, workspace_id: UUID, turn: service.TurnResult) -> TurnOut:
    return TurnOut(
        conversation=await service.project_conversation(db, workspace_id, turn.conversation),
        message=(await service.project_messages(db, workspace_id, [turn.message]))[0],
        task_id=turn.task.id,
        mode=turn.mode,
    )


@conversations_router.get("")
async def list_conversations(
    ctx: ViewerCtx,
    db: DbSession,
    q: str | None = None,
    agent_id: UUID | None = None,
    status: str | None = None,
    pinned: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ConversationListOut:
    if status is not None and status not in ("active", "archived"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'archived'")
    items, total = await service.list_conversations(
        db,
        ctx.workspace_id,
        q=q,
        agent_id=agent_id,
        status_filter=status,
        pinned=pinned,
        limit=limit,
        offset=offset,
    )
    return ConversationListOut(
        items=await service.project_conversations(db, ctx.workspace_id, items), total=total
    )


@conversations_router.post("", status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
    publisher: PublisherDep,
) -> ConversationDetailOut:
    conversation, _turn = await service.create_conversation(
        db,
        ctx,
        temporal,
        agent_id=payload.agent_id,
        title=payload.title,
        text=payload.text,
        client_turn_id=payload.client_turn_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        publisher=publisher,
    )
    return await service.get_detail(db, ctx.workspace_id, conversation.id)


@conversations_router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: UUID, ctx: ViewerCtx, db: DbSession
) -> ConversationDetailOut:
    return await service.get_detail(db, ctx.workspace_id, conversation_id)


@conversations_router.patch("/{conversation_id}")
async def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
) -> ConversationOut:
    conversation = await service.update_conversation(
        db,
        ctx,
        conversation_id,
        values=payload.model_dump(exclude_unset=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await service.project_conversation(db, ctx.workspace_id, conversation)


@conversations_router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> Response:
    await service.delete_conversation(
        db, ctx, conversation_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@conversations_router.get("/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: UUID, ctx: ViewerCtx, db: DbSession, after: UUID | None = None
) -> list[ConversationMessageOut]:
    messages = await service.list_messages(db, ctx.workspace_id, conversation_id, after=after)
    return await service.project_messages(db, ctx.workspace_id, messages)


@conversations_router.post("/{conversation_id}/turns")
async def send_turn(
    conversation_id: UUID,
    payload: TurnIn,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
    publisher: PublisherDep,
) -> TurnOut:
    turn = await service.send_turn(
        db,
        ctx,
        temporal,
        conversation_id,
        text=payload.text,
        client_turn_id=payload.client_turn_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        publisher=publisher,
    )
    return await _turn_out(db, ctx.workspace_id, turn)


@conversations_router.get("/{conversation_id}/activity")
async def conversation_activity(
    conversation_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    kinds: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> ActivityListOut:
    return await service.list_activity(
        db,
        ctx.workspace_id,
        conversation_id=conversation_id,
        kinds=service.parse_kinds(kinds),
        before=before,
        limit=limit,
    )


@workspace_feed_router.get("/activity")
async def workspace_activity(
    ctx: ViewerCtx,
    db: DbSession,
    agent_id: UUID | None = None,
    team_id: UUID | None = None,
    conversation_id: UUID | None = None,
    kinds: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> ActivityListOut:
    return await service.list_activity(
        db,
        ctx.workspace_id,
        agent_id=agent_id,
        team_id=team_id,
        conversation_id=conversation_id,
        kinds=service.parse_kinds(kinds),
        before=before,
        limit=limit,
    )


@workspace_feed_router.get("/attention")
async def workspace_attention(ctx: ViewerCtx, db: DbSession) -> AttentionOut:
    return await service.attention(db, ctx.workspace_id)
