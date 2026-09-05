"""``jhin-admin``, the console half of account administration.

These commands are the answer to "nobody can sign in" and to "first-run setup
is closed and I need another account", so the things they must never do are
specific: hand back a half-made workspace, take a password off the command
line, or call an install healthy while its schema is behind the code.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jhin_api.cli.commands import COMMANDS
from jhin_api.cli.main import main
from jhin_api.cli.parser import PROGRAM, build_parser
from jhin_api.cli.runtime import CommandError, Result, Runtime, emit
from jhin_api.connections import service as connections_service
from jhin_api.deps import WorkspaceContext
from jhin_api.security.passwords import verify_password
from jhin_api.security.tokens import hash_token
from jhin_db.base import Base
from jhin_db.migrate import alembic_config
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AuditEvent,
    Connection,
    Skill,
    User,
    UserSession,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_secrets import SecretCrypto
from jhin_secrets.crypto import (
    MasterKey,
    decode_master_key_material,
    generate_master_key_material,
)
from jhin_skills import load_builtin_skills

# Obviously fake and local to this module: long enough for the account policy,
# used nowhere but here.
PASSWORD = "unit-test-passphrase-9182"
REPLACEMENT_PASSWORD = "unit-test-second-passphrase-77"

OWNER_EMAIL = "operator@example.com"
OWNER_ARGV = (
    "owner",
    "create",
    "--email",
    "OPERATOR@example.com",
    "--name",
    "Ops",
    "--workspace",
    "Acme HQ",
    "--password-stdin",
)


@dataclass
class Console:
    """One ``jhin-admin`` invocation against the in-memory database.

    The same SQLite the shared ``session`` fixture builds, kept alongside its
    engine because ``doctor`` reads the connection itself — the Alembic
    revision a database is stamped with does not live in the ORM.
    """

    session: AsyncSession
    engine: AsyncEngine
    monkeypatch: pytest.MonkeyPatch

    async def run(self, *argv: str, stdin: str = "") -> Result:
        args = build_parser().parse_args(argv)
        # A StringIO is not a terminal, so this is also the automation path:
        # prompts refuse to run and confirmations do not block.
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        return await COMMANDS[args.command](
            Runtime(
                args=args,
                db=self.session,
                engine=self.engine,
                # Only the migration scripts are read from this; the revision
                # the database carries is read off the engine.
                database_url="sqlite://",
                request_id=new_uuid7(),
            )
        )

    async def bootstrap(self) -> Result:
        return await self.run(*OWNER_ARGV, stdin=PASSWORD)

    async def count(self, model: type[Base]) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
async def console(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Console]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield Console(session=session, engine=engine, monkeypatch=monkeypatch)
    await engine.dispose()


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Every flag the CLI accepts, including the ones on its subcommands."""
    options = {option for action in parser._actions for option in action.option_strings}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                options |= _option_strings(subparser)
    return options


async def test_owner_create_builds_the_workspace_the_setup_page_would(console: Console) -> None:
    result = await console.bootstrap()

    user = await console.session.scalar(select(User))
    assert user is not None
    # Sign-in lowercases the address before it looks anyone up, so a stray
    # capital here would be an account nobody could ever reach.
    assert user.email == OWNER_EMAIL
    assert verify_password(user.password_hash, PASSWORD)

    membership = await console.session.scalar(select(WorkspaceMembership))
    assert membership is not None
    assert membership.role == WorkspaceRole.OWNER.value
    assert result.data["workspace"]["slug"] == "acme-hq"
    # The starter skills are the step a hand-written INSERT forgets: the
    # workspace loads either way, but its Skills page stays empty forever.
    assert await console.count(Skill) == len(load_builtin_skills())
    # Nothing here is holding a session token, so none was minted.
    assert await console.count(UserSession) == 0


