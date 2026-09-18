"""Public, resumable projections of the transactionally committed journal."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.public_payloads import public_tool_payload
from jhin_db.models import Agent, Conversation, User
from jhin_db.models.timeline import ConversationEvent
from jhin_observability.workspace_metrics import workspace_metrics
from jhin_secrets.intake import redact_legacy_payload
from jhin_tools.sanitize import sanitize_payload

_FIELDS = {
    "message": (
        "id conversation_id task_id run_id sender_type sender_id recipient_type "
        "recipient_id message_type content_json visibility created_at"
    ),
    "tool_call": (
        "id task_id run_id agent_id tool_name connection_id sanitized_input_json "
        "sanitized_output_json status approval_id review_id started_at completed_at "
        "duration_ms error_code created_at sandbox_job"
    ),
    "task": (
        "id title description state assigned_agent_id parent_task_id metadata_json "
        "created_at updated_at"
    ),
    "approval": (
        "id task_id run_id requested_by_agent_id action_type action_payload_sanitized "
        "reason status requested_at decided_at decided_by_user_id"
    ),
    "user_question": (
        "id task_id run_id agent_id question context options_json allow_other required input_key "
        "value_type status answer_text asked_at answered_at"
    ),
    "work_request": (
        "id requester_task_id requester_agent_id target_agent_id title description status "
        "created_task_id response created_at completed_at"
    ),
    "work_review": (
        "id task_id run_id work_request_id subject_agent_id reviewer_type reviewer_agent_id "
        "reviewer_user_id status verdict feedback requested_at decided_at "
        "decided_by_agent_id decided_by_user_id created_at"
    ),
    "generation": (
        "id task_id run_id agent_id text status model step metadata_json created_at completed_at"
    ),
    "managed_file": (
        "id name path kind status error current_revision_id mime_type size_bytes created_at"
        " updated_at"
    ),
    "runtime_session": "id user_id kind status network error created_at updated_at",
}
_KINDS = {
    "tool_call": "action",
    "user_question": "question",
    "work_request": "delegation",
    "work_review": "approval",
    "managed_file": "file",
    "runtime_session": "action",
}


class ItemActor(BaseModel):
    type: str = "system"
    id: UUID | None = None
    name: str | None = None


class ConversationItem(BaseModel):
    id: str
    version: Literal[1] = 1
    sequence: int
    revision: int
    kind: str
    status: str
    actor: ItemActor
    task_id: UUID | None = None
    run_id: UUID | None = None
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class ConversationItemsOut(BaseModel):
    version: Literal[1] = 1
    items: list[ConversationItem]
    cursor: int
    next_before: int | None = None
    has_more: bool = False


def public_data(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in _FIELDS:
        return None
    if kind == "message" and payload.get("visibility") != "visible":
        return None
    fields = _FIELDS[kind].split()
    result = redact_legacy_payload({key: value for key, value in payload.items() if key in fields})
    if kind == "tool_call":
        for key in ("sanitized_input_json", "sanitized_output_json"):
            if isinstance(result.get(key), dict):
                result[key] = public_tool_payload(str(result.get("tool_name") or ""), result[key])
    if kind == "approval" and isinstance(result.get("action_payload_sanitized"), dict):
        result["action_payload_sanitized"] = public_tool_payload(
            str(result.get("action_type") or ""), result["action_payload_sanitized"]
        )
    if kind == "task":
        metadata = result.get("metadata_json") or {}
        result["metadata_json"] = {
            key: metadata[key]
            for key in ("execution_mode", "delivery", "queue", "instruction_receipts")
            if key in metadata
        }
    if kind == "runtime_session":
        config = payload.get("config_json") or {}
        result["command"] = redact_legacy_payload(config.get("command", ""))
    return sanitize_payload(result, max_string_chars=65_536, max_document_bytes=131_072)


def project_event(event: ConversationEvent) -> ConversationItem:
    data = public_data(event.source_kind, event.payload_json)
    hidden = data is None or event.operation == "delete"
    data = {} if hidden else data
    assert data is not None
    identity = data
    if (
        hidden
        and event.source_kind == "message"
        and event.payload_json.get("sender_type") == "agent"
        and event.payload_json.get("message_type") == "text"
    ):
        # Withdrawing a saved reply must also withdraw its earlier public
        # generation, including after a fresh snapshot. Keep only structural
        # identity; no removed message text or provider payload survives.
        identity = {
            key: event.payload_json.get(key)
            for key in ("sender_type", "sender_id", "task_id", "run_id", "created_at")
        }
        data = {"message_type": "text"}
    agent = (
        identity.get("agent_id")
        or identity.get("requested_by_agent_id")
        or identity.get("assigned_agent_id")
    )
    actor_type = identity.get("sender_type") or (
        "user" if identity.get("user_id") else ("agent" if agent else "system")
    )
    actor_id = identity.get("sender_id") or identity.get("user_id") or agent
    if event.source_kind == "work_review":
        deciding_user = data.get("decided_by_user_id")
        reviewing_agent = data.get("decided_by_agent_id") or data.get("subject_agent_id")
        actor_type = "user" if deciding_user else ("agent" if reviewing_agent else "system")
        actor_id = deciding_user or reviewing_agent
    return ConversationItem(
        id=f"{event.source_kind}:{event.source_id}",
        sequence=event.sequence,
        revision=event.sequence,
        kind=_KINDS.get(event.source_kind, event.source_kind),
        status="removed" if hidden else str(data.get("status") or data.get("state") or "completed"),
        actor=ItemActor(type=actor_type, id=actor_id),
        task_id=identity.get("task_id"),
        run_id=identity.get("run_id"),
        created_at=identity.get("created_at")
        or identity.get("requested_at")
        or identity.get("asked_at")
        or event.created_at,
        data=data,
    )


async def require_conversation(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID
) -> Conversation:
    chat = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if chat is None:
        raise HTTPException(404, "Conversation not found")
    return chat


async def enrich_actors(
    db: AsyncSession, workspace_id: UUID, items: list[ConversationItem]
) -> list[ConversationItem]:
    agent_ids = {item.actor.id for item in items if item.actor.type == "agent" and item.actor.id}
    user_ids = {item.actor.id for item in items if item.actor.type == "user" and item.actor.id}
    agents: dict[UUID, str] = {}
    users: dict[UUID, str] = {}
    if agent_ids:
        result = await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == workspace_id, Agent.id.in_(agent_ids)
            )
        )
        agents = dict(result.tuples().all())
    if user_ids:
        result = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))
        users = dict(result.tuples().all())
    for item in items:
        item.actor.name = (
            (agents.get(item.actor.id) if item.actor.type == "agent" else users.get(item.actor.id))
            if item.actor.id is not None
            else None
        )
        if item.actor.type == "system":
            item.actor.name = "System"
        if item.kind == "message":
            item.data["sender_name"] = item.actor.name
            item.data["agent_id"] = (
                str(item.actor.id) if item.actor.type == "agent" and item.actor.id else None
            )
        elif item.kind == "action" and item.actor.type == "agent":
            item.data["agent_name"] = item.actor.name
    return items


async def snapshot(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    *,
    before: int | None = None,
    limit: int = 50,
    item_ids: list[str] | None = None,
) -> ConversationItemsOut:
    await require_conversation(db, workspace_id, conversation_id)
    limit = max(1, min(100, limit))
    scope = (
        ConversationEvent.workspace_id == workspace_id,
        ConversationEvent.conversation_id == conversation_id,
    )
    cursor = int(await db.scalar(select(func.max(ConversationEvent.sequence)).where(*scope)) or 0)
    selection: tuple[ColumnElement[bool], ...] = scope
    if item_ids:
        if len(item_ids) > 100:
            raise HTTPException(422, "At most 100 loaded item IDs may be refreshed at once")
        identities: set[tuple[str, UUID]] = set()
        for identity in item_ids:
            kind, separator, source_id = identity.partition(":")
            try:
                if not separator or len(kind) > 48 or not kind.replace("_", "").isalpha():
                    raise ValueError
                identities.add((kind, UUID(source_id)))
            except ValueError:
                raise HTTPException(422, "Item IDs must use the stable kind:UUID format") from None
        selection = (
            *scope,
            tuple_(ConversationEvent.source_kind, ConversationEvent.source_id).in_(identities),
        )
        limit = 100
    latest = (
        select(
            func.max(ConversationEvent.sequence).label("sequence"),
            func.min(ConversationEvent.sequence).label("position"),
        )
        .where(*selection, ConversationEvent.sequence <= cursor)
        .group_by(ConversationEvent.source_kind, ConversationEvent.source_id)
        .subquery()
    )
    query = (
        select(ConversationEvent, latest.c.position)
        .join(latest, latest.c.sequence == ConversationEvent.sequence)
        .where(*scope)
    )
    if before is not None and not item_ids:
        query = query.where(latest.c.position < before)
    rows = list(await db.execute(query.order_by(latest.c.position.desc()).limit(limit + 1)))
    page = rows[:limit]
    return ConversationItemsOut(
        items=await enrich_actors(
            db, workspace_id, [project_event(row[0]) for row in reversed(page)]
        ),
        cursor=cursor,
        next_before=page[-1][1] if len(rows) > limit and not item_ids else None,
        has_more=len(rows) > limit and not item_ids,
    )


async def replay(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID, *, after: int, limit: int = 200
) -> list[ConversationItem]:
    await require_conversation(db, workspace_id, conversation_id)
    rows = await db.scalars(
        select(ConversationEvent)
        .where(
            ConversationEvent.workspace_id == workspace_id,
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.sequence > after,
        )
        .order_by(ConversationEvent.sequence)
        .limit(min(500, max(1, limit)))
    )
    items = []
    for row in rows:
        recorded = row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC)
        workspace_metrics().histogram("conversation_event_delivery_seconds").record(
            max(0, (datetime.now(UTC) - recorded).total_seconds())
        )
        items.append(project_event(row))
    return await enrich_actors(db, workspace_id, items)
