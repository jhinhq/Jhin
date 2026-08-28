"""Built-in memory tools: ``memory.search`` and ``memory.propose``.

Both are scoped to the *calling* agent by construction: the executor uses
``ctx.agent_id`` and the agent's live team memberships as the authorization
subject, so another agent's private memory can never be returned, and a
proposal is routed through deterministic policy with the current task as its
source (the model cannot pick a source, activate workspace memory, or
broaden visibility).

A proposal that actually stores something also writes one visible chat card
(``kind: "memory_saved"``), because "I recorded that" in the agent's prose is
a claim and not evidence: the two bugs this card exists for were a correction
the agent acknowledged and never stored, and a save the person believed was
company-wide that landed on one agent. See :func:`_write_memory_card`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_db.models import Conversation, MemoryRecord, Message, Team, UserQuestion
from jhin_domain import (
    MEMORY_RETRIEVABLE_STATUSES,
    ActorType,
    MemoryKind,
    MemoryScope,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    UserQuestionStatus,
    new_uuid7,
    structured_content,
)
from jhin_memory import (
    ActorFacts,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    build_memory_context,
    derive_source_facts,
)
from jhin_memory.types import MAX_CANDIDATE_CHARS
from jhin_policy import MEMORY_PROPOSE_CAPABILITY, MEMORY_READ_CAPABILITY, RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError

_SEARCH_MAX_CHARS = 4_000


class MemorySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=8, ge=1, le=20)


class MemorySearchItem(BaseModel):
    id: str
    version: int
    kind: str
    scope: str
    status: str
    content: str
    pinned: bool


class MemorySearchOutput(BaseModel):
    items: list[MemorySearchItem]
    mode: str
    degraded: bool
    context_hash: str


async def _memory_search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(MemorySearchInput, payload)
    context = await build_memory_context(
        ctx.session,
        workspace_id=ctx.workspace_id,
        agent_id=ctx.agent_id,
        query=data.query,
        max_records=data.limit,
        max_chars=_SEARCH_MAX_CHARS,
    )
    return MemorySearchOutput(
        items=[
            MemorySearchItem(
                id=str(item.id),
                version=item.version,
                kind=item.kind.value,
                scope=item.scope.value,
                status=item.status.value,
                content=item.content,
                pinned=item.pinned,
            )
            for item in context.items
        ],
        mode=context.provenance.mode,
        degraded=context.provenance.degraded,
        context_hash=context.provenance.context_hash,
    )


class MemoryProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    kind: MemoryKind = MemoryKind.FACT
    subject: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    requested_scope: MemoryScope = MemoryScope.AGENT
    # The id of an answered organization.ask_person question whose answer
    # authorises a scope wider than this chat could reach on its own. It is
    # only a pointer: every fact that matters is re-read from the row.
    authorized_by_question_id: str | None = Field(default=None, max_length=64)


class MemoryProposeOutput(BaseModel):
    outcome: str
    status: str
    memory_id: str
    reasons: list[str]
    # The reason codes above are for audit; on their own they taught agents to
    # repeat words like "non_amplification" back to the person who asked. This
    # says what happened and what would work instead.
    detail: str = ""


# Plain language for the outcomes an agent can actually do something about.
# Anything unlisted falls back to the generic line below, which is still a
# sentence rather than a code.
_REASON_DETAIL: dict[str, str] = {
    "non_amplification": (
        "Not saved at that scope. A chat is between you and one person, so it "
        "can become your own memory on its own. To remember it for the team or "
        "the company, ask them with organization.ask_person (kind "
        "'memory_scope') and propose again with authorized_by_question_id set "
        "to their answer."
    ),
    "insufficient_authority": (
        "Not saved: that scope is wider than the person asking is allowed to "
        "set. Remember it at 'agent' scope instead, and say who would need to "
        "record it more widely."
    ),
    "no_team_for_scope": (
        "Not saved: you are not on a team, so there is no team memory to add "
        "to. Propose it with requested_scope 'agent' instead."
    ),
    "no_agent_for_scope": "Not saved: this memory has no agent to belong to.",
    "low_information": (
        "Not saved: too vague to be useful later. Only propose something a "
        "colleague could act on months from now without this conversation."
    ),
    "self_reference": ("Not saved: it describes this conversation rather than a durable fact."),
    "source_internal": ("Not saved: it came from hidden reasoning, which never becomes memory."),
    "duplicate": "Already remembered; nothing new was stored.",
    "near_duplicate": "Already remembered in nearly these words; nothing new was stored.",
    "adjudicated_same": "Already remembered; nothing new was stored.",
    "contradiction": (
        "Not saved: it contradicts something already remembered. Say so to the "
        "person rather than overwriting it yourself."
    ),
    "workspace_promotion_requires_review": (
        "Saved, but a person has to approve it before it becomes "
        "workspace-wide. Tell them it is waiting for review."
    ),
}


def _propose_detail(outcome: str, status: str, reasons: tuple[str, ...] | list[str]) -> str:
    for reason in reasons:
        detail = _REASON_DETAIL.get(reason)
        if detail:
            return detail
    if outcome == "reject":
        return "Not saved, and the reason is not one you can act on."
    if status == "active":
        return "Remembered."
    return "Recorded."


# Why a cited answer did not authorise this memory. Each one is a way the
# model could otherwise have widened a memory on its own say-so, so none of
# them falls back to an agent-scoped save: a memory quietly filed as the
# agent's own, when the person believes it is company-wide, is worse than a
# refusal the agent has to relay.
_GRANT_DETAIL: dict[str, str] = {
    "question_not_found": (
        "Not saved. There is no such question in this workspace, so nothing "
        "authorises a wider memory. Ask them with organization.ask_person "
        "first, then propose once with the id that call returns."
    ),
    "question_not_yours": (
        "Not saved. That question was asked by a different agent, and their "
        "answer is not yours to spend. Ask the person yourself."
    ),
    "question_not_this_run": (
        "Not saved. That answer was given in an earlier run and no longer "
        "authorises anything. Ask again now if you still need the scope."
    ),
    "question_not_answered": (
        "Not saved. They have not answered that question yet, so nothing is "
        "authorised. Wait for the answer rather than assuming it."
    ),
    "scope_not_authorized": (
        "Not saved. Their answer did not authorise a wider memory. Propose it "
        "again with requested_scope 'agent' and no authorized_by_question_id, "
        "and tell them who could record it more widely."
    ),
    "scope_mismatch": (
        "Not saved. They authorised a different scope from the one you asked "
        "for. Propose it with requested_scope set to exactly the scope in "
        "the answer."
    ),
    "grant_already_used": (
        "Not saved. You already used their answer for one memory. Ask again "
        "if there is a second fact to record."
    ),
}


def _grant_problem(
    row: UserQuestion | None, ctx: ToolExecutionContext, requested_scope: MemoryScope
) -> str | None:
    """The first reason this answer does not authorise this memory.

    Every check reads the Postgres row, never the tool arguments: the model
    supplies an id and nothing else, and an id is not a claim.
    """
    if row is None:
        return "question_not_found"
    if row.agent_id != ctx.agent_id:
        return "question_not_yours"
    # An answer authorises the turn it was given in. Without this, a question
    # answered last month would still be authorising memories today.
    if row.run_id != ctx.run_id:
        return "question_not_this_run"
    if row.status != UserQuestionStatus.ANSWERED.value:
        return "question_not_answered"
    if not row.granted_scope:
        return "scope_not_authorized"
    if row.granted_scope != requested_scope.value:
        return "scope_mismatch"
    # One answer is worth exactly one memory. The tool-call escape keeps a
    # gateway replay of the *same* call idempotent rather than refusing it.
    if row.grant_consumed_at is not None and row.grant_consumed_tool_call_id != ctx.tool_call_id:
        return "grant_already_used"
    return None


async def _authorized_actor(
    ctx: ToolExecutionContext, data: MemoryProposeInput
) -> tuple[ActorFacts, str | None]:
    """The actor this proposal is made as, and why it could not be widened.

    Without a cited question it is the agent itself, exactly as before. With
    one, it becomes the person who answered — carrying the RBAC ceiling the
    API recorded at answer time — and ``authored_by_model`` keeps the quality
    screens on, because they authorised the scope and not the wording.
    """
    agent_actor = ActorFacts(actor_type=ActorType.AGENT, actor_id=ctx.agent_id)
    if not data.authorized_by_question_id:
        return agent_actor, None
    try:
        question_id = UUID(data.authorized_by_question_id)
    except ValueError:
        return agent_actor, "question_not_found"
    row = await ctx.session.scalar(
        select(UserQuestion)
        .where(
            UserQuestion.id == question_id,
            UserQuestion.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
    )
    problem = _grant_problem(row, ctx, data.requested_scope)
    if problem is not None or row is None:
        return agent_actor, problem or "question_not_found"
    row.grant_consumed_at = datetime.now(UTC)
    row.grant_consumed_tool_call_id = ctx.tool_call_id
    return (
        ActorFacts(
            actor_type=ActorType.USER,
            actor_id=row.answered_by_user_id,
            explicit=True,
            authority=MemoryScope(row.granted_authority),
            authored_by_model=True,
        ),
        None,
    )


# The scope, in the words a person uses about it. Written here from the
# stored record, never by the model — for the same reason the ask_person
# options are not: a card reading "the Platform team" over a workspace-wide
# write is the mislabelling bug again, one surface along.
_SCOPE_LABELS: dict[str, str] = {
    MemoryScope.AGENT.value: "just you and me",
    MemoryScope.WORKSPACE.value: "everyone in the workspace",
}

# Memory the agent will actually recall. A record still ``proposed`` pending
# human review is stored but not yet memory, and the card must not say it is.
_RECALLABLE_STATUSES = frozenset(s.value for s in MEMORY_RETRIEVABLE_STATUSES)


async def _scope_label(ctx: ToolExecutionContext, record: MemoryRecord) -> str:
    if record.scope != MemoryScope.TEAM.value:
        return _SCOPE_LABELS.get(record.scope, "just you and me")
    # ``scope_id`` is the team the record was actually filed under, which is
    # not necessarily the calling agent's own team.
    name = await ctx.session.scalar(
        select(Team.name).where(Team.id == record.scope_id, Team.workspace_id == ctx.workspace_id)
    )
    return f"the {name} team" if name else "your team"


async def _conflicting_memory(ctx: ToolExecutionContext, record: MemoryRecord) -> str:
    """An older recallable memory on the same subject and scope that this one
    did not replace. Empty when there is none, or when the subject is blank --
    without a subject there is nothing to be confident is the same topic, and
    a false "still active" warning would be its own kind of noise."""
    subject = (record.subject or "").strip()
    if not subject:
        return ""
    previous = await ctx.session.scalar(
        select(MemoryRecord)
        .where(
            MemoryRecord.workspace_id == ctx.workspace_id,
            MemoryRecord.id != record.id,
            MemoryRecord.scope == record.scope,
            MemoryRecord.scope_id == record.scope_id,
            MemoryRecord.subject == record.subject,
            MemoryRecord.status.in_(_RECALLABLE_STATUSES),
        )
        .order_by(MemoryRecord.created_at.desc())
        .limit(1)
    )
    return previous.content if previous is not None else ""


async def _write_memory_card(
    ctx: ToolExecutionContext, record: MemoryRecord, source: SourceFacts
) -> None:
    """One visible chat row saying what was stored, at which scope, in whose words.

    Written from the persisted record inside the same transaction as the
    record itself, so the card and the memory are true together or neither
    exists. Exactly one card per stored record follows from that: a proposal
    that stores nothing (rejected, duplicate) never reaches here, a run that
    proposes twice stores two records and gets two cards, and a gateway
    replay of the same call re-proposes content that is now an exact
    duplicate of itself — outcome ``duplicate``, no second card.

    Only memory the agent will actually recall gets a card. A record still
    ``proposed`` pending human review is not memory yet, and a card saying
    "saved · everyone in the workspace" over one would be exactly the
    over-claim this whole surface exists to stop.
    """
    if record.status not in _RECALLABLE_STATUSES:
        return
    superseded = ""
    if record.supersedes_id is not None:
        previous = await ctx.session.get(MemoryRecord, record.supersedes_id)
        if previous is not None:
            superseded = previous.content
    # A correction the near-duplicate policy did not recognise as one stores a
    # second record and leaves the first live, so the agent will recall both
    # Wednesday and Thursday. The card is the surface meant to settle exactly
    # that, and saying only "Remembered" sides against itself while the
    # agent's own prose claims it replaced something. Name what is still
    # standing so the person can retire it.
    still_standing = ""
    if record.supersedes_id is None:
        still_standing = await _conflicting_memory(ctx, record)
    conversation_id = source.ref.conversation_id
    recipient_id: UUID | None = None
    if conversation_id is not None:
        recipient_id = await ctx.session.scalar(
            select(Conversation.created_by_user_id).where(
                Conversation.id == conversation_id,
                Conversation.workspace_id == ctx.workspace_id,
            )
        )
    ctx.session.add(
        Message(
            id=new_uuid7(),
            workspace_id=ctx.workspace_id,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            conversation_id=conversation_id,
            sender_type=SenderType.AGENT.value,
            sender_id=ctx.agent_id,
            recipient_type=RecipientType.USER.value,
            recipient_id=recipient_id,
            message_type=MessageType.STATUS.value,
            content_json=structured_content(
                # The summary is the remembered words themselves, so a
                # renderer that does not know this card yet still shows what
                # was stored rather than an empty row.
                record.content,
                kind="memory_saved",
                memory_id=str(record.id),
                action="updated" if record.supersedes_id is not None else "saved",
                scope=record.scope,
                scope_label=await _scope_label(ctx, record),
                content=record.content,
                superseded=superseded,
                still_standing=still_standing,
            ),
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    await ctx.session.flush()


async def _memory_propose(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(MemoryProposeInput, payload)
    actor, problem = await _authorized_actor(ctx, data)
    if problem is not None:
        return MemoryProposeOutput(
            outcome="reject",
            status="none",
            memory_id="",
            reasons=[problem],
            detail=_GRANT_DETAIL[problem],
        )
    source = await derive_source_facts(
        ctx.session, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, task_id=ctx.task_id
    )
    if source is None:
        raise ToolExecutionError(
            "current task not found", code="memory_source_missing", side_effect_possible=False
        )
    candidate = MemoryCandidate(
        content=data.content,
        kind=data.kind,
        subject=data.subject,
        tags=tuple(data.tags),
        confidence=data.confidence,
        importance=data.importance,
        requested_scope=data.requested_scope,
    )
    result = await apply_candidates(
        ctx.session,
        candidates=[candidate],
        source=source,
        actor=actor,
    )
    decision = result.decisions[0]
    record_id = ""
    status = "none"
    if result.created:
        record_id = str(result.created[0].id)
        status = result.created[0].status
        await _write_memory_card(ctx, result.created[0], source)
    elif decision.duplicate_of is not None:
        record_id = str(decision.duplicate_of)
        status = "duplicate"
    return MemoryProposeOutput(
        outcome=decision.outcome,
        status=status,
        memory_id=record_id,
        reasons=list(decision.reasons),
        detail=_propose_detail(decision.outcome, status, decision.reasons),
    )


MEMORY_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="memory.search",
            description=(
                "Search your curated long-term memory (your private memory, your "
                "teams' memory, and company knowledge) for records relevant to a "
                "query. Returns only records you are authorized to see right now."
            ),
            risk=RiskLevel.READ,
            input_model=MemorySearchInput,
            output_model=MemorySearchOutput,
            required_capability=MEMORY_READ_CAPABILITY,
        ),
        _memory_search,
        None,
    ),
    (
        ToolDefinition(
            name="memory.propose",
            description=(
                "Propose one concise, durable memory from the current task. "
                "**When a person corrects something you have remembered, or "
                "tells you a stored fact has changed, call this** with the new "
                'wording -- saying "got it, I\'ll use that from now on" '
                "records nothing, and you will state the old value again in the "
                "next conversation. A correction supersedes the memory it "
                "replaces; propose it the same way you proposed the original. "
                "Use requested_scope 'agent' by default: an ordinary chat is "
                "between you and one person, and what is said there is your "
                "memory unless they say otherwise. When the fact is about a "
                "team or the whole company, first ask them with "
                "organization.ask_person (kind 'memory_scope'), then propose "
                "once with requested_scope set to the scope they chose and "
                "authorized_by_question_id set to that question's id -- their "
                "answer is what authorises the wider memory, not your reading "
                "of the conversation. Never pass authorized_by_question_id for "
                "a question they did not answer, and never propose a wider "
                "scope than the one they picked; both are refused and the "
                "memory is lost. A rejected "
                "proposal comes back with a `detail` sentence saying what would "
                "work instead -- relay that, not the reason codes. Never "
                "include secrets or credentials."
            ),
            risk=RiskLevel.WRITE,
            input_model=MemoryProposeInput,
            output_model=MemoryProposeOutput,
            required_capability=MEMORY_PROPOSE_CAPABILITY,
            supports_approval=True,
        ),
        _memory_propose,
        None,
    ),
)
