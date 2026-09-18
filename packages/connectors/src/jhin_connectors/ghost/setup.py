"""Bind a captured variable using actual human-provided Ghost setup facts.

No model may nominate a guessed credential destination or promote itself by
inventing publishing authority. No capability grants are created here.
"""

import re
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_connectors.ghost.client import (
    GhostApiError,
    admin_origin,
    ghost_request,
    validate_admin_url,
)
from jhin_db.models import (
    Agent,
    AuditEvent,
    Connection,
    Message,
    Task,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_secrets.authority import human_content_authorized, human_message_authorized
from jhin_secrets.intake import supplied_ghost_admin_urls
from jhin_secrets.variables import (
    VariableActor,
    VariableError,
    VariableStore,
    bind_internal,
)
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.naming_authority import resolve_name_giver


class GhostBindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variable_id: UUID
    admin_url: str = Field(min_length=1, max_length=2000)
    publisher_agent_id: UUID | None = None


class GhostBindOutput(BaseModel):
    connection_id: UUID
    name: str
    admin_url: str
    publisher_agent_id: UUID | None
    verified: bool
    detail: str
    verified_memory_facts: list[str] = Field(default_factory=list)


def _clauses(texts: list[str]) -> list[str]:
    """Only direct declarations can confirm setup; quoted examples cannot."""
    clauses = []
    for text in texts:
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
        clauses.extend(re.split(r"(?<=[.!?])\s+|[;\n]|\b(?:but|and)\b", text, flags=re.I))
    return clauses


def _negative(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:no|not|never|cannot|can't|don't|doesn't|without|avoid|forbid|stop|unless)\b",
            text,
            re.I,
        )
    )


def _supplied_urls(texts: list[str]) -> list[str]:
    return supplied_ghost_admin_urls(texts)


def _publisher_confirmed(texts: list[str], name: str) -> bool:
    named = re.escape(name)
    positive = False
    for clause in _publisher_clauses(texts, name):
        if not re.search(rf"(?<!\w){named}(?!\w)", clause, re.I):
            continue
        if _negative(clause) and re.search(r"\bpublish\w*\b", clause, re.I):
            return False
        if re.search(
            rf"(?:\b(?:only\s+)?(?:the\s+)?{named}\s+"
            r"(?:can|may|should|must|will|is allowed to)\s+publish\b|"
            rf"\b{named}(?:\s+\([^()\n]{{1,100}}\))?\s+is\s+"
            r"(?:(?:the|our)\s+)?(?:(?:designated|sole|only)\s+)?publisher\b|"
            rf"\b(?:publisher|publishing agent)\s+(?:is\s+)?(?:the\s+)?{named}(?!\w))",
            clause,
            re.I,
        ):
            positive = True
    return positive


def _publisher_rejected(texts: list[str], name: str) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(name)}(?!\w)", clause, re.I)
        and _negative(clause)
        and re.search(r"\bpublish\w*\b", clause, re.I)
        for clause in _publisher_clauses(texts, name)
    )


def _publisher_clauses(texts: list[str], name: str) -> list[str]:
    clean: list[str] = []
    for text in texts:
        # A quoted agent name is ordinary formatting; a quoted instruction
        # or documentation example does not designate publishing authority.
        for left, right in (('"', '"'), ("'", "'"), ("`", "`"), ("“", "”")):
            text = text.replace(left + name + right, name)
        text = re.sub(r'`[^`]*`|"[^"]*"|“[^”]*”|(?<!\w)\'[^\']*\'(?!\w)', "", text)
        clean.extend(
            clause
            for clause in _clauses([text])
            if not re.search(
                r"\b(?:example|quoted?|said|says|whether|maybe|perhaps)\b", clause, re.I
            )
        )
    return clean


def _url_rejected(texts: list[str], base: str) -> bool:
    for clause in _clauses(texts):
        if not _negative(re.sub(r'https?://[^\s<>"`]+', "", clause)):
            continue
        for url in re.findall(r"https?://[^\s<>\"`]+", clause):
            with suppress(GhostApiError):
                if validate_admin_url(url.rstrip(".,;)")) == base:
                    return True
    return False