@pytest.mark.parametrize(
    ("field", "argv", "expected"),
    (
        ("email", ("--email", "not an email at all"), "not a valid email address"),
        ("name", ("--name", "   "), "cannot be empty"),
        ("workspace", ("--workspace", ""), "cannot be empty"),
    ),
)
async def test_owner_create_refuses_what_the_setup_form_would(
    console: Console, field: str, argv: tuple[str, ...], expected: str
) -> None:
    """argv is untyped where the HTTP schemas are not, and this command runs
    exactly once: it closes first-run setup behind itself, so an address nobody
    can sign in with would strand the install with no second chance."""
    replacements = {"--email": "someone@example.com", "--name": "Ops", "--workspace": "Acme HQ"}
    replacements[argv[0]] = argv[1]
    with pytest.raises(CommandError) as caught:
        await console.run(
            "owner",
            "create",
            *[part for option, value in replacements.items() for part in (option, value)],
            "--password-stdin",
            stdin=PASSWORD,
        )

    assert expected in str(caught.value)
    assert await console.count(User) == 0
    assert await console.count(Workspace) == 0


async def test_user_create_refuses_an_address_sign_in_could_never_match(
    console: Console,
) -> None:
    await console.bootstrap()

    with pytest.raises(CommandError) as caught:
        await console.run(
            "user",
            "create",
            "--email",
            "who?",
            "--name",
            "Newcomer",
            "--workspace",
            "acme-hq",
            "--role",
            "member",
            "--yes",
            "--password-stdin",
            stdin=PASSWORD,
        )

    assert "not a valid email address" in str(caught.value)
    assert await console.count(User) == 1


async def test_owner_create_refuses_once_an_account_exists(console: Console) -> None:
    await console.bootstrap()

    with pytest.raises(CommandError) as caught:
        await console.run(
            "owner",
            "create",
            "--email",
            "second@example.com",
            "--name",
            "Second",
            "--workspace",
            "Another",
            "--password-stdin",
            stdin=PASSWORD,
        )

    # The same rule the HTTP endpoint follows, and the way out of it.
    assert "user create" in str(caught.value)
    assert await console.count(User) == 1


async def test_user_create_joins_an_existing_workspace_in_the_role_asked_for(
    console: Console,
) -> None:
    await console.bootstrap()

    result = await console.run(
        "user",
        "create",
        "--email",
        "colleague@example.com",
        "--name",
        "Colleague",
        "--workspace",
        "acme-hq",
        "--role",
        "admin",
        "--password-stdin",
        stdin=PASSWORD,
    )

    assert result.data["role"] == "admin"
    user = await console.session.scalar(select(User).where(User.email == "colleague@example.com"))
    assert user is not None
    assert verify_password(user.password_hash, PASSWORD)
    membership = await console.session.scalar(
        select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
    )
    assert membership is not None
    assert membership.role == WorkspaceRole.ADMIN.value


async def test_user_create_rejects_an_address_that_already_has_an_account(
    console: Console,
) -> None:
    await console.bootstrap()

    with pytest.raises(CommandError) as caught:
        await console.run(
            "user",
            "create",
            "--email",
            OWNER_EMAIL,
            "--name",
            "Twice",
            "--workspace",
            "acme-hq",
            "--role",
            "member",
            "--password-stdin",
            stdin=PASSWORD,
        )

    assert "already has an account" in str(caught.value)
    assert await console.count(User) == 1


@pytest.mark.parametrize(
    ("weak", "expected"),
    [
        ("short-one", "at least 12 characters"),
        ("passwordpassword", "most commonly guessed"),
    ],
)
async def test_a_weak_password_is_refused_in_the_policy_s_own_words(
    console: Console, weak: str, expected: str
) -> None:
    with pytest.raises(CommandError) as caught:
        await console.run(*OWNER_ARGV, stdin=weak)

    assert expected in str(caught.value)
    assert await console.count(User) == 0


def test_no_command_takes_a_password_on_the_command_line() -> None:
    """argv is readable by every process on the host and lands in shell
    history, so the only password-shaped flag in the whole CLI is the one that
    says the value is arriving somewhere else."""
    password_flags = {option for option in _option_strings(build_parser()) if "password" in option}

    assert password_flags == {"--password-stdin"}


