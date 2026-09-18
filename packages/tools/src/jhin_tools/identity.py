"""The one tool an agent uses to set its own name.

The bug this exists for, in full:

    Operator: "Hi what is your name?"
    Agent:    "I don't have a personal name - you can just call me your
               Senior Software Engineer."
    Operator: "Your name is Bisby"
    Agent:    "Got it - you can call me Bisby for this chat."

Both replies were honest. The agent had no tool that writes ``Agent.name``
— the only writer was an admin-gated ``PATCH /agents/{id}`` — so "for this
chat" was the whole of what it could offer, and the next conversation opened
with the old name again.

A name is a **row**, not a memory. Memory renders at the bottom of the system
prompt, ranked and capped, and on installs without embeddings retrieval is
lexical and degraded; a record saying "I am Bisby" would argue with two
unconditional assertions of the old name above it — the preamble and the
roster — and lose. So this tool changes the row, and the memory it writes
alongside is the *provenance* (who conferred the name, and when), not the
name itself.

Five things hold it down:

- **The input names no target.** There is a name and nothing else, so
  renaming a colleague is not expressible rather than merely refused. That
  stays true however the model is prompted, which a validator would not.
- **A name is conferred by a person** (:mod:`jhin_tools.naming_authority`).
  Self-only removed "rename a colleague" and left "order a colleague to
  rename itself", which is the same outcome one hop further out — and that is
  how both live runs did it, a CTO agent delegating the rename and the target
  calling this tool on its first step without asking anyone. So the executor
  refuses a run with no human counterpart, and the audit row records who
  actually asked, delegation chain included, instead of crediting the person
  at the top of a thread who never asked for anything.
- **The name is allow-listed, not deny-listed**
  (``jhin_policy.agent_name_problem``). It is the first agent-written string
  to reach layer 1 of a system prompt unhedged, and it reaches 84 other
  agents through the roster.
- **``Agent.slug`` never changes.** The slug is the stable handle — links,
  references, and the preamble's fallback name all keep working across a
  rename, and a rename cannot squat on another agent's handle.
- **A rename cannot be hidden.** One visible receipt in the conversation and
  one ``agent.renamed`` audit row, both written in the same transaction as
  the row change, so a person can see it happen and undo it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AuditEvent, MemoryRecord, Message
from jhin_domain import (
    MEMORY_RETRIEVABLE_STATUSES,
    ActorType,
    MemoryKind,
    MemoryScope,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    new_uuid7,
    structured_content,
)
from jhin_memory import (
    ActorFacts,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    create_version,
    derive_source_facts,
)
from jhin_policy import (
    IDENTITY_SELF_CAPABILITY,
    MAX_STORED_NAME_CHARS,
    RiskLevel,
    ToolDefinition,
    agent_name_problem,
    normalize_agent_name,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError
from jhin_tools.naming_authority import NameGiver, resolve_name_giver

# The subject every name-provenance record shares, so a later rename
# supersedes the earlier note instead of stacking a second one beside it.
NAME_SUBJECT = "self.name"

# Mirrors ``jhin_api.slugs.slugify`` closely enough to answer "would this
# name collide with a colleague's handle". It is used for the *check* only:
# this tool never writes a slug, and the API remains the authority on how one
# is minted.
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_form(name: str) -> str:
    return _NON_SLUG_RE.sub("-", name.strip().lower()).strip("-")


class SetNameInput(BaseModel):
    """A name, and nothing else. There is deliberately no target field."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=MAX_STORED_NAME_CHARS,
        description=(
            "What you want to be called from now on: letters, digits, spaces, "
            "hyphens, apostrophes and full stops, at most 48 characters and "
            "three words."
        ),
    )

    # The name *rule* is deliberately not a field validator. The gateway
    # reports a schema failure as identifiers only — "name: value_error",
    # never the message — because submitted values must not travel back
    # through that path. Here the reason is the entire point: an agent told
    # "your name is Chief Revenue Officer Bot" should learn that a name is at
    # most three words and offer a variant, not read "value_error". So the
    # rule runs in the executor, where :func:`_refusal` puts its sentence
    # where the model will actually read it, and the schema keeps only the
    # bound that needs no explanation.


class SetNameOutput(BaseModel):
    name: str
    previous_name: str
    # Unchanged by a rename, and said out loud so the model does not tell
    # anyone its links or references have moved.
    slug: str
    changed: bool
    summary: str


