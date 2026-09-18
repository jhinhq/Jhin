"""Human setup provenance, opaque variables, app grants, and publisher scope."""

import pytest
from sqlalchemy import select

from jhin_connectors.ghost.access import ghost_access_allowed
from jhin_connectors.ghost.client import GhostApiError
from jhin_connectors.ghost.setup import GhostBindInput, bind_ghost
from jhin_db.models import Agent, Connection, Message, Task, User, WorkspaceMembership
from jhin_domain import new_uuid7
from jhin_policy import Grant, GrantEffect
from jhin_secrets.authority import attest_human_content
from jhin_secrets.variables import VariableActor, VariableStore

KEY = "a" * 24 + ":" + "b" * 64


@pytest.mark.parametrize(
    "phrase",
    [
        "Ashley (our Marketing Director) is the designated publisher.",
        "Ashley is the sole publisher.",
        "Ashley is our only publisher.",
    ],
)
def test_explicit_named_publisher_designation(phrase):
    from jhin_connectors.ghost.setup import _publisher_confirmed

    assert _publisher_confirmed([phrase], "Ashley")


@pytest.fixture
async def setup(context, workspace, monkeypatch):
    db = context.session
    user = User(email="owner@example.invalid", display_name="Owner", password_hash="fixture")
    author = Agent(id=context.agent_id, workspace_id=workspace.id, name="Blogger", slug="blogger")
    director = Agent(workspace_id=workspace.id, name="Marketing Director", slug="director")
    db.add_all([user, author, director])
    await db.flush()
    member = WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner")
    task = Task(
        id=context.task_id,
        workspace_id=workspace.id,
        title="Connect Ghost",
        assigned_agent_id=author.id,
        correlation_id=new_uuid7(),
    )
    db.add_all([member, task])
    await db.flush()
    message = Message(
        workspace_id=workspace.id,
        task_id=task.id,
        sender_type="user",
        sender_id=user.id,
        recipient_type="agent",
        recipient_id=author.id,
        content_json=attest_human_content(
            {"text": "Connect Ghost at http://ghost:2368. Only Marketing Director may publish."},
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        ),
    )
    db.add(message)
    store = VariableStore(db, context.crypto)
    variable = await store.set(
        VariableActor(workspace.id, "user", user.id, is_admin=True),
        scope="company",
        scope_id=workspace.id,
        name="GHOST_ADMIN_KEY",
        sensitive=True,
        value=KEY,
    )
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368,http://other:2368")
    seen = []

    async def request(base, key, method, path, **kw):
        assert key == KEY
        seen.append((base, method, path))
        return {"posts": []}

    monkeypatch.setattr("jhin_connectors.ghost.setup.ghost_request", request)
    return context, variable, director, message, member, seen


async def test_binding_uses_secret_internally_without_new_grants(setup):
    ctx, variable, director, _, _, seen = setup
    result = await bind_ghost(
        ctx,
        GhostBindInput(
            variable_id=variable.id, admin_url="http://ghost:2368", publisher_agent_id=director.id
        ),
    )
    assert result.verified and seen == [("http://ghost:2368", "GET", "posts/")]
    assert KEY not in result.model_dump_json()
    connection = await ctx.session.get(Connection, result.connection_id)
    assert KEY not in str(connection.config_json)
    assert connection.encrypted_secret_id is None and connection.last_verified_at
    assert result.verified_memory_facts
    default = [
        Grant(
            capability="ghost.post.read",
            effect=GrantEffect.ALLOW,
            scope={"variable_audience": True},
        )
    ]
    assert await ghost_access_allowed(ctx, connection, "ghost.post.read", default)
    assert not await ghost_access_allowed(
        ctx,
        connection,
        "ghost.post.publish",
        [
            Grant(
                capability="ghost.post.publish",
                effect=GrantEffect.ALLOW,
                scope={"variable_audience": True},
            )
        ],
    )


