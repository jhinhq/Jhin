"""Shared variable writes require current human authority, never agent assertions."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from jhin_db.models import Message, Task, User, WorkspaceMembership
from jhin_db.models.variables import ScopedVariable
from jhin_domain import new_uuid7
from jhin_secrets.authority import attest_human_content


async def test_own_variable_tools_return_plaintext_and_preserve_cas(context):
    from jhin_tools.variables import VariableGetInput, VariableSetInput, get_variable, set_variable

    result = await set_variable(context, VariableSetInput(name="blog.tone", value="Friendly"))
    assert result.item["value"] == "Friendly"
    read = await get_variable(context, VariableGetInput(variable_id=result.item["id"]))
    assert read.item == result.item
    updated = await set_variable(
        context, VariableSetInput(variable_id=result.item["id"], expected_version=1, value="Direct")
    )
    assert updated.item["value"] == "Direct" and updated.item["version"] == 2
    with pytest.raises(Exception, match="version"):
        await set_variable(
            context,
            VariableSetInput(variable_id=result.item["id"], expected_version=9, value="Stale"),
        )


async def test_company_scope_needs_current_admin_user_message(context):
    from jhin_tools.variables import VariableSetInput, set_variable

    db = context.session
    task = Task(
        id=context.task_id,
        workspace_id=context.workspace_id,
        assigned_agent_id=context.agent_id,
        title="Set tone",
        correlation_id=new_uuid7(),
    )
    user = User(email="owner@example.test", display_name="Owner", password_hash="unused")
    db.add_all([task, user])
    await db.flush()
    member = WorkspaceMembership(workspace_id=context.workspace_id, user_id=user.id, role="admin")
    db.add(member)
    message = Message(
        workspace_id=context.workspace_id,
        task_id=task.id,
        sender_type="agent",
        sender_id=context.agent_id,
        recipient_type="agent",
        recipient_id=context.agent_id,
        message_type="text",
        content_json=attest_human_content(
            {"text": "Save the blog tone for the whole company."},
            workspace_id=context.workspace_id,
            user_id=user.id,
            role="admin",
        ),
        visibility="visible",
    )
    db.add(message)
    await db.flush()
    payload = VariableSetInput(name="blog.tone", scope="company", value="Friendly")
    with pytest.raises(Exception, match="authority"):
        await set_variable(context, payload)
    message.sender_type, message.sender_id = "user", user.id
    await db.flush()
    result = await set_variable(context, payload)
    assert result.item["scope"] == "company"
    member.role = "member"
    await db.flush()
    with pytest.raises(Exception, match="authority"):
        await set_variable(
            context,
            VariableSetInput(variable_id=result.item["id"], expected_version=1, value="Bad"),
        )
    assert (await db.scalar(select(ScopedVariable))).plaintext == "Friendly"
    with pytest.raises(Exception, match="authority"):
        await set_variable(
            replace(context, task_id=new_uuid7()),
            VariableSetInput(name="other", scope="company", value="No"),
        )


async def test_scoped_grants_and_denies_cover_id_based_updates(context):
    from jhin_policy import Grant, GrantEffect
    from jhin_tools.variables import VariableSetInput, set_variable, validate_variable_write

    target = {"scope": "company", "scope_id": str(context.workspace_id)}
    allow = Grant(capability="variables.write", scope=target)
    authorized = replace(context, authorizing_grants=(allow,))
    create = VariableSetInput(name="shared.tone", scope="company", value="Friendly")
    assert await validate_variable_write(context, create, [allow]) is None
    result = await set_variable(authorized, create)
    change = VariableSetInput(variable_id=result.item["id"], expected_version=1, value="Concise")
    assert await validate_variable_write(context, change, [allow]) is None
    denied = Grant(capability="variables.write", scope=target, effect=GrantEffect.DENY)
    refusal = await validate_variable_write(context, change, [allow, denied])
    assert refusal is not None and "explicitly denied" in refusal.reason
    other = Grant(
        capability="variables.write", scope={"scope": "team", "scope_id": str(new_uuid7())}
    )
    assert await validate_variable_write(context, change, [other]) is not None


@pytest.mark.parametrize(
    "text,company,team",
    [
        ("Do not share this key with the company", False, False),
        ("Never delete Marketing variables", False, False),
        ("Don't share company-wide; keep for Marketing", False, True),
        ('Someone said "share this key with the company"', False, False),
        ("> Share this key with the company", False, False),
        ("Maybe store company-wide or for Marketing", False, False),
        ("Share this key team-wide", False, True),
        ("Save this value team wide", False, True),
        ("Save this value company-wide", True, False),
        ("Create this setting company-wide", True, False),
        ("Create this setting for Marketing", False, True),
        ("Never create this setting company-wide", False, False),
        ('Someone said "create this setting company-wide"', False, False),
    ],
)
async def test_shared_scope_requires_positive_unquoted_current_instruction(
    context, text, company, team
):
    from jhin_db.models import Agent, AgentTeamMembership, Team
    from jhin_tools.variables import human_scope_authorized

    db = context.session
    target = Team(workspace_id=context.workspace_id, name="Marketing")
    other = Team(workspace_id=context.workspace_id, name="Engineering")
    user = User(email="admin@example.test", display_name="Admin", password_hash="unused")
    task = Task(
        id=context.task_id,
        workspace_id=context.workspace_id,
        title="Settings",
        correlation_id=new_uuid7(),
    )
    db.add_all([target, other, user, task])
    await db.flush()
    db.add_all(
        [
            WorkspaceMembership(workspace_id=context.workspace_id, user_id=user.id, role="admin"),
            AgentTeamMembership(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                team_id=target.id,
                is_primary=True,
            ),
        ]
    )
    (await db.get(Agent, context.agent_id)).team_id = target.id
    db.add(
        Message(
            workspace_id=context.workspace_id,
            task_id=task.id,
            sender_type="user",
            sender_id=user.id,
            recipient_type="agent",
            recipient_id=context.agent_id,
            message_type="text",
            visibility="visible",
            content_json=attest_human_content(
                {"text": text}, workspace_id=context.workspace_id, user_id=user.id, role="admin"
            ),
        )
    )
    await db.flush()
    assert await human_scope_authorized(context, "company", context.workspace_id) is company
    assert await human_scope_authorized(context, "team", target.id) is team
    assert not await human_scope_authorized(context, "team", other.id)


async def test_later_prohibition_revokes_earlier_shared_request(context):
    from jhin_tools.variables import human_scope_authorized

    user = User(email="admin@example.test", display_name="Admin", password_hash="unused")
    task = Task(
        id=context.task_id,
        workspace_id=context.workspace_id,
        title="Settings",
        correlation_id=new_uuid7(),
    )
    context.session.add_all([user, task])
    await context.session.flush()
    context.session.add(
        WorkspaceMembership(workspace_id=context.workspace_id, user_id=user.id, role="admin")
    )
    for text in ("Share company-wide", "Do not share company-wide"):
        context.session.add(
            Message(
                workspace_id=context.workspace_id,
                task_id=task.id,
                sender_type="user",
                sender_id=user.id,
                recipient_type="agent",
                recipient_id=context.agent_id,
                message_type="text",
                visibility="visible",
                content_json=attest_human_content(
                    {"text": text}, workspace_id=context.workspace_id, user_id=user.id, role="admin"
                ),
            )
        )
        await context.session.flush()
    assert not await human_scope_authorized(context, "company", context.workspace_id)


async def test_empty_variable_list_discovers_current_scopes_without_write_authority(context):
    from datetime import UTC, datetime

    from jhin_db.models import Agent, AgentTeamMembership, Team, Workspace
    from jhin_tools.variables import (
        VariableListInput,
        VariableSetInput,
        list_variables,
        set_variable,
    )

    db = context.session
    workspace = await db.get(Workspace, context.workspace_id)
    agent = await db.get(Agent, context.agent_id)
    current = Team(workspace_id=context.workspace_id, name="Readiness Marketing")
    departed = Team(workspace_id=context.workspace_id, name="Former team")
    unrelated = Team(workspace_id=context.workspace_id, name="Unrelated team")
    other_workspace = Workspace(name="Other company", slug="other-company")
    db.add_all([current, departed, unrelated, other_workspace])
    await db.flush()
    foreign = Team(workspace_id=other_workspace.id, name="Foreign team")
    db.add(foreign)
    await db.flush()
    agent.team_id = departed.id
    db.add_all(
        [
            AgentTeamMembership(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                team_id=current.id,
                is_primary=True,
            ),
            AgentTeamMembership(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                team_id=departed.id,
                left_at=datetime.now(UTC),
            ),
            # Even corrupt legacy membership data cannot expose another workspace.
            AgentTeamMembership(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                team_id=foreign.id,
            ),
        ]
    )
    await db.flush()
    result = await list_variables(context, VariableListInput(scope="company"))
    assert result.items == []
    assert result.model_dump(mode="json")["accessible_scopes"] == [
        {"scope": "agent", "scope_id": str(agent.id), "name": agent.name},
        {"scope": "team", "scope_id": str(current.id), "name": current.name},
        {"scope": "company", "scope_id": str(workspace.id), "name": workspace.name},
    ]
    assert result.accessible_scopes_truncated is False
    with pytest.raises(Exception, match="authority"):
        await set_variable(
            context,
            VariableSetInput(
                name="shared.tone", scope="team", scope_id=current.id, value="Friendly"
            ),
        )


async def test_scope_discovery_is_bounded_and_can_target_an_authorized_team(context):
    from jhin_db.models import AgentTeamMembership, Team
    from jhin_tools.variables import VariableListInput, list_variables

    teams = [Team(workspace_id=context.workspace_id, name=f"Team {i:03}") for i in range(101)]
    context.session.add_all(teams)
    await context.session.flush()
    context.session.add_all(
        [
            AgentTeamMembership(
                workspace_id=context.workspace_id, agent_id=context.agent_id, team_id=team.id
            )
            for team in teams
        ]
    )
    await context.session.flush()
    result = await list_variables(context, VariableListInput())
    assert len(result.accessible_scopes) == 102
    assert result.accessible_scopes_truncated is True
    assert [row.scope_id for row in result.accessible_scopes if row.scope == "team"] == [
        team.id for team in teams[:100]
    ]
    targeted = await list_variables(context, VariableListInput(scope="team", scope_id=teams[-1].id))
    assert targeted.items == []
    assert [row.scope_id for row in targeted.accessible_scopes if row.scope == "team"] == [
        teams[-1].id
    ]
    assert targeted.accessible_scopes_truncated is False


@pytest.mark.parametrize("inactive", [False, True])
async def test_scope_discovery_rejects_missing_or_inactive_caller(context, inactive):
    from jhin_db.models import Agent
    from jhin_tools.variables import VariableListInput, list_variables

    if inactive:
        (await context.session.get(Agent, context.agent_id)).status = "paused"
        await context.session.flush()
    else:
        context = replace(context, agent_id=new_uuid7())
    with pytest.raises(Exception, match="Variable not found"):
        await list_variables(context, VariableListInput())


async def test_discovered_scopes_support_sensitive_copy_chain_without_revealing_value(context):
    from jhin_db.models import Agent, AgentTeamMembership, Team
    from jhin_policy import Grant
    from jhin_secrets import MasterKey, SecretCrypto
    from jhin_secrets.variables import VariableActor, VariableStore
    from jhin_tools.variables import (
        VariableCopyInput,
        VariableGetInput,
        VariableListInput,
        copy_variable,
        get_variable,
        list_variables,
    )

    ctx = replace(context, crypto=SecretCrypto(MasterKey(key=b"d" * 32)))
    team = Team(workspace_id=ctx.workspace_id, name="Readiness Marketing")
    director = Agent(workspace_id=ctx.workspace_id, name="Director", slug="director")
    owner = User(email="scope-owner@example.test", display_name="Owner", password_hash="unused")
    ctx.session.add_all([team, director, owner])
    await ctx.session.flush()
    ctx.session.add_all(
        [
            AgentTeamMembership(workspace_id=ctx.workspace_id, agent_id=agent_id, team_id=team.id)
            for agent_id in (ctx.agent_id, director.id)
        ]
    )
    await ctx.session.flush()
    secret = "ab" * 12 + ":" + "cd" * 32
    source = await VariableStore(ctx.session, ctx.crypto).set(
        VariableActor(ctx.workspace_id, "user", owner.id, is_admin=True),
        name="ghost.key",
        scope="agent",
        scope_id=ctx.agent_id,
        sensitive=True,
        value=secret,
    )
    discovery = await list_variables(ctx, VariableListInput())
    forbidden = {"value", "plaintext", "secret_id", "masked_hint", "ciphertext", "nonce"}
    assert secret not in discovery.model_dump_json()
    assert forbidden.isdisjoint(discovery.items[0])
    for scope in discovery.model_dump(mode="json")["accessible_scopes"]:
        assert set(scope) == {"scope", "scope_id", "name"}
    previous = source
    copies = []
    for destination in [scope for scope in discovery.accessible_scopes if scope.scope != "agent"]:
        allowed = replace(
            ctx,
            authorizing_grants=(
                Grant(
                    capability="variables.write",
                    scope={"scope": destination.scope, "scope_id": str(destination.scope_id)},
                ),
            ),
        )
        payload = VariableCopyInput(
            source_variable_id=previous.id,
            expected_version=previous.version,
            scope=destination.scope,
            scope_id=destination.scope_id,
        )
        copied = await copy_variable(allowed, payload)
        assert copied.item["source_variable_id"] == previous.id
        assert copied.item["source_version"] == previous.version
        assert copied.item["version"] == 1 and copied.item["id"] != previous.id
        assert copied.item["configured"] is True and copied.item["sensitive"] is True
        assert forbidden.isdisjoint(copied.item) and secret not in copied.model_dump_json()
        assert (await copy_variable(allowed, payload)).item["id"] == copied.item["id"]
        previous = await ctx.session.get(ScopedVariable, copied.item["id"])
        copies.append(previous)
    assert source.scope == "agent" and source.version == 1
    assert len({source.secret_id, *(row.secret_id for row in copies)}) == 3
    director_ctx = replace(ctx, agent_id=director.id, agent_name=director.name)
    with pytest.raises(Exception, match="Variable not found"):
        await get_variable(director_ctx, VariableGetInput(variable_id=source.id))
    for copied in copies:
        output = await get_variable(director_ctx, VariableGetInput(variable_id=copied.id))
        assert output.item["id"] == copied.id
        assert forbidden.isdisjoint(output.item)