def _refusal(message: str, *, code: str, fix: str) -> ToolExecutionError:
    """A refusal whose reason actually reaches the model.

    ``GatewayOutcome.observation_json`` renders a failed call as ``{"error":
    code, "detail": decision_reason}``, and ``decision_reason`` is built from
    the **hint** — the exception's own message does not travel that far. So
    the reason goes in the hint, followed by what to do with it. The preamble
    promises the agent will relay the reason and offer a near variant; this is
    the sentence it relays.
    """
    return ToolExecutionError(
        message,
        code=code,
        side_effect_possible=False,
        hint=f"{message}. {fix}",
    )


def validated_agent_name(raw: str) -> str:
    """The normalized name, or a refusal naming what is wrong with it.

    Public because this tool is not the only writer of ``Agent.name`` an
    *agent* can reach: ``organization.create_agent`` names a colleague. One
    rule for both, or the guarantee is only about renames.
    """
    problem = agent_name_problem(raw)
    if problem is not None:
        raise _refusal(
            problem,
            code="invalid_agent_name",
            fix=(
                "Tell the person that reason in your own words, offer a name that "
                "fits, and call this again with the one they pick"
            ),
        )
    return normalize_agent_name(raw)


async def _caller(session: AsyncSession, ctx: ToolExecutionContext) -> Agent:
    me = await session.scalar(
        select(Agent).where(Agent.id == ctx.agent_id, Agent.workspace_id == ctx.workspace_id)
    )
    if me is None:
        raise ToolExecutionError(
            "the calling agent no longer exists in this workspace",
            code="agent_not_found",
            side_effect_possible=False,
        )
    return me


async def _colliding_agent(
    session: AsyncSession, ctx: ToolExecutionContext, name: str
) -> str | None:
    """The name of a colleague this rename would be confused with, if any.

    Both halves matter: two agents called "Bisby" make every roster line and
    every "ask Bisby to…" ambiguous, and a name whose slug is already some
    colleague's handle is the same collision one step removed.
    """
    row = await session.scalar(
        select(Agent.name)
        .where(
            Agent.workspace_id == ctx.workspace_id,
            Agent.id != ctx.agent_id,
            or_(func.lower(Agent.name) == name.lower(), Agent.slug == _slug_form(name)),
        )
        .limit(1)
    )
    return row


# What to say when there is nobody who can confer a name, per shape of run.
# Each one has to leave the model with something to do, because the refusal
# it replaces is a rename it was told to perform: an agent that reads "no"
# and nothing else reports the block and stops, and the person who actually
# wanted the name never learns that asking directly would work.
_NO_PERSON_FIX = (
    "Tell them you cannot rename yourself on a colleague's say-so, and that "
    "the person who wants it should tell you directly — in a chat, or by "
    "asking a workspace admin. Then carry on with the rest of the work."
)
_NOBODY_HERE_FIX = (
    "Leave it as it is and carry on. When a person next talks to you, tell "
    "them what happened and set the name they ask for then."
)


def _no_person_refusal(giver: NameGiver) -> ToolExecutionError:
    """The refusal for a run with nobody human on the other side.

    Both halves are bounded well inside ``MAX_HINT_CHARS``: the hint is the
    only part that reaches the model, and a fix truncated mid-sentence is a
    refusal it cannot act on.
    """
    if giver.origin in ("delegation", "work_request"):
        who = giver.asked_by_a_colleague or "a colleague"
        return _refusal(
            f"your name is conferred by a person, and {who} is a colleague, "
            "not a person; your name is unchanged",
            code="name_needs_a_person",
            fix=_NO_PERSON_FIX,
        )
    return _refusal(
        "your name is conferred by a person, and there is no person in this "
        "run to confer one; your name is unchanged",
        code="name_needs_a_person",
        fix=_NOBODY_HERE_FIX,
    )


def _rename_audit(
    ctx: ToolExecutionContext,
    *,
    action: str,
    target: Agent,
    previous_name: str,
    name: str,
    giver: NameGiver,
) -> AuditEvent:
    """One audit row saying who actually asked.

    ``requested_by_user_id`` is a person who was there, or null — never the
    person a chain hangs from. ``requested_via`` says which shape of run it
    was, and ``requested_by_chain`` names the colleagues that asked, nearest
    first, so "the CTO ordered this" is in the row rather than inferred from
    two task ids later.
    """
    return AuditEvent(
        workspace_id=ctx.workspace_id,
        actor_type=ActorType.AGENT.value,
        actor_id=ctx.agent_id,
        action=action,
        target_type="agent",
        target_id=target.id,
        metadata_json={
            "from": previous_name,
            "to": name,
            "slug": target.slug,
            "requested_by_user_id": str(giver.user_id) if giver.user_id is not None else None,
            "requested_by_name": giver.display_name,
            "requested_via": giver.origin,
            "requested_by_chain": [dict(entry) for entry in giver.chain],
            "run_id": str(ctx.run_id),
            "via": "organization.identity.set_name",
        },
    )