async def test_set_password_replaces_the_hash_and_revokes_every_session(
    console: Console,
) -> None:
    await console.bootstrap()
    user = await console.session.scalar(select(User))
    assert user is not None
    console.session.add(
        UserSession(
            user_id=user.id,
            token_hash="fake-session-token-hash",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await console.session.flush()

    result = await console.run(
        "user",
        "set-password",
        "--email",
        OWNER_EMAIL,
        "--password-stdin",
        "--yes",
        stdin=REPLACEMENT_PASSWORD,
    )

    assert verify_password(user.password_hash, REPLACEMENT_PASSWORD)
    assert not verify_password(user.password_hash, PASSWORD)
    # A password change is the answer to "somebody else has my credentials",
    # so the sessions minted under the old one must not outlive it.
    assert result.data["revoked_sessions"] == 1
    session_record = await console.session.scalar(select(UserSession))
    assert session_record is not None
    assert session_record.revoked_at is not None


async def test_set_role_will_not_leave_a_workspace_without_an_owner(console: Console) -> None:
    await console.bootstrap()

    # The service refuses, and ``main`` turns that refusal into a sentence
    # rather than a traceback.
    with pytest.raises(HTTPException) as caught:
        await console.run(
            "user",
            "set-role",
            "--email",
            OWNER_EMAIL,
            "--workspace",
            "acme-hq",
            "--role",
            "member",
            "--yes",
        )

    assert "at least one owner" in str(caught.value.detail)


async def test_doctor_reports_pending_migrations_and_an_open_setup(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("MASTER_KEY", raising=False)

    # The schema here came from ``Base.metadata.create_all``, so nothing has
    # stamped a revision: the shape of an install whose migrations never ran.
    result = await console.run("doctor")

    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    assert result.data["migrations"] == {
        "ok": False,
        "current": None,
        "head": head,
        "pending": True,
    }
    assert result.data["setup_open"] is True
    assert result.data["counts"] == {"users": 0, "workspaces": 0, "agents": 0}
    assert result.data["master_key"]["ok"] is False
    assert result.exit_code == 1
    assert "jhin-db-migrate" in "\n".join(result.lines)


async def test_doctor_closes_setup_once_an_owner_exists_and_never_prints_the_key(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = generate_master_key_material()
    monkeypatch.delenv("MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("MASTER_KEY", material)
    await console.bootstrap()

    result = await console.run("doctor")

    assert result.data["setup_open"] is False
    assert result.data["counts"] == {"users": 1, "workspaces": 1, "agents": 0}
    # Whether it loaded, and nothing else about it.
    assert result.data["master_key"] == {"ok": True, "detail": None}
    assert material not in json.dumps(result.data) + "\n".join(result.lines)


async def test_invite_create_prints_the_link_and_stores_only_its_hash(console: Console) -> None:
    await console.bootstrap()

    result = await console.run(
        "invite",
        "create",
        "--email",
        "newcomer@example.com",
        "--workspace",
        "acme-hq",
        "--role",
        "member",
    )

    token = result.data["invite_url"].rsplit("/", 1)[-1]
    assert f"  {result.data['invite_url']}" in result.lines
    invitation = await console.session.scalar(select(WorkspaceInvitation))
    assert invitation is not None
    # Shown once, here; the database keeps nothing replayable.
    assert invitation.token_hash == hash_token(token)
    assert invitation.email == "newcomer@example.com"


async def test_json_output_parses_and_carries_what_a_script_needs(
    console: Console, capsys: pytest.CaptureFixture[str]
) -> None:
    await console.bootstrap()
    await console.run(
        "user",
        "create",
        "--email",
        "colleague@example.com",
        "--name",
        "Colleague",
        "--workspace",
        "acme-hq",
        "--role",
        "member",
        "--password-stdin",
        stdin=PASSWORD,
    )

    listed = await console.run("user", "list", "--workspace", "acme-hq", "--json")
    emit(listed, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace"]["slug"] == "acme-hq"
    assert [(user["email"], user["role"]) for user in payload["users"]] == [
        (OWNER_EMAIL, "owner"),
        ("colleague@example.com", "member"),
    ]

    workspaces = await console.run("workspace", "list", "--json")
    emit(workspaces, as_json=True)
    listing = json.loads(capsys.readouterr().out)
    assert listing["workspaces"][0]["members"] == 2


def test_a_bare_invocation_prints_help_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", [PROGRAM])

    main()

    assert f"usage: {PROGRAM}" in capsys.readouterr().out


async def test_an_unknown_workspace_is_a_sentence_not_a_stack_trace(console: Console) -> None:
    await console.bootstrap()

    with pytest.raises(CommandError) as caught:
        await console.run("user", "list", "--workspace", "no-such-workspace")

    assert "workspace list" in str(caught.value)


async def test_workspace_create_gives_the_new_workspace_its_starter_skills(
    console: Console,
) -> None:
    await console.bootstrap()
    before = await console.count(Skill)

    result = await console.run(
        "workspace", "create", "--name", "Second Team", "--owner", OWNER_EMAIL
    )

    workspace = await console.session.scalar(
        select(Workspace).where(Workspace.slug == result.data["workspace"]["slug"])
    )
    assert workspace is not None
    assert await console.count(Skill) == before + len(load_builtin_skills())
    membership = await console.session.scalar(
        select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id)
    )
    assert membership is not None
    assert membership.role == WorkspaceRole.OWNER.value


# --- agent: giving an agent an app from the console --------------------------

ENGINEER = "Senior Software Engineer"
GRANT_ARGV = ("agent", "grant", "--agent", ENGINEER, "--bundle", "code-editing")


async def _seed_agent(console: Console, name: str = ENGINEER) -> Agent:
    workspace = await console.session.scalar(select(Workspace))
    assert workspace is not None
    agent = Agent(
        workspace_id=workspace.id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{new_uuid7().hex[-6:]}",
    )
    console.session.add(agent)
    await console.session.commit()
    return agent


async def _owner_context(console: Console) -> WorkspaceContext:
    workspace = await console.session.scalar(select(Workspace))
    owner = await console.session.scalar(select(User))
    assert workspace is not None and owner is not None
    return WorkspaceContext(user=owner, workspace_id=workspace.id, role=WorkspaceRole.OWNER)


def _master_key(monkeypatch: pytest.MonkeyPatch) -> SecretCrypto:
    material = generate_master_key_material()
    monkeypatch.delenv("MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("MASTER_KEY", material)
    return SecretCrypto(MasterKey(key=decode_master_key_material(material)))


async def _seed_github(
    console: Console, crypto: SecretCrypto, *, name: str = "GitHub"
) -> Connection:
    connection, _ = await connections_service.create_connection(
        console.session,
        crypto,
        await _owner_context(console),
        connector_type="github",
        name=name,
        auth_type="pat",
        credentials={"token": "github-pat-for-tests"},
        config={},
        request_id=new_uuid7(),
        ip_hash="test",
    )
    return connection


async def _seed_sandbox(
    console: Console,
    crypto: SecretCrypto,
    github: Connection,
    *,
    allowed: list[str],
    name: str = "Existing sandbox",
) -> Connection:
    connection, _ = await connections_service.create_connection(
        console.session,
        crypto,
        await _owner_context(console),
        connector_type="cli",
        name=name,
        auth_type="none",
        credentials={},
        config={
            "default_network": "none",
            "git_connection_id": str(github.id),
            "allowed_repositories": allowed,
        },
        request_id=new_uuid7(),
        ip_hash="test",
    )
    return connection


def test_the_agent_group_parses_and_refuses_a_bundle_beside_a_capability() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [*GRANT_ARGV, "--create-sandbox", "--repositories", "octo/a,octo/b", "--yes", "--json"]
    )
    assert args.command == "agent grant"
    assert args.create_sandbox is True
    assert args.repositories == "octo/a,octo/b"
    assert parser.parse_args(["agent", "access", "--agent", "x"]).command == "agent access"
    assert parser.parse_args(["agent", "list"]).command == "agent list"
    assert parser.parse_args(["agent", "revoke", "--agent", "x", "--grant", "y"]).command == (
        "agent revoke"
    )
    with pytest.raises(SystemExit):
        parser.parse_args([*GRANT_ARGV, "--capability", "github.issue.read"])
    with pytest.raises(SystemExit):
        parser.parse_args(["agent", "revoke", "--agent", "x", "--bundle", "b", "--grant", "g"])


async def test_agent_grant_writes_the_sandbox_the_grants_and_the_rule_as_the_console(
    console: Console, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    agent = await _seed_agent(console)
    github = await _seed_github(console, crypto)

    result = await console.run(
        *GRANT_ARGV, "--create-sandbox", "--repositories", "*", "--yes", "--json"
    )

    capsys.readouterr()  # the master-key warning lands on stdout under test
    emit(result, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"]["name"] == ENGINEER
    assert len(payload["grants_created"]) == 11
    assert payload["grants_existing"] == []
    assert [rule["capability"] for rule in payload["rules_added"]] == ["cli.repository.push"]
    sandbox = await console.session.scalar(
        select(Connection).where(Connection.connector_type == "cli")
    )
    assert sandbox is not None
    assert sandbox.name == "Sandbox for GitHub"
    assert sandbox.config_json == {
        "default_network": "none",
        "git_connection_id": str(github.id),
        "allowed_repositories": ["*"],
    }
    assert payload["created_connection"]["id"] == str(sandbox.id)
    rows = list(
        await console.session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == agent.id)
        )
    )
    assert len(rows) == 11
    refreshed = await console.session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == [
        {"capability": "cli.repository.push", "risk": None, "action": "approval"}
    ]
    granted = list(
        await console.session.scalars(
            select(AuditEvent).where(AuditEvent.action == "agent.permission.granted")
        )
    )
    assert len(granted) == 11
    assert all(event.actor_type == "system" for event in granted)
    assert all(
        event.metadata_json["cli"] == "jhin-admin agent grant"
        and event.metadata_json["bundle"] == "code-editing"
        for event in granted
    )
    created = await console.session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "connection.created", AuditEvent.target_id == sandbox.id
        )
    )
    assert created is not None
    assert created.actor_type == "system"
    assert created.metadata_json["cli"] == "jhin-admin agent grant"
    assert created.metadata_json["agent_id"] == str(agent.id)
    assert "granted   cli.repository.push" in "\n".join(result.lines)
    assert any(line.startswith("connection created  Sandbox for GitHub") for line in result.lines)
    assert any("Push branches named agent/*" in line for line in result.lines)


async def test_agent_grant_dry_run_writes_nothing(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    agent = await _seed_agent(console)
    await _seed_github(console, crypto)

    result = await console.run(*GRANT_ARGV, "--create-sandbox", "--dry-run")

    assert result.data["dry_run"] is True
    assert len(result.data["grants_created"]) == 11
    assert "Dry run: nothing was written." in result.lines
    assert await console.count(AgentCapabilityGrant) == 0
    assert await console.count(Connection) == 1
    refreshed = await console.session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == []


async def test_agent_grant_refusals_are_sentences(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    await _seed_agent(console)

    with pytest.raises(CommandError) as unknown:
        await console.run("agent", "grant", "--agent", "Nobody", "--bundle", "code-editing")
    assert "No agent matches 'Nobody' in Acme HQ" in str(unknown.value)

    with pytest.raises(CommandError) as no_github:
        await console.run(*GRANT_ARGV, "--create-sandbox")
    assert str(no_github.value) == (
        "Acme HQ has no active GitHub connection. Connect one under Apps first."
    )

    github = await _seed_github(console, crypto)
    with pytest.raises(CommandError) as no_sandbox:
        await console.run(*GRANT_ARGV)
    assert str(no_sandbox.value) == (
        "No CLI Sandbox connection uses 'GitHub'. Pass --create-sandbox to make one pointing "
        "at it (repositories: *), or --sandbox <name|id>."
    )

    with pytest.raises(CommandError) as scope:
        await console.run(
            "agent",
            "grant",
            "--agent",
            ENGINEER,
            "--capability",
            "cli.repository.push",
            "--scope",
            "connection_id=x",
            "--yes",
        )
    assert str(scope.value) == (
        "`cli.repository.push` needs `repository` in its scope; pass --scope repository=... "
        "(for example *)."
    )

    sandbox = await _seed_sandbox(console, crypto, github, allowed=["octo/alpha"])
    with pytest.raises(HTTPException) as outside:
        await console.run(
            *GRANT_ARGV, "--sandbox", "Existing sandbox", "--repositories", "octo/beta"
        )
    assert "'Existing sandbox' allows only: octo/alpha" in str(outside.value.detail)

    with pytest.raises(HTTPException) as taken:
        await console.run(*GRANT_ARGV, "--create-sandbox", "--yes")
    assert str(taken.value.detail).startswith(
        f"A CLI Sandbox connection '{sandbox.name}' already uses 'GitHub'"
    )

    await _seed_agent(console, name="Twin")
    await _seed_agent(console, name="Twin")
    with pytest.raises(CommandError) as twins:
        await console.run("agent", "access", "--agent", "twin")
    assert str(twins.value).startswith("Two agents in Acme HQ are called 'twin':")

    await console.run("workspace", "create", "--name", "Second", "--owner", OWNER_EMAIL)
    with pytest.raises(CommandError) as ambiguous:
        await console.run("agent", "list")
    assert str(ambiguous.value) == (
        "More than one workspace exists; pass --workspace (`jhin-admin workspace list` shows them)."
    )

    monkeypatch.delenv("MASTER_KEY", raising=False)
    with pytest.raises(CommandError) as no_key:
        await console.run(*GRANT_ARGV, "--workspace", "acme-hq", "--create-sandbox", "--yes")
    assert "JHIN_MASTER_KEY is not available" in str(no_key.value)
    assert await console.count(AgentCapabilityGrant) == 0


async def test_agent_access_lists_problems_and_what_would_be_offered(
    console: Console, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    agent = await _seed_agent(console)
    await _seed_github(console, crypto)
    await console.run(*GRANT_ARGV, "--create-sandbox", "--yes")
    console.session.add(
        AgentCapabilityGrant(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            capability="linear.issue.read",
            scope_json={"connection_id": str(new_uuid7())},
            effect="allow",
        )
    )
    await console.session.commit()

    result = await console.run("agent", "access", "--agent", ENGINEER, "--json")

    text = "\n".join(result.lines)
    assert text.startswith(f"{ENGINEER} ({agent.id}) in Acme HQ")
    assert "Code editing" in text and " on" in text
    assert "needs attention: Connection no longer exists." in text
    assert "Dangling grants: 1" in text
    assert "rule  cli.repository.push risk=any -> approval" in text
    assert "linear.issue.read" not in text.split("Would be offered")[1]
    assert "cli.repository.checkout" in text.split("Would be offered")[1]
    capsys.readouterr()  # the master-key warning lands on stdout under test
    emit(result, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["dangling_grants"] == 1
    assert "github.pull_request.create" in payload["would_be_offered"]
    code_editing = next(b for b in payload["bundles"] if b["id"] == "code-editing")
    assert code_editing["state"] == "on"


async def test_agent_revoke_bundle_names_hand_made_rows_and_leaves_the_rule(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    agent = await _seed_agent(console)
    await _seed_github(console, crypto)
    granted = await console.run(*GRANT_ARGV, "--create-sandbox", "--yes")
    sandbox_id = granted.data["created_connection"]["id"]
    await console.run(
        "agent",
        "grant",
        "--agent",
        ENGINEER,
        "--capability",
        "cli.file.read",
        "--scope",
        f"connection_id={sandbox_id}",
        "--scope",
        "path=docs/*",
        "--yes",
    )
    assert await console.count(AgentCapabilityGrant) == 12

    result = await console.run(
        "agent", "revoke", "--agent", ENGINEER, "--bundle", "code-editing", "--yes"
    )

    assert len(result.data["revoked"]) == 12
    assert [row["capability"] for row in result.data["hand_made"]] == ["cli.file.read"]
    assert result.data["hand_made"][0]["scope_json"]["path"] == "docs/*"
    assert await console.count(AgentCapabilityGrant) == 0
    refreshed = await console.session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == [
        {"capability": "cli.repository.push", "risk": None, "action": "approval"}
    ]
    again = await console.run("agent", "revoke", "--agent", ENGINEER, "--bundle", "code-editing")
    assert again.lines == [f"Nothing to revoke: Code editing is not on for {ENGINEER}."]


async def test_agent_grant_capability_refuses_what_the_api_refuses_before_writing(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--capability`` never saw the API's request schema, so it once wrote an
    allow row in a namespace no agent may hold. The one validation path the
    API uses now runs first — before the confirmation, before a dry run —
    and the refusal reaches the terminal as the API's own sentence."""
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    await _seed_agent(console)
    github = await _seed_github(console, crypto)
    sandbox = await _seed_sandbox(console, crypto, github, allowed=["octo/alpha"])
    pinned = f"connection_id={sandbox.id}"

    async def grant(*argv: str) -> Result:
        return await console.run("agent", "grant", "--agent", ENGINEER, "--capability", *argv)

    with pytest.raises(HTTPException) as forbidden:
        await grant("agent.permission.grant", "--yes")
    assert forbidden.value.detail == (
        "capabilities in this namespace can never be granted to agents"
    )
    with pytest.raises(HTTPException) as malformed:
        await grant("GitHub.Repository.Read", "--dry-run")
    assert malformed.value.detail == "not a valid dotted capability name or pattern"
    with pytest.raises(HTTPException) as wide:
        await grant(
            "cli.repository.checkout", "--scope", pinned, "--scope", "repository=*", "--yes"
        )
    assert str(wide.value.detail) == (
        "'Existing sandbox' allows only: octo/alpha — '*' is outside it. Add it to the "
        "sandbox's allowed repositories under Apps, or grant only what the sandbox allows."
    )
    with pytest.raises(HTTPException) as protected:
        await grant(
            "cli.repository.push",
            "--scope",
            pinned,
            "--scope",
            "repository=octo/alpha",
            "--scope",
            "branch=main",
            "--yes",
        )
    assert str(protected.value.detail).startswith("branch 'main' is refused on every push")
    assert await console.count(AgentCapabilityGrant) == 0

    inside = await grant(
        "cli.repository.checkout", "--scope", pinned, "--scope", "repository=octo/alpha", "--yes"
    )
    assert inside.data["grant"]["problems"] == []
    assert await console.count(AgentCapabilityGrant) == 1


async def test_agent_grant_bounds_the_repository_list_in_the_planner_s_words(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    await _seed_agent(console)
    await _seed_github(console, crypto)
    fifty = [f"octo/r{index}" for index in range(50)]

    with pytest.raises(CommandError) as too_many:
        await console.run(
            *GRANT_ARGV,
            "--create-sandbox",
            "--repositories",
            ",".join([*fifty, "octo/one-more"]),
            "--dry-run",
        )
    assert str(too_many.value) == "At most 50 repositories can be granted at once."
    assert await console.count(Connection) == 1

    # Duplicates count once, as the planner counts them.
    result = await console.run(
        *GRANT_ARGV, "--create-sandbox", "--repositories", ",".join(fifty + fifty), "--dry-run"
    )
    assert result.data["dry_run"] is True
    checkouts = [
        row
        for row in result.data["grants_created"]
        if row["capability"] == "cli.repository.checkout"
    ]
    assert len(checkouts) == 50
    assert await console.count(AgentCapabilityGrant) == 0


async def test_agent_grant_repositories_absent_is_everything_but_empty_is_refused(
    console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--repositories`` left off means every repository the allow-list
    permits. Given and empty (a bare comma, whitespace) it once silently
    became ``*`` too; now it is refused in the planner's own words, the way
    the API refuses ``repositories: []``."""
    from jhin_api.cli.commands import CommandError
    from jhin_policy.bundles import NO_REPOSITORIES_SENTENCE

    await console.bootstrap()
    crypto = _master_key(monkeypatch)
    await _seed_agent(console)
    await _seed_github(console, crypto)

    for empty in (",", " , ", ""):
        with pytest.raises(CommandError) as refused:
            await console.run(
                "agent",
                "grant",
                "--agent",
                ENGINEER,
                "--bundle",
                "github-read",
                "--repositories",
                empty,
                "--yes",
            )
        assert str(refused.value) == NO_REPOSITORIES_SENTENCE

    result = await console.run(
        "agent", "grant", "--agent", ENGINEER, "--bundle", "github-read", "--yes", "--json"
    )
    rows = result.data["grants_created"]
    assert rows and all(row["scope_json"].get("repository") == "*" for row in rows), rows