async def _human_setup(ctx: ToolExecutionContext, data: GhostBindInput, base: str) -> UUID:
    giver = await resolve_name_giver(
        ctx.session,
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not giver.user_id or giver.origin not in {"chat", "approval"}:
        raise GhostApiError(
            "Ghost setup requires a direct request from a workspace admin",
            code="ghost_setup_authority",
        )
    role = await ctx.session.scalar(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == ctx.workspace_id,
            WorkspaceMembership.user_id == giver.user_id,
        )
    )
    if role not in {"owner", "admin"}:
        raise GhostApiError(
            "Ghost setup requires a current workspace admin", code="ghost_setup_authority"
        )
    task = await ctx.session.get(Task, ctx.task_id)
    if task is None:
        raise GhostApiError("Ghost setup task is unavailable", code="ghost_setup_authority")
    query = (
        select(Message)
        .where(
            Message.workspace_id == ctx.workspace_id,
            Message.sender_type == "user",
            Message.sender_id == giver.user_id,
            Message.task_id == task.id,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(30)
    )
    messages = await ctx.session.scalars(query)
    texts = [
        message.content_json.get("text", "")
        for message in messages
        if await human_message_authorized(ctx.session, message, required_scope="apps:write")
    ]
    texts = [text for text in texts if isinstance(text, str)]
    metadata = task.metadata_json or {}
    resolved_values = metadata.get("resolved_inputs", {})
    authorities = metadata.get("resolved_input_authority", {})
    resolved = {}
    if isinstance(resolved_values, dict) and isinstance(authorities, dict):
        for key in ("ghost_admin_url", "ghost_publisher_agent_id"):
            proof = authorities.get(key)
            if not isinstance(proof, dict):
                continue
            try:
                answer_user_id = UUID(str(proof.get("user_id")))
            except ValueError:
                continue
            if await human_content_authorized(
                ctx.session, ctx.workspace_id, answer_user_id, proof, required_scope="apps:write"
            ):
                resolved[key] = resolved_values.get(key)
    supplied_urls = [resolved.get("ghost_admin_url")] if isinstance(resolved, dict) else []
    supplied_urls.extend(_supplied_urls(texts))
    supplied = False
    for candidate in supplied_urls:
        if not isinstance(candidate, str):
            continue
        with suppress(GhostApiError):
            supplied = supplied or validate_admin_url(candidate.rstrip(".,;)")) == base
    if not supplied or _url_rejected(texts, base):
        raise GhostApiError(
            "Ask for the actual Ghost Admin URL using required input_key='ghost_admin_url'; "
            "no supplied Admin URL matches this destination",
            code="ghost_url_not_confirmed",
        )
    if data.publisher_agent_id:
        publisher = await ctx.session.scalar(
            select(Agent).where(
                Agent.id == data.publisher_agent_id,
                Agent.workspace_id == ctx.workspace_id,
                Agent.status == "active",
            )
        )
        answered_publisher = (
            str(resolved.get("ghost_publisher_agent_id", "")).strip().casefold()
            if isinstance(resolved, dict)
            else ""
        )
        if (
            publisher is None
            or _publisher_rejected(texts, publisher.name)
            or not (
                _publisher_confirmed(texts, publisher.name)
                or answered_publisher in {str(publisher.id), publisher.name.casefold()}
            )
        ):
            raise GhostApiError(
                "Ask which agent may publish using required input_key='ghost_publisher_agent_id'; "
                "the human request must name the designated publisher",
                code="ghost_publisher_not_confirmed",
            )
    return giver.user_id


async def bind_ghost(ctx: ToolExecutionContext, payload: BaseModel) -> GhostBindOutput:
    from jhin_connectors.ghost.authority import installation_authority

    data = GhostBindInput.model_validate(payload.model_dump())
    base = validate_admin_url(data.admin_url)
    user_id = await _human_setup(ctx, data, base)
    if data.publisher_agent_id:
        await installation_authority(
            ctx.session, ctx.workspace_id, base, data.publisher_agent_id, establish=True
        )
    # Variable mutation and revocation acquire workspace before connection.
    # Preserve that order across re-binding, including concurrent deletion.
    await ctx.session.scalar(
        select(Workspace.id).where(Workspace.id == ctx.workspace_id).with_for_update(key_share=True)
    )
    store = VariableStore(ctx.session, ctx.crypto)
    try:
        variable = await store.get(
            VariableActor(ctx.workspace_id, "agent", ctx.agent_id), data.variable_id
        )
        if not variable.sensitive:
            raise VariableError("Choose a sensitive Admin key variable")
        if data.publisher_agent_id:
            await store.get(
                VariableActor(ctx.workspace_id, "agent", data.publisher_agent_id), data.variable_id
            )
    except VariableError:
        raise GhostApiError(
            "The Admin key must be a sensitive variable accessible to the writer "
            "and designated publisher; ask to share it at the appropriate scope",
            code="ghost_key_scope",
        ) from None
    # Stable identity for uncertain tool retries; one binding per variable.
    name = f"Ghost · {variable.name} · {str(variable.id)[-12:]}"
    matches = list(
        await ctx.session.scalars(
            select(Connection)
            .where(
                Connection.workspace_id == ctx.workspace_id,
                Connection.connector_type == "ghost",
                Connection.config_json["admin_key_variable_id"].as_string() == str(variable.id),
            )
            .limit(2)
            .with_for_update()
        )
    )
    if len(matches) > 1:
        raise GhostApiError(
            "This key has multiple existing Ghost connections; an admin must resolve "
            "the conflicting bindings before reconnecting",
            code="ghost_binding_conflict",
        )
    connection = matches[0] if matches else None
    if connection is not None and (
        connection.connector_type != "ghost"
        or connection.config_json.get("admin_url") != base
        or connection.config_json.get("admin_key_variable_id") != str(variable.id)
    ):
        raise GhostApiError(
            "This key already has a different Ghost binding; an admin must change it explicitly",
            code="ghost_binding_conflict",
        )
    if connection is None:
        connection = Connection(
            id=new_uuid7(),
            workspace_id=ctx.workspace_id,
            connector_type="ghost",
            name=name,
            auth_type="api_key",
            status="error",
            created_by_user_id=user_id,
            config_json={
                "admin_url": base,
                "admin_key_variable_id": str(variable.id),
                "configured_by_agent_id": str(ctx.agent_id),
            },
        )
        ctx.session.add(connection)
        await ctx.session.flush()
    if data.publisher_agent_id:
        connection.config_json = {
            **connection.config_json,
            "publisher_agent_id": str(data.publisher_agent_id),
        }
    await bind_internal(
        ctx,
        variable.id,
        connection_id=connection.id,
        credential_field="admin_key",
        approved_origin=admin_origin(base),
    )
    key = await store.resolve_bound(
        VariableActor(ctx.workspace_id, "agent", ctx.agent_id),
        variable.id,
        connection.id,
        credential_field="admin_key",
        approved_origin=admin_origin(base),
        allow_disabled=True,
    )
    connection.status = "error"
    connection.last_verified_at = datetime.now(UTC)
    connection.last_error = "Ghost setup has not been verified"
    try:
        await ghost_request(base, key, "GET", "posts/", params={"limit": 1})
    except GhostApiError as error:
        connection.last_error = str(error)
        await ctx.session.flush()
        raise
    connection.status = "active"
    connection.last_error = None
    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type="user",
            actor_id=user_id,
            action="ghost.connection.bound",
            target_type="connection",
            target_id=connection.id,
            metadata_json={
                "variable_id": str(variable.id),
                "admin_url": base,
                "publisher_agent_id": connection.config_json.get("publisher_agent_id", ""),
                "agent_id": str(ctx.agent_id),
            },
        )
    )
    await ctx.session.flush()
    return GhostBindOutput(
        connection_id=connection.id,
        name=connection.name,
        admin_url=base,
        publisher_agent_id=connection.config_json.get("publisher_agent_id") or None,
        verified=True,
        detail=(
            "Ghost Admin access verified. Draft tools are ready. "
            + (
                "Only the designated publisher can approve and publish reviewed revisions."
                if connection.config_json.get("publisher_agent_id")
                else "Drafts only: no publishing agent has been designated. Review handoff and "
                "publication require an admin to name a publisher who can access this key."
            )
        ),
        verified_memory_facts=[f"Ghost Admin URL: {base}", f"Ghost connection: {connection.id}"],
    )


GHOST_SETUP_TOOLS = (
    (
        ToolDefinition(
            name="ghost.connection.bind",
            description=(
                "Connect Ghost using a sensitive variable reference, the actual "
                "human-supplied Ghost Admin URL, and optionally the human-named "
                "publishing director. Ask required questions for missing facts before "
                "using this tool. Never pass a raw key or guess a URL. "
                "Requires a direct admin request; adds no capability grants."
            ),
            risk=RiskLevel.WRITE,
            supports_approval=True,
            input_model=GhostBindInput,
            output_model=GhostBindOutput,
            required_capability="ghost.connection.bind",
        ),
        bind_ghost,
    ),
)
