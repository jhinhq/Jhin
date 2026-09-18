"""Routes for conversations, the company activity feed, and attention.

/api/v1/workspaces/{workspace_id}/conversations   list/create/detail/update/delete,
                                                  messages, turns, activity
/api/v1/workspaces/{workspace_id}/activity        company-wide activity feed
/api/v1/workspaces/{workspace_id}/attention       what needs the user now
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_api.conversations import service, timeline
from jhin_api.conversations.schemas import (
    AcknowledgeFailuresOut,
    ActivityListOut,
    AttentionOut,
    ConversationBranchIn,
    ConversationControlIn,
    ConversationCreate,
    ConversationDetailOut,
    ConversationListOut,
    ConversationMessageOut,
    ConversationOut,
    ConversationToolCallListOut,
    ConversationUpdate,
    QueuedTurnUpdate,
    ResumeOut,
    TurnIn,
    TurnOut,
)
from jhin_api.deps import (
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
from jhin_observability.workspace_metrics import workspace_metrics

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
        project_id=payload.project_id,
        execution_mode=payload.execution_mode,
        model_profile_id=payload.model_profile_id,
        crypto=getattr(request.app.state, "secret_crypto", None),
        secure_inputs=[entry.transient_value() for entry in payload.secure_inputs],
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
    conversation_id: UUID, request: Request, ctx: MemberCtx, db: DbSession
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


@conversations_router.get("/{conversation_id}/tool-calls")
async def conversation_tool_calls(
    conversation_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    before: UUID | None = None,
    limit: int = 100,
) -> ConversationToolCallListOut:
    return await service.list_tool_calls(
        db, ctx.workspace_id, conversation_id, before=before, limit=limit
    )


@conversations_router.get("/{conversation_id}/items")
async def conversation_items(
    conversation_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    before: int | None = None,
    limit: int = 50,
    item_id: Annotated[list[str] | None, Query(max_length=100)] = None,
) -> timeline.ConversationItemsOut:
    return await timeline.snapshot(
        db, ctx.workspace_id, conversation_id, before=before, limit=limit, item_ids=item_id
    )


@conversations_router.get("/{conversation_id}/events")
async def conversation_events(
    conversation_id: UUID, request: Request, ctx: ViewerCtx, db: DbSession, after: int = 0
) -> StreamingResponse:
    await timeline.require_conversation(db, ctx.workspace_id, conversation_id)
    try:
        cursor = int(request.headers.get("last-event-id") or after)
        if cursor < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Event cursor must be a non-negative integer") from None
    # Each replay batch owns a short transaction; never retain a connection
    # for the lifetime of an idle browser stream. Re-authorize periodically
    # by reconnecting rather than allowing a revoked cookie to live forever.
    await db.rollback()
    factory = request.app.state.session_factory

    async def events() -> AsyncIterator[str]:
        nonlocal cursor
        wake = asyncio.Event()
        subscription = None
        client = getattr(request.app.state, "nats_client", None)

        async def on_wake(_message: Any) -> None:
            wake.set()

        if client is not None and not client.is_closed:
            with contextlib.suppress(Exception):
                subscription = await client.subscribe(f"jhin.v1.{ctx.workspace_id}.>", cb=on_wake)
        try:
            async for frame in _journal_frames(
                request, factory, ctx.workspace_id, conversation_id, cursor, wake
            ):
                yield frame
        finally:
            if subscription is not None:
                with contextlib.suppress(Exception):
                    await subscription.unsubscribe()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _journal_frames(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    conversation_id: UUID,
    cursor: int,
    wake: asyncio.Event,
) -> AsyncIterator[str]:
    started = heartbeat = time.monotonic()
    recovering = cursor > 0
    metrics = workspace_metrics()
    metrics.counter("conversation_reconnects_total").add(1, outcome="started")
    while time.monotonic() - started < 60:
        if await request.is_disconnected():
            return
        wake.clear()
        async with factory() as session:
            rows = await timeline.replay(session, workspace_id, conversation_id, after=cursor)
            if not rows:
                from sqlalchemy import select

                from jhin_db.models import Conversation

                high = await session.scalar(
                    select(Conversation.timeline_sequence).where(
                        Conversation.id == conversation_id,
                        Conversation.workspace_id == workspace_id,
                    )
                )
                if high is None or cursor > high:
                    metrics.counter("conversation_reconnects_total").add(1, outcome="rejected")
                    yield "event: snapshot_required\ndata: {}\n\n"
                    return
        if rows and rows[0].sequence != cursor + 1:
            metrics.counter("conversation_reconnects_total").add(1, outcome="rejected")
            yield "event: snapshot_required\ndata: {}\n\n"
            return
        for item in rows:
            cursor = item.sequence
            yield f"id: {cursor}\nevent: item\ndata: {item.model_dump_json()}\n\n"
        if rows:
            continue
        if recovering:
            metrics.histogram("conversation_recovery_seconds").record(
                time.monotonic() - started, outcome="completed"
            )
            metrics.counter("conversation_reconnects_total").add(1, outcome="completed")
            recovering = False
        if time.monotonic() - heartbeat >= 15:
            heartbeat = time.monotonic()
            yield ": heartbeat\n\n"
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=1)


@conversations_router.get("/{conversation_id}/tool-calls/{tool_call_id}/logs")
async def conversation_tool_logs(
    conversation_id: UUID, tool_call_id: UUID, ctx: ViewerCtx, db: DbSession
) -> Response:
    item = await service.get_tool_call(db, ctx.workspace_id, conversation_id, tool_call_id)
    job = item.sandbox_job
    if job is None:
        body = json.dumps(item.sanitized_output_json, indent=2, ensure_ascii=False)
    else:
        body = (
            "Retained output tails (at most 8192 characters per stream).\n\nSTDOUT\n"
            + job.stdout
            + "\nSTDERR\n"
            + job.stderr
        )
    return Response(
        body,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="tool-{tool_call_id}.log"',
            "Cache-Control": "no-store",
        },
    )


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
        execution_mode=payload.execution_mode,
        delivery=payload.delivery,
        model_profile_id=payload.model_profile_id,
        attachment_ids=payload.attachment_ids,
        context_refs=payload.context_refs,
        crypto=getattr(request.app.state, "secret_crypto", None),
        secure_inputs=[entry.transient_value() for entry in payload.secure_inputs],
    )
    return await _turn_out(db, ctx.workspace_id, turn)


@conversations_router.post("/{conversation_id}/control")
async def control_conversation(
    conversation_id: UUID,
    payload: ConversationControlIn,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> dict[str, Any]:
    return await service.control(
        db,
        ctx,
        temporal,
        conversation_id,
        action=payload.action,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@conversations_router.post("/{conversation_id}/branches", status_code=201)
async def branch_conversation(
    conversation_id: UUID, payload: ConversationBranchIn, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    return await service.branch(
        db,
        ctx,
        conversation_id,
        message_id=payload.message_id,
        checkpoint_id=payload.checkpoint_id,
        title=payload.title,
    )


@conversations_router.patch("/{conversation_id}/queued/{task_id}")
async def edit_queued_turn(
    conversation_id: UUID,
    task_id: UUID,
    payload: QueuedTurnUpdate,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
) -> dict[str, Any]:
    return await service.change_queued(
        db,
        ctx,
        conversation_id,
        task_id,
        text=payload.text,
        crypto=getattr(request.app.state, "secret_crypto", None),
        secure_inputs=[entry.transient_value() for entry in payload.secure_inputs],
    )


@conversations_router.delete("/{conversation_id}/queued/{task_id}")
async def remove_queued_turn(
    conversation_id: UUID, task_id: UUID, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    return await service.change_queued(db, ctx, conversation_id, task_id, text=None)


@conversations_router.post("/{conversation_id}/resume")
async def resume_conversation(
    conversation_id: UUID,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
    publisher: PublisherDep,
) -> ResumeOut:
    """Pick the conversation's failed turn back up, without retyping it.

    Safe to press twice: a second call while the first is still working
    answers with that same task and ``created: false`` rather than starting a
    parallel one. It refuses (409) when the last turn did not fail, when a
    call from that turn was never accounted for and repeating it could repeat
    whatever it did, and when the chat or the agent cannot take work.
    """
    result = await service.resume_conversation(
        db,
        ctx,
        temporal,
        conversation_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        publisher=publisher,
        crypto=getattr(request.app.state, "secret_crypto", None),
    )
    return ResumeOut(
        conversation=await service.project_conversation(db, ctx.workspace_id, result.conversation),
        task_id=result.task.id,
        resumed_task_id=result.resumed_task.id,
        created=result.created,
    )


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


@workspace_feed_router.post("/attention/acknowledge-failures")
async def acknowledge_attention_failures(
    request: Request, ctx: MemberCtx, db: DbSession
) -> AcknowledgeFailuresOut:
    """Dismiss every failed task the inbox currently lists."""
    return await service.acknowledge_failures(
        db, ctx, request_id=req_id(request), ip_hash=ip_hash(request)
    )
