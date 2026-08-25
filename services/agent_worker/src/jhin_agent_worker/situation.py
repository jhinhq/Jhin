"""Situational awareness for the reasoning prompt: the clock and the
counterpart.

Two facts every agent needs in every conversation, and which no amount of
per-agent memory should have to supply:

* **What time is it** — ``now`` in the workspace's configured timezone
  (``workspace.default_timezone``), with the day of week spelled out.
* **Who am I talking to** — the human participant of this conversation, or,
  for a delegated/requested child task, the agent that asked for the work.

Both are *shared knowledge*: derived live from workspace rows on every run,
never learned and stored per agent. Durable shared facts have a different
home — the memory subsystem's ``workspace`` scope, which
:func:`jhin_memory.retrieval.authorization_filter` already grants to every
agent in the workspace. This module deliberately does not duplicate that.

Wall-clock reads live here, in *activity* code. Workflow code never calls
into this module, and a Temporal replay never re-runs the composition: the
reasoning activity short-circuits on the recorded manifest/reasoning pair
for an already-executed step, so a replayed step returns its recorded result
without recomposing a prompt. Only a fresh (never-recorded) step composes,
and it is free to read the clock.

Privacy: public identity only. A person contributes their ``display_name``
and their workspace role in plain words; ``user.email`` is never read here,
and no user outside this workspace or outside this conversation is ever
named.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_agents.context import Interlocutor, format_local_time, interlocutor_block, time_block
from jhin_db.models import (
    Agent,
    Conversation,
    Message,
    Task,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import MessageVisibility, SenderType, WorkspaceRole

DEFAULT_TIMEZONE = "UTC"

# Workspace roles in plain words. Anything unrecognized degrades to the
# neutral "workspace member" rather than leaking a raw enum value.
_ROLE_WORDS = {
    WorkspaceRole.OWNER.value: "workspace owner",
    WorkspaceRole.ADMIN.value: "workspace admin",
    WorkspaceRole.MEMBER.value: "workspace member",
    WorkspaceRole.VIEWER.value: "workspace viewer",
}

__all__ = [
    "DEFAULT_TIMEZONE",
    "format_local_time",
    "resolve_interlocutors",
    "resolve_timezone",
    "situation_context",
]


def resolve_timezone(name: str | None) -> ZoneInfo:
    """The workspace timezone, falling back to UTC.

    An unset or unknown zone name must never fail a run: a wrong-looking
    clock is bad, no clock at all is worse.
    """
    if not name or not name.strip():
        return ZoneInfo(DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


async def _workspace_timezone(session: AsyncSession, workspace_id: UUID) -> str:
    name = await session.scalar(
        select(Workspace.default_timezone).where(Workspace.id == workspace_id)
    )
    return name or DEFAULT_TIMEZONE


def _role_words(role: str | None) -> str:
    return _ROLE_WORDS.get(role or "", "workspace member")


async def _human_interlocutor(
    session: AsyncSession, *, workspace_id: UUID, user_id: UUID, relation: str = ""
) -> Interlocutor | None:
    """Public identity for one workspace member, or None when they are not
    (or are no longer) a member of this workspace."""
    row = (
        await session.execute(
            select(User.display_name, WorkspaceMembership.role)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                User.id == user_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
        )
    ).first()
    if row is None:
        return None
    display_name, role = row
    # Deliberately no email fallback: if a person has no display name we
    # would rather say nothing than put their address in a prompt.
    if not (display_name or "").strip():
        return None
    return Interlocutor(
        display_name=display_name,
        kind="human",
        role=_role_words(role),
        relation=relation,
    )


async def _requesting_agent(
    session: AsyncSession, *, workspace_id: UUID, task: Task
) -> Interlocutor | None:
    """The agent counterpart for a delegated or requested child task.

    Delegation (``organization.delegate_task`` / engineering tickets) and
    work requests both stamp the requester into ``task.metadata_json``; the
    live agent row supplies the role title so the wording stays current.
    """
    metadata = task.metadata_json or {}
    for key, id_field, name_field, relation in (
        (
            "delegation",
            "delegated_by_agent_id",
            "delegated_by_agent_name",
            "who delegated this task to you and is waiting for your result",
        ),
        (
            "work_request",
            "requester_agent_id",
            "requester_agent_name",
            "who asked you for this work and is waiting for your result",
        ),
    ):
        block = metadata.get(key)
        if not isinstance(block, dict):
            continue
        name = str(block.get(name_field, "") or "")
        role_title = ""
        raw_id = str(block.get(id_field, "") or "")
        if raw_id:
            try:
                agent_id = UUID(raw_id)
            except ValueError:
                agent_id = None
            if agent_id is not None:
                found = (
                    await session.execute(
                        select(Agent.name, Agent.role_title).where(
                            Agent.id == agent_id, Agent.workspace_id == workspace_id
                        )
                    )
                ).first()
                if found is not None:
                    name = found[0] or name
                    role_title = found[1] or ""
        if not name.strip():
            continue
        return Interlocutor(
            display_name=name,
            kind="agent",
            role=role_title,
            relation=relation,
        )
    return None


async def _conversation_human(
    session: AsyncSession, *, workspace_id: UUID, task: Task
) -> Interlocutor | None:
    """The person on the other side of this chat.

    Preferred source is the most recent *person-sent* message on this task —
    in a shared workspace the person who spoke last is the one being
    answered, which is not necessarily whoever opened the thread. The
    conversation's creator is the fallback for the very first turn, whose
    seed message may not be committed yet when the first step composes.
    """
    sender_id = await session.scalar(
        select(Message.sender_id)
        .where(
            Message.workspace_id == workspace_id,
            Message.task_id == task.id,
            Message.sender_type == SenderType.USER.value,
            Message.sender_id.is_not(None),
            Message.visibility == MessageVisibility.VISIBLE.value,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if sender_id is None and task.conversation_id is not None:
        sender_id = await session.scalar(
            select(Conversation.created_by_user_id).where(
                Conversation.id == task.conversation_id,
                Conversation.workspace_id == workspace_id,
            )
        )
    if sender_id is None:
        return None
    return await _human_interlocutor(session, workspace_id=workspace_id, user_id=sender_id)


async def resolve_interlocutors(
    session: AsyncSession, *, workspace_id: UUID, task: Task
) -> tuple[Interlocutor, ...]:
    """Who this agent is talking with on this task.

    An agent counterpart wins over a human one: a delegated child task is a
    conversation with the requesting agent even when a person started the
    parent thread. A trigger- or schedule-started task has no counterpart at
    all and resolves to ``()``, which renders no block.
    """
    requester = await _requesting_agent(session, workspace_id=workspace_id, task=task)
    if requester is not None:
        return (requester,)
    human = await _conversation_human(session, workspace_id=workspace_id, task=task)
    return (human,) if human is not None else ()


async def situation_context(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    task: Task,
    now: datetime | None = None,
) -> tuple[str, str]:
    """``(time_context, interlocutor_context)`` for one reasoning step.

    ``now`` is injectable so tests (and any future replay-pinned caller) can
    fix the clock; production passes None and reads it here, inside the
    activity.
    """
    moment = now or datetime.now(UTC)
    timezone_name = await _workspace_timezone(session, workspace_id)
    zone = resolve_timezone(timezone_name)
    local = moment.astimezone(zone)
    interlocutors = await resolve_interlocutors(session, workspace_id=workspace_id, task=task)
    return time_block(local, str(zone.key)), interlocutor_block(interlocutors)
