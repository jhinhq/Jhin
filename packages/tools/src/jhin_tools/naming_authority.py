"""Who may confer a name, decided from the run's own rows.

A name is conferred by a *person*. Making ``organization.identity.set_name``
self-only removed "rename a colleague"; it did not remove "order a colleague
to rename itself", which reaches the same outcome by one more hop. Both live
runs that found this used that hop: a CTO agent delegated the rename, the
target called the tool on its first step without asking anyone, and the audit
row named the human at the top of the thread — a person who never asked for
it. The second run said so in its own words ("Direct request from me, your
CTO (not from a human)… CTO-issued housekeeping") and the rename still
happened, because the only thing standing in the way was the model's
judgement. A model declining is not a control.

So this module answers one question before the executor writes anything:
**is there a person on the other side of this run, and which person is it?**
It is deliberately the same question, read from the same rows, that
``interlocutor_block`` renders into the prompt (see
``services/agent_worker/.../situation.py``) — with one addition the prompt has
no use for: a human who *approved this exact call* is also a person
conferring the name.

The boundary, and why each side of it falls where it does:

======================  ================================================
run shape               may a name be conferred?
======================  ================================================
chat turn with a        **yes** — the person is the counterpart, and the
person                  audit names whoever actually spoke last, not
                        whoever opened the thread.
a task where a person   **yes** — same rule, same evidence: a visible
has spoken              message this workspace's member sent.
delegated child task    **no** — the counterpart is the delegating
                        agent. "Rename yourself" from a colleague is the
                        renaming of a colleague, one hop removed.
agent work request      **no** — same, and the sharper case: a work
                        request child carries the *requester's*
                        ``conversation_id``, so the person in that thread
                        is reachable from this task and is exactly the
                        person the audit must not credit.
trigger / schedule /    **no** — nobody is there. A name conferred with
assigned task with      no one in the room cannot be undone by the person
nobody speaking         who did not know it happened.
any of the above, where **yes** — an approval is a person deciding this
a human approved this   call, in words that named it. Without this a
tool call               workspace on a restrictive policy could park the
                        rename on a person and then have the executor
                        refuse what they just approved.
======================  ================================================

An agent counterpart **wins over** a human one, exactly as it does for the
prompt block: a delegated child whose parent is somebody's chat is a
conversation with the delegating agent, not with that person.

The result carries the delegation chain when there is one, so the audit row
records who actually asked all the way up rather than crediting the person a
chain happens to hang from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import (
    Approval,
    Conversation,
    Message,
    Task,
    ToolCall,
    User,
    WorkspaceMembership,
)
from jhin_domain import ApprovalStatus, MessageVisibility, SenderType

#: How far up the parent chain the requester walk goes. A chain longer than
#: this is already past every delegation-depth guard; the bound is here so a
#: cycle in ``parent_task_id`` cannot spin the executor.
MAX_CHAIN_DEPTH = 8

#: Where the ask came from. The first two are people; the rest are not.
Origin = Literal["chat", "approval", "delegation", "work_request", "unattended"]

# The task-metadata blocks that mean "a colleague asked for this work", and
# the fields they carry the requester in. Written by
# ``organization.delegate_task`` / the engineering-ticket activities and by
# ``organization.request_work``; read here, never trusted for authority — the
# presence of either block is what refuses the rename.
_REQUESTER_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("delegation", "delegated_by_agent_id", "delegated_by_agent_name"),
    ("work_request", "requester_agent_id", "requester_agent_name"),
)


@dataclass(frozen=True)
class NameGiver:
    """The person who may confer a name on this run, or the reason there is
    none.

    ``user_id is None`` is the refusal: ``origin`` then says which shape of
    run it was, and ``chain`` names the colleagues that asked, nearest first.
    """

    origin: Origin
    user_id: UUID | None = None
    display_name: str = ""
    chain: tuple[dict[str, str], ...] = ()

    @property
    def is_a_person(self) -> bool:
        return self.user_id is not None

    @property
    def asked_by_a_colleague(self) -> str:
        """The nearest colleague that asked for this work, or ``""``."""
        return self.chain[0]["agent_name"] if self.chain else ""


def _chain_entry(task: Task) -> dict[str, str] | None:
    """The colleague that asked for *this* task, or None if nobody did."""
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    for key, id_field, name_field in _REQUESTER_BLOCKS:
        block = metadata.get(key)
        if not isinstance(block, dict):
            continue
        return {
            "via": key,
            "agent_id": str(block.get(id_field, "") or ""),
            "agent_name": str(block.get(name_field, "") or ""),
            "task_id": str(task.id),
        }
    return None


async def _requester_chain(
    session: AsyncSession, *, workspace_id: UUID, task: Task
) -> tuple[dict[str, str], ...]:
    """Every colleague that asked for this work, nearest first.

    The walk goes up ``parent_task_id`` rather than reading one block,
    because the ask that matters may be two tasks away: a CTO delegates to a
    lead, the lead delegates onward, and the audit row for anything the last
    agent does has to be able to say so.
    """
    chain: list[dict[str, str]] = []
    seen: set[UUID] = set()
    current: Task | None = task
    while current is not None and len(chain) < MAX_CHAIN_DEPTH:
        if current.id in seen:
            break
        seen.add(current.id)
        entry = _chain_entry(current)
        if entry is not None:
            chain.append(entry)
        if current.parent_task_id is None:
            break
        current = await session.scalar(
            select(Task).where(Task.id == current.parent_task_id, Task.workspace_id == workspace_id)
        )
    return tuple(chain)


async def _member(
    session: AsyncSession, *, workspace_id: UUID, user_id: UUID
) -> tuple[UUID, str] | None:
    """A person of *this* workspace, and their display name.

    Membership is checked, not assumed: the row that named them (a message,
    a conversation) can outlive their access, and someone who has left is no
    longer a person of this workspace who can be asked about it afterwards.
    An empty display name is fine — the id is the attribution, the name is
    only how the memory reads.
    """
    row = (
        await session.execute(
            select(User.id, User.display_name)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(User.id == user_id, WorkspaceMembership.workspace_id == workspace_id)
        )
    ).first()
    if row is None:
        return None
    return row[0], (row[1] or "")


async def _person_in_the_room(
    session: AsyncSession, *, workspace_id: UUID, task: Task
) -> tuple[UUID, str] | None:
    """Whoever this run is answering.

    The most recent *person-sent* visible message on this task first — in a
    shared thread the person who spoke last is the one being answered, and
    they are the one the audit must name. The conversation's creator is the
    fallback for a first turn whose seed message is not committed yet. The
    same two sources, in the same order, as the "Who you are talking with"
    block.
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
    return await _member(session, workspace_id=workspace_id, user_id=sender_id)