async def test_same_named_private_variables_with_shared_uuid_prefix_bind_independently(
    setup, monkeypatch
):
    from dataclasses import replace
    from uuid import UUID

    ctx, _company_variable, _director, _message, member, _seen = setup
    db = ctx.session
    peer = Agent(workspace_id=ctx.workspace_id, name="Second Blogger", slug="second-blogger")
    db.add(peer)
    await db.flush()
    task = Task(
        workspace_id=ctx.workspace_id,
        assigned_agent_id=peer.id,
        title="Connect Ghost",
        correlation_id=new_uuid7(),
    )
    db.add(task)
    await db.flush()
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            sender_type="user",
            sender_id=member.user_id,
            recipient_type="agent",
            recipient_id=peer.id,
            content_json=attest_human_content(
                {"text": "Connect Ghost at http://ghost:2368."},
                workspace_id=ctx.workspace_id,
                user_id=member.user_id,
                role="owner",
            ),
        )
    )
    store = VariableStore(db, ctx.crypto)
    owner = VariableActor(ctx.workspace_id, "user", member.user_id, is_admin=True)
    identities = iter(
        [
            UUID("01900000-1111-7000-8000-000000000001"),
            UUID("01900000-1111-7000-8000-000000000002"),
        ]
    )
    with monkeypatch.context() as scoped:
        scoped.setattr("jhin_secrets.variables.new_uuid7", lambda: next(identities))
        first = await store.set(
            owner,
            scope="agent",
            scope_id=ctx.agent_id,
            name="BLOG_TOKEN",
            sensitive=True,
            value=KEY,
        )
        second = await store.set(
            owner, scope="agent", scope_id=peer.id, name="BLOG_TOKEN", sensitive=True, value=KEY
        )
    assert str(first.id)[:8] == str(second.id)[:8]
    peer_ctx = replace(ctx, agent_id=peer.id, agent_name=peer.name, task_id=task.id)
    first_input = GhostBindInput(variable_id=first.id, admin_url="http://ghost:2368")
    second_input = GhostBindInput(variable_id=second.id, admin_url="http://ghost:2368")
    a = await bind_ghost(ctx, first_input)
    b = await bind_ghost(peer_ctx, second_input)
    assert a.connection_id != b.connection_id and a.name != b.name
    assert (await bind_ghost(ctx, first_input)).connection_id == a.connection_id
    assert (await bind_ghost(peer_ctx, second_input)).connection_id == b.connection_id
    rows = list(await db.scalars(select(Connection)))
    assert len(rows) == 2
    assert {row.config_json["admin_key_variable_id"] for row in rows} == {
        str(first.id),
        str(second.id),
    }


async def test_rebinding_after_variable_or_connection_rename_preserves_connection_identity(setup):
    ctx, variable, _director, _message, member, _seen = setup
    data = GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
    original = await bind_ghost(ctx, data)
    row = await ctx.session.get(Connection, original.connection_id)
    row.name = "Existing editorial connection"
    await VariableStore(ctx.session, ctx.crypto).set(
        VariableActor(ctx.workspace_id, "user", member.user_id, is_admin=True),
        variable_id=variable.id,
        expected_version=1,
        name="RENAMED_GHOST_KEY",
    )
    repeated = await bind_ghost(ctx, data)
    assert repeated.connection_id == original.connection_id
    assert repeated.name == row.name == "Existing editorial connection"
    assert len(list(await ctx.session.scalars(select(Connection)))) == 1


async def test_legacy_duplicate_variable_connections_fail_without_provider_call_or_mutation(setup):
    from copy import deepcopy

    ctx, variable, _director, _message, _member, seen = setup
    data = GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
    created = await bind_ghost(ctx, data)
    original = await ctx.session.get(Connection, created.connection_id)
    duplicate = Connection(
        workspace_id=ctx.workspace_id,
        connector_type="ghost",
        name="Legacy renamed duplicate",
        auth_type="api_key",
        status="disabled",
        config_json=deepcopy(original.config_json),
    )
    ctx.session.add(duplicate)
    await ctx.session.flush()

    def snapshot(row):
        return {
            "id": row.id,
            "name": row.name,
            "status": row.status,
            "config": deepcopy(row.config_json),
            "last_verified_at": row.last_verified_at,
            "last_error": row.last_error,
        }

    before = [snapshot(original), snapshot(duplicate)]
    seen.clear()
    with pytest.raises(GhostApiError) as refused:
        await bind_ghost(ctx, data)
    assert refused.value.code == "ghost_binding_conflict"
    assert seen == []
    await ctx.session.flush()
    assert [snapshot(original), snapshot(duplicate)] == before
    assert len(list(await ctx.session.scalars(select(Connection)))) == 2