async def _write_receipt(
    ctx: ToolExecutionContext,
    source: SourceFacts,
    *,
    previous_name: str,
    name: str,
    slug: str,
    recipient_id: UUID | None,
) -> None:
    """One visible chat row saying the name changed, from what, to what.

    The same shape as the memory receipt (``_write_memory_card``) and for the
    same reason: "I'll go by Bisby now" in an agent's prose reads identically
    whether the row changed, was refused, or was never written. It is staged
    in the caller's transaction, so the card and the rename are true together
    or neither exists.

    A run with no conversation behind it — a trigger, a delegated child —
    still gets the row, on its task rather than in a chat. It is the audit
    event that makes a rename impossible to hide; this is the copy that makes
    it impossible to *miss*, wherever there is somebody reading.
    """
    ctx.session.add(
        Message(
            id=new_uuid7(),
            workspace_id=ctx.workspace_id,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            conversation_id=source.ref.conversation_id,
            sender_type=SenderType.AGENT.value,
            sender_id=ctx.agent_id,
            recipient_type=RecipientType.USER.value,
            recipient_id=recipient_id,
            message_type=MessageType.STATUS.value,
            content_json=structured_content(
                f"Now called {name} (was {previous_name}).",
                kind="agent_renamed",
                previous_name=previous_name,
                name=name,
                slug=slug,
            ),
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    await ctx.session.flush()


async def _remember_who_named_me(
    ctx: ToolExecutionContext,
    source: SourceFacts,
    *,
    previous_name: str,
    name: str,
    asked_by: str,
) -> None:
    """One companion memory: who conferred this name and when.

    Not the name — that is the row, and this record must never be mistaken
    for the thing that holds it. The copy says so out loud, because an agent
    reading it back in six months (or a person deciding whether to forget it)
    should not have to guess whether forgetting renames it.

    Written by the platform, not the model, so it is exempt from the
    self-reference screen by *writer*: ``is_self_referential`` still rejects
    an agent that decides on its own to memorise "the assistant is called X",
    which remains worthless.

    A second rename replaces this note as its next **version** rather than
    proposing a second one beside it. Two live records on subject
    ``self.name`` would be marked contested — two answers to "why am I called
    this" that are both true is not a contradiction, and the ordinary
    near-duplicate path would just as happily have skipped the new one and
    left the note naming the previous name. One live note, with the earlier
    ones superseded and still readable in the version chain.
    """
    when = datetime.now(UTC).date().isoformat()
    who = asked_by or "someone in this conversation"
    was = f" Before that I was {previous_name}." if previous_name else ""
    content = (
        f"I go by {name} because {who} asked me to on {when}.{was} The name itself "
        "lives on my agent record, not in this note: forgetting this removes the "
        "explanation, not the name."
    )
    actor = ActorFacts(
        actor_type=ActorType.AGENT,
        actor_id=ctx.agent_id,
        authored_by_platform=True,
    )
    previous_note = await ctx.session.scalar(
        select(MemoryRecord)
        .where(
            MemoryRecord.workspace_id == ctx.workspace_id,
            MemoryRecord.scope == MemoryScope.AGENT.value,
            MemoryRecord.scope_id == ctx.agent_id,
            MemoryRecord.subject == NAME_SUBJECT,
            MemoryRecord.status.in_([status.value for status in MEMORY_RETRIEVABLE_STATUSES]),
        )
        .order_by(MemoryRecord.created_at.desc())
        .limit(1)
    )
    if previous_note is not None:
        await create_version(
            ctx.session,
            previous_note,
            content=content,
            actor=actor,
            subject=NAME_SUBJECT,
            confidence=1.0,
            importance=0.7,
        )
        return
    await apply_candidates(
        ctx.session,
        candidates=[
            MemoryCandidate(
                content=content,
                kind=MemoryKind.FACT,
                subject=NAME_SUBJECT,
                tags=("identity",),
                # A recorded fact about something that just happened, not an
                # inference: there is nothing to be uncertain about.
                confidence=1.0,
                importance=0.7,
                requested_scope=MemoryScope.AGENT,
            )
        ],
        source=source,
        actor=actor,
        agent_name=name,
    )


async def _set_name(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(SetNameInput, payload)
    session = ctx.session
    name = validated_agent_name(data.name)
    me = await _caller(session, ctx)
    previous_name = me.name

    if normalize_agent_name(previous_name) == name:
        # Also the idempotency backstop for a gateway replay: no second
        # receipt, no second audit row, no second memory.
        return SetNameOutput(
            name=me.name,
            previous_name=previous_name,
            slug=me.slug,
            changed=False,
            summary=f"You are already called {me.name}; nothing changed.",
        )

    # Who is on the other side of this run. Before the collision check,
    # because "you may not be renamed here at all" is the answer, and telling
    # a colleague's errand-runner which names are taken is answering a
    # question it was never entitled to ask.
    giver = await resolve_name_giver(
        session,
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not giver.is_a_person:
        # The refusal is recorded, not just returned: an order to rename that
        # the platform stopped is exactly what an operator wants to find
        # afterwards, and the row is the only place it would survive. It
        # commits with the failed call because this refusal is declared to
        # have no side effect, so the gateway keeps the transaction.
        session.add(
            _rename_audit(
                ctx,
                action="agent.rename_refused",
                target=me,
                previous_name=previous_name,
                name=name,
                giver=giver,
            )
        )
        await session.flush()
        raise _no_person_refusal(giver)

    clash = await _colliding_agent(session, ctx, name)
    if clash is not None:
        raise _refusal(
            f"a colleague in this workspace is already called {clash}",
            code="agent_name_taken",
            fix="Say so, and pick a name that is not already a colleague's",
        )

    source = await derive_source_facts(
        session, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, task_id=ctx.task_id
    )
    if source is None:
        raise ToolExecutionError(
            "current task not found", code="identity_source_missing", side_effect_possible=False
        )

    me.name = name
    # Emphatically NOT me.slug: the handle is stable across a rename, so
    # links, references and the preamble's fallback name all survive it.
    session.add(
        _rename_audit(
            ctx,
            action="agent.renamed",
            target=me,
            previous_name=previous_name,
            name=name,
            giver=giver,
        )
    )
    await session.flush()
    await _write_receipt(
        ctx,
        source,
        previous_name=previous_name,
        name=name,
        slug=me.slug,
        recipient_id=giver.user_id,
    )
    await _remember_who_named_me(
        ctx, source, previous_name=previous_name, name=name, asked_by=giver.display_name
    )
    return SetNameOutput(
        name=name,
        previous_name=previous_name,
        slug=me.slug,
        changed=True,
        summary=(
            f"You are called {name} from now on (you were {previous_name}); your handle "
            f"{me.slug} is unchanged. This conversation still shows the old name above, "
            "and everything after it uses the new one."
        ),
    )


IDENTITY_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.identity.set_name",
            description=(
                "Set what you are called. **When someone tells you your name, or "
                "what to call yourself, call this in the same turn** — replying "
                'that they "can call you that for this chat" changes nothing, and '
                "the next conversation starts from your old name again. This "
                "changes only your own name; there is no way to rename a "
                "colleague with it, and an admin does that from the agent's "
                "settings. A name is conferred by a **person**: when a "
                "colleague asks you to rename yourself — however they word it "
                "— this call is refused, so say so and tell them the person "
                "who wants it should ask you directly. A name is letters, digits, spaces, hyphens, "
                "apostrophes and full stops, at most 48 characters and three "
                "words, and it cannot be a colleague's name. Your handle (slug) "
                "does not change, so existing links and references keep working. "
                "It takes effect immediately for everyone else, but the "
                "conversation you are in was written with the old name above, so "
                "say plainly what you are called now."
            ),
            risk=RiskLevel.WRITE,
            input_model=SetNameInput,
            output_model=SetNameOutput,
            required_capability=IDENTITY_SELF_CAPABILITY,
            # A workspace on a restrictive approval policy should be able to
            # park a rename on a person rather than be told the tool cannot
            # be approved at all.
            supports_approval=True,
        ),
        _set_name,
        None,
    ),
)

__all__ = [
    "IDENTITY_TOOLS",
    "NAME_SUBJECT",
    "SetNameInput",
    "SetNameOutput",
    "validated_agent_name",
]