async def _approver(
    session: AsyncSession, *, workspace_id: UUID, tool_call_id: UUID | None
) -> tuple[UUID, str] | None:
    """The person who approved *this* call, if it went through an approval.

    Read from the approval row rather than from anything the run carries: the
    row is the authority on the decision, and it is the only place a person's
    "yes, rename it" is recorded.
    """
    if tool_call_id is None:
        return None
    approval_id = await session.scalar(
        select(ToolCall.approval_id).where(
            ToolCall.id == tool_call_id, ToolCall.workspace_id == workspace_id
        )
    )
    if approval_id is None:
        return None
    row = (
        await session.execute(
            select(Approval.decided_by_user_id, Approval.status).where(
                Approval.id == approval_id, Approval.workspace_id == workspace_id
            )
        )
    ).first()
    if row is None:
        return None
    decided_by, status = row
    if decided_by is None or status != ApprovalStatus.APPROVED.value:
        return None
    return await _member(session, workspace_id=workspace_id, user_id=decided_by)


async def resolve_name_giver(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    task_id: UUID,
    tool_call_id: UUID | None = None,
) -> NameGiver:
    """The person conferring a name on this run, or why there is none.

    Order matters and is the whole control:

    1. a colleague asking makes this an agent-to-agent run, whatever else is
       reachable from it — that is the hop the self-only input did not close;
    2. otherwise the person this run is answering;
    3. otherwise the person who approved this call;
    4. otherwise nobody.
    """
    task = await session.scalar(
        select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
    )
    if task is None:
        # No task row is not "nobody asked" — it is "this run cannot be
        # accounted for", which is the same refusal for a stronger reason.
        return NameGiver(origin="unattended")

    chain = await _requester_chain(session, workspace_id=workspace_id, task=task)
    asked_by_colleague = _chain_entry(task)

    person: tuple[UUID, str] | None = None
    origin: Origin
    if asked_by_colleague is not None:
        origin = "work_request" if asked_by_colleague["via"] == "work_request" else "delegation"
    else:
        person = await _person_in_the_room(session, workspace_id=workspace_id, task=task)
        origin = "chat" if person is not None else "unattended"

    if person is None:
        approver = await _approver(session, workspace_id=workspace_id, tool_call_id=tool_call_id)
        if approver is not None:
            person, origin = approver, "approval"

    if person is None:
        return NameGiver(origin=origin, chain=chain)
    user_id, display_name = person
    return NameGiver(origin=origin, user_id=user_id, display_name=display_name, chain=chain)


__all__ = ["MAX_CHAIN_DEPTH", "NameGiver", "Origin", "resolve_name_giver"]