async def test_unconfirmed_url_never_receives_key(setup):
    ctx, variable, director, _, _, seen = setup
    with pytest.raises(GhostApiError, match="no supplied Admin URL"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://other:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert seen == []
    assert await ctx.session.scalar(select(Connection.id)) is None


async def test_agent_text_cannot_supply_human_setup_authority(setup):
    ctx, variable, director, message, _, seen = setup
    message.sender_type = "agent"
    with pytest.raises(GhostApiError, match="direct request"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert seen == []


async def test_current_admin_role_is_required(setup):
    ctx, variable, director, _, member, seen = setup
    member.role = "member"
    with pytest.raises(GhostApiError, match="current workspace admin"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert seen == []


async def test_publisher_must_be_named_by_human(setup):
    ctx, variable, director, message, _, seen = setup
    message.content_json = {**message.content_json, "text": "Connect Ghost at http://ghost:2368"}
    with pytest.raises(GhostApiError, match="must name"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert seen == []


async def test_draft_only_setup_needs_no_publisher(setup):
    ctx, variable, _, _, _, _ = setup
    result = await bind_ghost(
        ctx, GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
    )
    assert result.publisher_agent_id is None and result.verified
    assert "Drafts only" in result.detail and "no publishing agent" in result.detail


@pytest.mark.parametrize("action", ["reconnecting", "verifying"])
async def test_recheck_explicit_existing_ghost_connection_keeps_identity_and_drafts_only(
    setup, action
):
    ctx, variable, _, message, _, seen = setup
    data = GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
    original = await bind_ghost(ctx, data)
    seen.clear()
    message.content_json = {
        **message.content_json,
        "text": f"Confirm the original is intact by {action} its existing Ghost connection "
        "at http://ghost:2368. This private connection is Drafts only.",
    }
    await ctx.session.flush()
    result = await bind_ghost(ctx, data)
    assert result.verified and result.connection_id == original.connection_id
    assert result.publisher_agent_id is None and "Drafts only" in result.detail
    assert seen == [("http://ghost:2368", "GET", "posts/")]
    assert KEY not in result.model_dump_json()
    assert len(list(await ctx.session.scalars(select(Connection)))) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Never send the Ghost key to http://ghost:2368.",
        "Do not connect Ghost at http://ghost:2368.",
        "> Connect Ghost at http://ghost:2368.",
        "Ghost documentation example: http://ghost:2368.",
        "Do not reconnect its existing Ghost connection at http://ghost:2368.",
        '"Verify the existing Ghost connection at http://ghost:2368."',
        "Maybe verify its existing Ghost connection at http://ghost:2368.",
        "Reconnect its existing Ghost connection at http://ghost:2368 or http://other:2368.",
    ],
)
async def test_mention_or_negative_url_does_not_authorize_setup(setup, text):
    ctx, variable, _, message, _, seen = setup
    message.content_json = {**message.content_json, "text": text}
    with pytest.raises(GhostApiError, match="no supplied Admin URL"):
        await bind_ghost(
            ctx, GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
        )
    assert not seen


@pytest.mark.parametrize(
    "phrase",
    [
        "Marketing Director must not publish.",
        "Do not let Marketing Director publish.",
        "Marketing Director cannot publish.",
        "I asked Marketing Director whether Blogger should publish.",
        "> Only Marketing Director may publish.",
        'The docs say "Only Marketing Director may publish."',
        "Example: Only Marketing Director may publish.",
        "`Only Marketing Director may publish.`",
    ],
)
async def test_negative_or_quoted_publisher_is_not_authority(setup, phrase):
    ctx, variable, director, message, _, seen = setup
    message.content_json = {
        **message.content_json,
        "text": "Connect Ghost at http://ghost:2368.\n" + phrase,
    }
    with pytest.raises(GhostApiError, match="must name"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert not seen


async def test_failed_verification_keeps_honest_connection_state(setup, monkeypatch):
    ctx, variable, _, _, _, _ = setup

    async def fail(*args, **kwargs):
        raise GhostApiError("Ghost refused authentication", status_code=401)

    monkeypatch.setattr("jhin_connectors.ghost.setup.ghost_request", fail)
    with pytest.raises(GhostApiError):
        await bind_ghost(
            ctx, GhostBindInput(variable_id=variable.id, admin_url="http://ghost:2368")
        )
    connection = await ctx.session.scalar(select(Connection))
    assert connection.status == "error" and connection.last_verified_at


@pytest.mark.parametrize("ceiling", ["legacy", "member", "chats_only"])
async def test_unattested_or_limited_request_cannot_bind(setup, ceiling):
    ctx, variable, director, message, _, seen = setup
    content = dict(message.content_json)
    if ceiling == "legacy":
        content.pop("_human_authority")
    elif ceiling == "member":
        content["_human_authority"] = {**content["_human_authority"], "role": "member"}
    else:
        content["_human_authority"] = {
            **content["_human_authority"],
            "source": "api_key",
            "api_key_id": str(new_uuid7()),
            "scopes": ["chats:write"],
        }
    message.content_json = content
    with pytest.raises(GhostApiError, match="no supplied Admin URL"):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert not seen


@pytest.mark.parametrize("attested", [False, True])
async def test_required_answers_need_effective_human_authority(setup, attested):
    ctx, variable, director, message, _, seen = setup
    message.content_json = {**message.content_json, "text": "Please connect my Ghost key"}
    task = await ctx.session.get(Task, ctx.task_id)
    proof = {"user_id": str(message.sender_id)}
    if attested:
        proof["_human_authority"] = message.content_json["_human_authority"]
    task.metadata_json = {
        "resolved_inputs": {
            "ghost_admin_url": "http://ghost:2368",
            "ghost_publisher_agent_id": director.name,
        },
        "resolved_input_authority": {"ghost_admin_url": proof, "ghost_publisher_agent_id": proof},
    }
    payload = GhostBindInput(
        variable_id=variable.id, admin_url="http://ghost:2368", publisher_agent_id=director.id
    )
    if attested:
        result = await bind_ghost(ctx, payload)
        assert result.verified and seen
    else:
        with pytest.raises(GhostApiError, match="no supplied Admin URL"):
            await bind_ghost(ctx, payload)
        assert not seen


@pytest.mark.parametrize("revocation", ["url", "publisher"])
async def test_resolved_answer_cannot_override_human_revocation(setup, revocation):
    ctx, variable, director, message, _, seen = setup
    task = await ctx.session.get(Task, ctx.task_id)
    proof = {
        "user_id": str(message.sender_id),
        "_human_authority": message.content_json["_human_authority"],
    }
    task.metadata_json = {
        "resolved_inputs": {
            "ghost_admin_url": "http://ghost:2368",
            "ghost_publisher_agent_id": director.name,
        },
        "resolved_input_authority": {"ghost_admin_url": proof, "ghost_publisher_agent_id": proof},
    }
    text = (
        "Never send the key to http://ghost:2368/ghost/"
        if revocation == "url"
        else "Marketing Director must not publish."
    )
    message.content_json = {**message.content_json, "text": text}
    with pytest.raises(GhostApiError):
        await bind_ghost(
            ctx,
            GhostBindInput(
                variable_id=variable.id,
                admin_url="http://ghost:2368",
                publisher_agent_id=director.id,
            ),
        )
    assert not seen


async def test_default_audience_grant_cannot_access_manual_connection(setup):
    ctx, _, _, _, _, _ = setup
    conn = Connection(
        workspace_id=ctx.workspace_id,
        connector_type="ghost",
        name="Manual",
        auth_type="api_key",
        status="active",
        config_json={"admin_url": "http://ghost:2368"},
    )
    ctx.session.add(conn)
    await ctx.session.flush()
    default = Grant(
        capability="ghost.post.read", effect=GrantEffect.ALLOW, scope={"variable_audience": True}
    )
    assert not await ghost_access_allowed(ctx, conn, "ghost.post.read", [default])
    pinned = Grant(
        capability="ghost.post.read",
        effect=GrantEffect.ALLOW,
        scope={"connection_id": str(conn.id)},
    )
    assert await ghost_access_allowed(ctx, conn, "ghost.post.read", [pinned])
    assert not await ghost_access_allowed(
        ctx,
        conn,
        "ghost.post.read",
        [
            pinned,
            Grant(
                capability="ghost.*", effect=GrantEffect.DENY, scope={"connection_id": str(conn.id)}
            ),
        ],
    )
