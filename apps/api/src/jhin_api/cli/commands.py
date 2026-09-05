"""What each ``jhin-admin`` command does.

Every command drives the service functions the HTTP API drives, so an account
made here is indistinguishable from one made in the browser: the same password
policy, the same starter skills in a new workspace, the same audit trail. The
only rows written by hand are the ones no service knows how to write.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import Connection as SyncConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jhin_api.access import invitations
from jhin_api.audit import service as audit
from jhin_api.auth.service import (
    create_owner_and_workspace,
    needs_bootstrap,
    revoke_all_sessions,
)
from jhin_api.cli.parser import PROGRAM
from jhin_api.cli.passwords import read_new_password
from jhin_api.cli.runtime import (
    NO_CLIENT_ADDRESS,
    CommandError,
    Result,
    Runtime,
    confirm,
    fields,
    table,
)
from jhin_api.deps import WorkspaceContext
from jhin_api.health.service import check_postgres
from jhin_api.policy.bundles import agent_bundles, apply_bundle, remove_bundle
from jhin_api.policy.schemas import (
    BundleApply,
    BundleApplyOut,
    BundleNeedOut,
    BundleRemoveOut,
    SandboxCreate,
)
from jhin_api.policy.service import (
    annotate_grants,
    create_grant,
    list_grants,
    parse_rules,
    revoke_grant,
    validate_grant,
)
from jhin_api.security.passwords import hash_password
from jhin_api.settings import get_settings
from jhin_api.workspaces import service as workspaces
from jhin_connectors import build_default_definition_catalog, default_registry
from jhin_db.migrate import alembic_config
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Connection,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import ActorType, ConnectionStatus, UserStatus, WorkspaceRole
from jhin_policy import Grant, GrantEffect, bundle_by_id, capability_matches
from jhin_policy.bundles import (
    BUNDLE_IDS,
    MAX_REPOSITORIES,
    MAX_REPOSITORIES_SENTENCE,
    NO_REPOSITORIES_SENTENCE,
)
from jhin_secrets import SecretCrypto, load_master_key
from jhin_secrets.crypto import MasterKeyError
from jhin_tools import allowed_tool_definitions

#: What ``doctor`` counts to describe an install at a glance.
_CONTENT_COUNTS: tuple[tuple[str, type[Any]], ...] = (
    ("users", User),
    ("workspaces", Workspace),
    ("agents", Agent),
)

# The HTTP surface types these fields (EmailStr, Field(min_length=1,
# max_length=200)) and argv does not, so the checks have to be repeated here.
# It is not cosmetic: `owner create` runs exactly once per install and then
# closes first-run setup behind itself, so an address nobody can sign in with
# leaves an install with an owner it cannot reach and no second chance.
_EMAIL = TypeAdapter(EmailStr)
_MAX_EMAIL_CHARS = 320  # User.email's column width
_MAX_NAME_CHARS = 200  # the width the API's schemas allow


def _clean_email(raw: str) -> str:
    """Normalize an address the way sign-in will, or refuse it.

    Sign-in lowercases and strips before it looks anyone up, so the stored
    address has to be normalized the same way or the account is unreachable.
    """
    address = raw.strip().lower()
    if len(address) > _MAX_EMAIL_CHARS:
        raise CommandError(f"That email address is longer than {_MAX_EMAIL_CHARS} characters.")
    try:
        _EMAIL.validate_python(address)
    except ValidationError as exc:
        raise CommandError(
            f"{raw!r} is not a valid email address, and sign-in would never match it."
        ) from exc
    return address


def _clean_name(raw: str, *, what: str) -> str:
    """Trim a human-supplied name, refusing an empty or oversized one."""
    value = raw.strip()
    if not value:
        raise CommandError(f"The {what} cannot be empty.")
    if len(value) > _MAX_NAME_CHARS:
        raise CommandError(f"The {what} is longer than {_MAX_NAME_CHARS} characters.")
    return value


def _provenance(command: str) -> dict[str, Any]:
    """Mark an audit row as written from the console rather than the app.

    The same shape the dev seed marks its own rows with, so an operator
    reading the audit log later can tell which change came from a shell.
    """
    return {"cli": f"{PROGRAM} {command}"}


def _console_context(user: User, workspace_id: UUID) -> WorkspaceContext:
    """The context the workspace services check their caller's authority with.

    A shell on the server outranks every workspace role — whoever runs this
    can edit the database directly — so the context exists to satisfy the
    check, not to pretend the console holds a seat in the workspace. The
    account named here is the one the audit trail records as acting, which is
    why each caller picks a real person rather than a placeholder.
    """
    return WorkspaceContext(user=user, workspace_id=workspace_id, role=WorkspaceRole.OWNER)


def _user_out(user: User) -> dict[str, Any]:
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name}


def _workspace_out(workspace: Workspace) -> dict[str, Any]:
    return {"id": str(workspace.id), "name": workspace.name, "slug": workspace.slug}


async def _resolve_user(db: AsyncSession, email: str) -> User:
    address = email.strip().lower()
    user = await db.scalar(select(User).where(User.email == address))
    if user is None:
        raise CommandError(
            f"No account exists for {address}. `{PROGRAM} user list` shows every account."
        )
    return user


async def _resolve_workspace_or_only(db: AsyncSession, reference: str | None) -> Workspace:
    """The named workspace, or the only one when the install has exactly one.

    Every agent command takes ``--workspace``, and on the common single-
    workspace install nobody should have to look the slug up to type it.
    """
    if reference:
        return await _resolve_workspace(db, reference)
    workspaces = list(await db.scalars(select(Workspace).order_by(Workspace.created_at)))
    if len(workspaces) == 1:
        return workspaces[0]
    if not workspaces:
        raise CommandError(f"No workspaces yet. `{PROGRAM} workspace create` adds one.")
    raise CommandError(
        f"More than one workspace exists; pass --workspace (`{PROGRAM} workspace list` shows them)."
    )


async def _resolve_workspace(db: AsyncSession, reference: str) -> Workspace:
    """Find a workspace by slug or by id, whichever the operator had to hand."""
    try:
        workspace_id = UUID(reference)
    except ValueError:
        workspace = await db.scalar(select(Workspace).where(Workspace.slug == reference))
    else:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise CommandError(
            f"No workspace matches {reference!r}. `{PROGRAM} workspace list` shows every "
            "workspace and its slug."
        )
    return workspace


async def _workspace_owner(db: AsyncSession, workspace: Workspace) -> User:
    owner = await db.scalar(
        select(User)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
        .where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.role == WorkspaceRole.OWNER.value,
        )
        .order_by(WorkspaceMembership.created_at)
        .limit(1)
    )
    if owner is None:
        raise CommandError(
            f"{workspace.slug} has no owner, so there is nobody to send an invitation as. "
            f"Give it one with `{PROGRAM} user set-role --role owner` first."
        )
    return owner


# --- doctor ---


async def _check_database(engine: AsyncEngine) -> dict[str, Any]:
    try:
        # The probe the readiness endpoint runs, so `doctor` and
        # /health/ready cannot disagree about whether the database is there.
        await check_postgres(engine)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return {"ok": True, "detail": None}


def _applied_revision(connection: SyncConnection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def _check_migrations(
    engine: AsyncEngine, database_url: str, *, reachable: bool
) -> dict[str, Any]:
    """Compare the revision stamped in the database with the code's head.

    An install whose schema is behind the code fails in ways that look like
    bugs — a missing column deep inside a request — so it is worth saying
    plainly here rather than leaving an operator to infer it.
    """
    heads = list(ScriptDirectory.from_config(alembic_config(database_url)).get_heads())
    head = heads[0] if len(heads) == 1 else ",".join(heads)
    if not reachable:
        return {"ok": False, "current": None, "head": head, "pending": None}
    async with engine.connect() as connection:
        current = await connection.run_sync(_applied_revision)
    return {"ok": current == head, "current": current, "head": head, "pending": current != head}


def _check_master_key() -> dict[str, Any]:
    """Report whether the master key loads. Never what it is.

    Without it every stored credential is unreadable, so an operator needs to
    know it is there — and nothing more than that.
    """
    try:
        load_master_key()
    except MasterKeyError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": None}


async def _content_counts(db: AsyncSession) -> dict[str, int]:
    columns = [
        select(func.count()).select_from(model).scalar_subquery().label(name)
        for name, model in _CONTENT_COUNTS
    ]
    row = (await db.execute(select(*columns))).one()
    return {name: int(value or 0) for (name, _), value in zip(_CONTENT_COUNTS, row, strict=True)}


def _migration_line(migrations: dict[str, Any]) -> str:
    if migrations["pending"] is None:
        return f"unknown while the database is unreachable (code expects {migrations['head']})"
    if not migrations["pending"]:
        return f"ok, at {migrations['current']}"
    applied = migrations["current"] or "no revision at all"
    return (
        f"PENDING: the database is at {applied}, the code expects {migrations['head']}; "
        "run `jhin-db-migrate`"
    )


async def doctor(rt: Runtime) -> Result:
    database = await _check_database(rt.engine)
    reachable = bool(database["ok"])
    migrations = await _check_migrations(rt.engine, rt.database_url, reachable=reachable)
    master_key = _check_master_key()
    setup_open = await needs_bootstrap(rt.db) if reachable else None
    counts = await _content_counts(rt.db) if reachable else {}

    healthy = reachable and bool(migrations["ok"]) and bool(master_key["ok"])
    reported = [
        ("database", "ok" if reachable else f"UNREACHABLE: {database['detail']}"),
        ("migrations", _migration_line(migrations)),
        ("master key", "ok, loaded" if master_key["ok"] else f"UNUSABLE: {master_key['detail']}"),
    ]
    if setup_open is not None:
        reported.append(
            (
                "first-run setup",
                f"open: no account exists yet; `{PROGRAM} owner create` makes the first"
                if setup_open
                else "closed: an account already exists",
            )
        )
    reported += [(name, str(counts[name])) for name, _ in _CONTENT_COUNTS if name in counts]

    return Result(
        data={
            "ok": healthy,
            "database": database,
            "migrations": migrations,
            "master_key": master_key,
            "setup_open": setup_open,
            "counts": counts,
        },
        lines=[
            *fields(reported),
            "",
            "This install looks healthy." if healthy else "Something above needs fixing.",
        ],
        exit_code=0 if healthy else 1,
    )


# --- owner ---


async def owner_create(rt: Runtime) -> Result:
    args = rt.args
    if not await needs_bootstrap(rt.db):
        raise CommandError(
            "An account already exists, so first-run setup is closed, the same rule the "
            f"/setup page follows. Use `{PROGRAM} user create` to add another account."
        )
    email = _clean_email(args.email)
    display_name = _clean_name(args.name, what="owner's name")
    workspace_name = _clean_name(args.workspace, what="workspace name")
    password = read_new_password(email=email, from_stdin=args.password_stdin)
    user, workspace = await create_owner_and_workspace(
        rt.db,
        email=email,
        password=password,
        display_name=display_name,
        workspace_name=workspace_name,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        actor_type=ActorType.SYSTEM,
        metadata=_provenance("owner create"),
    )
    # The browser flow seats the operator in a session here. There is no
    # browser holding a token on this side, so the run stops at the rows.
    await rt.db.commit()
    return Result(
        data={"user": _user_out(user), "workspace": _workspace_out(workspace)},
        lines=[
            f"Created owner {user.email} and workspace {workspace.name} ({workspace.slug}).",
            "Sign in at the web UI with the password you just set.",
        ],
    )


# --- user ---


async def user_create(rt: Runtime) -> Result:
    args = rt.args
    email = _clean_email(args.email)
    display_name = _clean_name(args.name, what="person's name")
    role: WorkspaceRole = args.role
    workspace = await _resolve_workspace(rt.db, args.workspace)
    if await rt.db.scalar(select(User.id).where(User.email == email)):
        raise CommandError(
            f"{email} already has an account. `{PROGRAM} user set-role` changes what they "
            f"can do, and `{PROGRAM} invite create` adds them to another workspace."
        )
    if not confirm(
        f"Create {email} as {role.value} in {workspace.name} ({workspace.slug})?",
        assume_yes=args.yes,
    ):
        raise CommandError("Cancelled. Nothing was changed.")
    password = read_new_password(email=email, from_stdin=args.password_stdin)

    # No service creates a bare account — the web app only ever makes one by
    # accepting an invitation — so the row is built exactly as
    # ``invitations.accept`` builds it: address normalized (sign-in lowercases
    # before it looks anyone up, so a stray capital is an account nobody can
    # reach), Argon2id hash, active from the start.
    user = User(
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE.value,
    )
    rt.db.add(user)
    await rt.db.flush()
    audit.record(
        rt.db,
        action="user.created",
        target_type="user",
        target_id=user.id,
        workspace_id=workspace.id,
        actor_type=ActorType.SYSTEM,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        metadata={"email": email, **_provenance("user create")},
    )
    # add_member owns the membership rules and its own audit row, and commits
    # the account above in the same transaction. It is told the console acted,
    # or its row would read as though this brand-new account added itself.
    await workspaces.add_member(
        rt.db,
        _console_context(user, workspace.id),
        email=email,
        role=role,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        actor_type=ActorType.SYSTEM,
        extra_metadata=_provenance("user create"),
    )
    return Result(
        data={
            "user": _user_out(user),
            "workspace": _workspace_out(workspace),
            "role": role.value,
        },
        lines=[
            f"Created {user.email} and added them to {workspace.name} as {role.value}.",
            "They sign in at the web UI with the password you just set.",
        ],
    )


async def user_list(rt: Runtime) -> Result:
    reference = rt.args.workspace
    workspace = await _resolve_workspace(rt.db, reference) if reference else None
    listed: list[dict[str, Any]]
    if workspace is None:
        listed = [
            {**_user_out(user), "status": user.status, "role": None}
            for user in await rt.db.scalars(select(User).order_by(User.created_at))
        ]
    else:
        listed = [
            {**_user_out(user), "status": user.status, "role": membership.role}
            for membership, user in await workspaces.list_members(rt.db, workspace.id)
        ]

    data = {"workspace": _workspace_out(workspace) if workspace else None, "users": listed}
    if not listed:
        empty = (
            f"No accounts yet. `{PROGRAM} owner create` makes the first one."
            if workspace is None
            else f"{workspace.name} has no members."
        )
        return Result(data=data, lines=[empty])

    if workspace is None:
        headers = ["EMAIL", "NAME", "STATUS", "ID"]
        rows = [[u["email"], u["display_name"], u["status"], u["id"]] for u in listed]
    else:
        headers = ["EMAIL", "NAME", "ROLE", "STATUS", "ID"]
        rows = [[u["email"], u["display_name"], u["role"], u["status"], u["id"]] for u in listed]
    return Result(data=data, lines=table(headers, rows))


async def user_set_password(rt: Runtime) -> Result:
    args = rt.args
    user = await _resolve_user(rt.db, args.email)
    if not confirm(
        f"Set a new password for {user.email} and sign out every session they have open?",
        assume_yes=args.yes,
    ):
        raise CommandError("Cancelled. Nothing was changed.")
    password = read_new_password(email=user.email, from_stdin=args.password_stdin)

    user.password_hash = hash_password(password)
    # A password change is how somebody answers "I think someone else has my
    # credentials", so every session minted under the old password has to go
    # with it — exactly what ``auth.change_password`` does after it rotates.
    revoked = await revoke_all_sessions(rt.db, user.id)
    audit.record(
        rt.db,
        action="auth.password_changed",
        target_type="user",
        target_id=user.id,
        workspace_id=None,
        actor_type=ActorType.SYSTEM,
        actor_id=user.id,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        metadata={"revoked_sessions": revoked, **_provenance("user set-password")},
    )
    await rt.db.commit()
    return Result(
        data={"user": _user_out(user), "revoked_sessions": revoked},
        lines=[
            f"Password set for {user.email}.",
            f"{revoked} open session(s) revoked; they sign in again with the new password.",
        ],
    )


async def user_set_role(rt: Runtime) -> Result:
    args = rt.args
    user = await _resolve_user(rt.db, args.email)
    workspace = await _resolve_workspace(rt.db, args.workspace)
    role: WorkspaceRole = args.role
    membership = await rt.db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if membership is None:
        raise CommandError(
            f"{user.email} is not a member of {workspace.slug}. `{PROGRAM} invite create` is "
            "how somebody joins a workspace they are not in."
        )
    current = WorkspaceRole(membership.role)
    data = {
        "user": _user_out(user),
        "workspace": _workspace_out(workspace),
        "from_role": current.value,
        "to_role": role.value,
        "changed": current != role,
    }
    if current == role:
        return Result(
            data=data, lines=[f"{user.email} is already {role.value} in {workspace.name}."]
        )
    if not confirm(
        f"Change {user.email} from {current.value} to {role.value} in {workspace.name}?",
        assume_yes=args.yes,
    ):
        raise CommandError("Cancelled. Nothing was changed.")
    # The rule this keeps is the one a hand-written UPDATE would drop: a
    # workspace never loses its last owner. The authority checks alongside it
    # pass trivially here, because a shell on the server already outranks them.
    await workspaces.update_member_role(
        rt.db,
        _console_context(user, workspace.id),
        membership.id,
        role=role,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        actor_type=ActorType.SYSTEM,
        extra_metadata=_provenance("user set-role"),
    )
    return Result(
        data=data,
        lines=[f"{user.email} is now {role.value} in {workspace.name} (was {current.value})."],
    )


# --- workspace ---


async def workspace_create(rt: Runtime) -> Result:
    owner = await _resolve_user(rt.db, rt.args.owner)
    workspace = await workspaces.create(
        rt.db,
        name=_clean_name(rt.args.name, what="workspace name"),
        # New workspaces start on UTC; a member changes it in settings.
        default_timezone="UTC",
        creator_id=owner.id,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
    )
    return Result(
        data={"workspace": _workspace_out(workspace), "owner": _user_out(owner)},
        lines=[f"Created workspace {workspace.name} ({workspace.slug}), owned by {owner.email}."],
    )


async def workspace_list(rt: Runtime) -> Result:
    rows = (
        await rt.db.execute(
            select(Workspace, func.count(WorkspaceMembership.id))
            .outerjoin(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .group_by(Workspace.id)
            .order_by(Workspace.created_at)
        )
    ).all()
    listed = [
        {**_workspace_out(workspace), "status": workspace.status, "members": int(members or 0)}
        for workspace, members in rows
    ]
    data = {"workspaces": listed}
    if not listed:
        empty = f"No workspaces yet. `{PROGRAM} workspace create` adds one."
        return Result(data=data, lines=[empty])
    return Result(
        data=data,
        lines=table(
            ["SLUG", "NAME", "STATUS", "MEMBERS", "ID"],
            [[w["slug"], w["name"], w["status"], str(w["members"]), w["id"]] for w in listed],
        ),
    )


# --- invite ---


async def invite_create(rt: Runtime) -> Result:
    settings = get_settings()
    workspace = await _resolve_workspace(rt.db, rt.args.workspace)
    # An invitation records who sent it, and the console has no identity of
    # its own. The workspace's owner is who an operator with a shell is acting
    # for, so that is the account the row names — the same one the app would
    # have recorded had they clicked Invite themselves.
    inviter = await _workspace_owner(rt.db, workspace)
    created = await invitations.create_invitation(
        rt.db,
        _console_context(inviter, workspace.id),
        email=_clean_email(rt.args.email),
        role=rt.args.role,
        ttl_days=settings.invitation_ttl_days,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
    )
    invitation = created.invitation
    url = invitations.invite_url(settings.app_url, created.token)
    return Result(
        data={
            "invitation": {
                "id": str(invitation.id),
                "email": invitation.email,
                "role": invitation.role,
                "expires_at": invitation.expires_at.isoformat(),
            },
            "invite_url": url,
            "invited_by": inviter.email,
        },
        lines=[
            f"Invitation for {invitation.email} as {invitation.role}, "
            f"good for {settings.invitation_ttl_days} days:",
            "",
            f"  {url}",
            "",
            "Pass that link on yourself. Jhin sends no mail, only the hash of the link is "
            "stored, and it is shown here once.",
        ],
    )


# --- agent ---
#
# What an agent may use, and the console way to give it an app. Every write
# goes through the bundle service the Tools & Access tab uses, so a grant made
# here carries the same validation and the same audit trail — plus the
# ``{"cli": "jhin-admin agent grant"}`` provenance and a ``system`` actor.

_BUNDLE_FOR_APP = {"github": "github-read", "web": "web-access", "cli": "code-editing"}
_SCOPE_EXAMPLES = {
    "branch": "agent/*",
    "base": "*",
    "repository": "*",
    "connection_id": "<id>",
}


def _connector_label(connector_type: str) -> str:
    connector = default_registry().get(connector_type)
    return connector.manifest.display_name if connector is not None else connector_type


def _agent_out(agent: Agent) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "slug": agent.slug,
        "role_title": agent.role_title,
        "status": agent.status,
    }


def _scope_text(scope: dict[str, Any]) -> str:
    if not scope:
        return "any scope"
    return ", ".join(f"{key}={value}" for key, value in scope.items())


async def _resolve_agent(db: AsyncSession, workspace: Workspace, reference: str) -> Agent:
    """An agent by id, slug, or case-insensitive exact name."""
    try:
        agent_id: UUID | None = UUID(reference)
    except ValueError:
        agent_id = None
    if agent_id is not None:
        agent = await db.scalar(
            select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace.id)
        )
        if agent is not None:
            return agent
    wanted = reference.strip().casefold()
    matches = [
        agent
        for agent in await db.scalars(
            select(Agent).where(Agent.workspace_id == workspace.id).order_by(Agent.created_at)
        )
        if agent.slug == reference.strip() or agent.name.casefold() == wanted
    ]
    if not matches:
        raise CommandError(
            f"No agent matches '{reference}' in {workspace.name}. `{PROGRAM} agent list` "
            "shows them."
        )
    if len(matches) > 1:
        ids = ", ".join(str(agent.id) for agent in matches)
        raise CommandError(
            f"Two agents in {workspace.name} are called '{reference}': {ids}. Use the id."
        )
    return matches[0]


async def _resolve_connection(
    db: AsyncSession, workspace: Workspace, reference: str, connector_type: str
) -> Connection:
    """A connection of one type by id or case-insensitive name."""
    label = _connector_label(connector_type)
    try:
        connection_id: UUID | None = UUID(reference)
    except ValueError:
        connection_id = None
    rows = list(
        await db.scalars(
            select(Connection)
            .where(
                Connection.workspace_id == workspace.id,
                Connection.connector_type == connector_type,
            )
            .order_by(Connection.created_at)
        )
    )
    if connection_id is not None:
        for row in rows:
            if row.id == connection_id:
                return row
    wanted = reference.strip().casefold()
    matches = [row for row in rows if row.name.casefold() == wanted]
    if not matches:
        raise CommandError(
            f"No {label} connection matches '{reference}' in {workspace.name}. Apps lists them."
        )
    if len(matches) > 1:
        ids = ", ".join(str(row.id) for row in matches)
        raise CommandError(
            f"Two {label} connections in {workspace.name} are called '{reference}': {ids}. "
            "Use the id."
        )
    return matches[0]


async def _acting_admin(db: AsyncSession, workspace: Workspace, email: str | None) -> User:
    """Whom the audit trail records as acting: ``--as`` when given (and an
    admin or owner of the workspace), else the workspace's owner."""
    if not email:
        return await _workspace_owner(db, workspace)
    user = await _resolve_user(db, email)
    membership = await db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if membership is None or membership.role not in (
        WorkspaceRole.ADMIN.value,
        WorkspaceRole.OWNER.value,
    ):
        raise CommandError(f"{user.email} is not an admin or owner of {workspace.name}.")
    return user


def _load_crypto_for_sandbox() -> SecretCrypto:
    try:
        return SecretCrypto(load_master_key())
    except MasterKeyError:
        raise CommandError(
            "JHIN_MASTER_KEY is not available in this shell; inside the compose stack "
            f"`docker compose exec api {PROGRAM} ...` brings it with it."
        ) from None


def _parse_repositories(raw: str | None) -> list[str]:
    """Comma-separated entries, trimmed and de-duplicated the way the planner
    counts them, and bounded by the planner's own limits in its own words --
    before the request schema, whose limits are validation errors rather
    than sentences, is ever built.

    An ABSENT flag means every repository, which the sandbox's allow-list
    still bounds. A flag given but EMPTY is not a request for everything; it
    is refused the way the API refuses ``repositories: []``.
    """
    if raw is None:
        return ["*"]
    entries: list[str] = []
    for entry in raw.split(","):
        value = entry.strip()
        if value and value not in entries:
            entries.append(value)
    if len(entries) > MAX_REPOSITORIES:
        raise CommandError(MAX_REPOSITORIES_SENTENCE)
    if not entries:
        raise CommandError(NO_REPOSITORIES_SENTENCE)
    return entries


def _parse_scope(pairs: list[str]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise CommandError(f"--scope takes key=value, not {pair!r}.")
        scope[key.strip()] = value.strip()
    return scope


async def agent_list(rt: Runtime) -> Result:
    workspace = await _resolve_workspace_or_only(rt.db, rt.args.workspace)
    agents = list(
        await rt.db.scalars(
            select(Agent).where(Agent.workspace_id == workspace.id).order_by(Agent.created_at)
        )
    )
    data = {"workspace": _workspace_out(workspace), "agents": [_agent_out(a) for a in agents]}
    if not agents:
        return Result(data=data, lines=[f"{workspace.name} has no agents yet."])
    return Result(
        data=data,
        lines=table(
            ["NAME", "ROLE", "SLUG", "STATUS", "ID"],
            [[a.name, a.role_title or "", a.slug, a.status, str(a.id)] for a in agents],
        ),
    )


def _review_lines(bundle_id: str, result: BundleApplyOut, *, base: str | None) -> list[str]:
    """What the agent will be able to do, in the words the Review step uses."""
    sandbox = result.created_connection.name if result.created_connection else "the sandbox"
    for row in (*result.grants_created, *result.grants_existing):
        if row.capability.startswith("cli.") and row.connection_name:
            sandbox = row.connection_name
            break
    if bundle_id == "code-editing":
        base_text = "any base branch" if not base or base == "*" else f"base {base}"
        return [
            f"Check out any repository {sandbox} allows, browse, search, read and edit files, "
            "and run tests inside the sandbox.",
            "Push branches named agent/* — asks for your approval every time, even if this "
            "agent is later made Autonomous.",
            "Read repositories, branches, files and pull requests on GitHub; open pull "
            f"requests ({base_text}).",
        ]
    if bundle_id == "github-read":
        return [
            "Read repositories, branches, files, issues, pull requests, checks and workflow "
            "runs on GitHub. Nothing is written."
        ]
    if bundle_id == "web-access":
        connection = next(
            (
                row.connection_name
                for row in (*result.grants_created, *result.grants_existing)
                if row.connection_name
            ),
            "the Web connection",
        )
        return [f"Search the web and read public pages through {connection}."]
    return []


def _need_sentence(
    need: BundleNeedOut, workspace: Workspace, github_name: str, repositories: list[str]
) -> str:
    label = _connector_label(need.connector_type)
    if need.kind == "connect":
        return f"{workspace.name} has no active {label} connection. Connect one under Apps first."
    if need.kind == "choose":
        flag = "sandbox" if need.connector_type == "cli" else "github"
        names = ", ".join(choice.name for choice in need.choices)
        return (
            f"More than one {label} connection is active: pass --{flag} <name|id> "
            f"(choices: {names})."
        )
    if need.kind == "create_sandbox":
        github = need.choices[0].name if need.choices else github_name
        return (
            f"No CLI Sandbox connection uses '{github}'. Pass --create-sandbox to make one "
            f"pointing at it (repositories: {', '.join(repositories)}), or --sandbox <name|id>."
        )
    return f"This workspace does not offer: {need.detail}."


async def agent_grant(rt: Runtime) -> Result:
    args = rt.args
    workspace = await _resolve_workspace_or_only(rt.db, args.workspace)
    agent = await _resolve_agent(rt.db, workspace, args.agent)
    actor = await _acting_admin(rt.db, workspace, args.actor_email)
    ctx = _console_context(actor, workspace.id)
    provenance = _provenance("agent grant")
    base: dict[str, Any] = {"agent": _agent_out(agent), "workspace": _workspace_out(workspace)}

    if args.capability:
        return await _grant_capability(rt, ctx, agent, workspace, base, provenance)

    if args.bundle:
        bundle_id = args.bundle
    else:
        bundle_id = _BUNDLE_FOR_APP.get(args.app or "")
        if bundle_id is None:
            raise CommandError(f"There is no capability bundle for '{args.app}'; use --capability.")
    bundle = bundle_by_id(bundle_id)
    if bundle is None:
        raise CommandError(
            f"No capability bundle '{bundle_id}'. Choose one of: {', '.join(BUNDLE_IDS)}."
        )

    connections: dict[str, UUID] = {}
    github: Connection | None = None
    if args.github:
        github = await _resolve_connection(rt.db, workspace, args.github, "github")
        connections["github"] = github.id
    if args.sandbox:
        sandbox = await _resolve_connection(rt.db, workspace, args.sandbox, "cli")
        connections["cli"] = sandbox.id
    repositories = _parse_repositories(args.repositories)

    sandbox_spec: SandboxCreate | None = None
    crypto: SecretCrypto | None = None
    if args.create_sandbox:
        if github is None:
            candidates = list(
                await rt.db.scalars(
                    select(Connection).where(
                        Connection.workspace_id == workspace.id,
                        Connection.connector_type == "github",
                        Connection.status == ConnectionStatus.ACTIVE.value,
                    )
                )
            )
            if not candidates:
                raise CommandError(
                    f"{workspace.name} has no active GitHub connection. Connect one under "
                    "Apps first."
                )
            if len(candidates) > 1:
                names = ", ".join(candidate.name for candidate in candidates)
                raise CommandError(
                    "More than one GitHub connection is active: pass --github <name|id> "
                    f"(choices: {names})."
                )
            github = candidates[0]
        crypto = _load_crypto_for_sandbox()
        sandbox_spec = SandboxCreate(
            name=args.sandbox_name or "",
            git_connection_id=github.id,
            allowed_repositories=repositories,
        )

    def request(*, dry_run: bool) -> BundleApply:
        return BundleApply(
            connections=connections,
            repositories=repositories,
            base=args.base,
            sandbox=sandbox_spec,
            dry_run=dry_run,
        )

    async def apply(*, dry_run: bool) -> BundleApplyOut:
        return await apply_bundle(
            rt.db,
            crypto,
            ctx,
            agent.id,
            bundle.id,
            request(dry_run=dry_run),
            request_id=rt.request_id,
            ip_hash=NO_CLIENT_ADDRESS,
            actor_type=ActorType.SYSTEM,
            extra_metadata=provenance,
        )

    preview = await apply(dry_run=True)
    if preview.needs:
        raise CommandError(
            _need_sentence(preview.needs[0], workspace, github.name if github else "", repositories)
        )
    if args.dry_run:
        result = preview
    else:
        creates = (
            f"Creates connection '{sandbox_spec.name or f'Sandbox for {github.name}'}' "
            f"(repositories: {', '.join(repositories)}); "
            if sandbox_spec is not None and github is not None
            else ""
        )
        rules = len(preview.rules_added)
        if not confirm(
            f"Turn on {bundle.label} for {agent.name} in {workspace.name}? {creates}writes "
            f"{len(preview.grants_created)} grants and {rules} approval rule(s).",
            assume_yes=args.yes,
        ):
            raise CommandError("Cancelled. Nothing was changed.")
        result = await apply(dry_run=False)

    lines = list(_review_lines(bundle.id, result, base=args.base))
    if result.created_connection is not None:
        lines.append(
            f"connection created  {result.created_connection.name} ({result.created_connection.id})"
        )
    for row in result.grants_created:
        lines.append(f"granted   {row.capability}  {_scope_text(row.scope_json)}")
    for row in result.grants_existing:
        lines.append(f"kept      {row.capability}  {_scope_text(row.scope_json)}")
    for rule in result.rules_added:
        lines.append(f"rule added  {rule.capability} -> {rule.action}")
    for warning in result.warnings:
        lines.append(f"warning  {warning}")
    if result.dry_run:
        lines.append("Dry run: nothing was written.")
    return Result(data={**result.model_dump(mode="json"), **base}, lines=lines)


async def _grant_capability(
    rt: Runtime,
    ctx: WorkspaceContext,
    agent: Agent,
    workspace: Workspace,
    base: dict[str, Any],
    provenance: dict[str, Any],
) -> Result:
    """One capability, through the same ``create_grant`` the API uses. Required
    scope keys are never guessed: the operator passes them or is told which."""
    args = rt.args
    capability = args.capability.strip()
    scope = _parse_scope(args.scope)
    if args.effect == "allow":
        matched = [
            definition
            for definition in build_default_definition_catalog().definitions()
            if capability_matches(capability, definition.required_capability)
        ]
        for definition in matched:
            for key in definition.required_grant_scope_keys:
                if key not in scope:
                    raise CommandError(
                        f"`{capability}` needs `{key}` in its scope; pass --scope {key}=... "
                        f"(for example {_SCOPE_EXAMPLES.get(key, '<value>')})."
                    )
    # The one check every grant writer shares, run before the console asks
    # for confirmation or describes a dry run: a capability no agent may
    # hold, a row the evaluator would refuse on every call, a width the
    # pinned sandbox does not allow. ``create_grant`` runs it again; the
    # refusal reaches the terminal as the API's own sentence.
    await validate_grant(
        rt.db, workspace.id, capability=capability, scope=scope, effect=args.effect
    )
    already = await rt.db.scalar(
        select(AgentCapabilityGrant.id).where(
            AgentCapabilityGrant.agent_id == agent.id,
            AgentCapabilityGrant.capability == capability,
            AgentCapabilityGrant.effect == args.effect,
        )
    )
    if args.dry_run:
        return Result(
            data={**base, "capability": capability, "scope": scope, "effect": args.effect},
            lines=[
                f"would grant  {capability}  {_scope_text(scope)}",
                "Dry run: nothing was written.",
            ],
        )
    if not confirm(
        f"Grant {capability} ({_scope_text(scope)}, {args.effect}) to {agent.name} in "
        f"{workspace.name}?",
        assume_yes=args.yes,
    ):
        raise CommandError("Cancelled. Nothing was changed.")
    grant = await create_grant(
        rt.db,
        ctx,
        agent.id,
        capability=capability,
        scope=scope,
        effect=args.effect,
        request_id=rt.request_id,
        ip_hash=NO_CLIENT_ADDRESS,
        actor_type=ActorType.SYSTEM,
        extra_metadata=provenance,
    )
    (_row, problems, connection_name), *_ = await annotate_grants(rt.db, workspace.id, [grant])
    verb = "kept     " if already == grant.id else "granted  "
    lines = [f"{verb} {capability}  {_scope_text(grant.scope_json)}"]
    if problems:
        lines.append(f"warning  needs attention: {' '.join(problems)}")
    return Result(
        data={
            **base,
            "grant": {
                "id": str(grant.id),
                "capability": grant.capability,
                "scope": grant.scope_json,
                "effect": grant.effect,
                "problems": problems,
                "connection_name": connection_name,
            },
        },
        lines=lines,
    )


async def agent_access(rt: Runtime) -> Result:
    workspace = await _resolve_workspace_or_only(rt.db, rt.args.workspace)
    agent = await _resolve_agent(rt.db, workspace, rt.args.agent)
    statuses = await agent_bundles(rt.db, workspace.id, agent.id)
    annotated = await list_grants(rt.db, workspace.id, agent.id)
    rules = parse_rules(list(agent.approval_policy_json or []))
    live_ids = {
        str(connection_id)
        for connection_id in await rt.db.scalars(
            select(Connection.id).where(
                Connection.workspace_id == workspace.id,
                Connection.status == ConnectionStatus.ACTIVE.value,
            )
        )
    }
    grants = [
        Grant(capability=row.capability, scope=row.scope_json, effect=GrantEffect(row.effect))
        for row, _problems, _name in annotated
    ]
    offered = [
        definition.name
        for definition in allowed_tool_definitions(
            build_default_definition_catalog(), grants, live_connection_ids=live_ids
        )
    ]
    dangling = [row for row, problems, _name in annotated if problems]

    lines = [f"{agent.name} ({agent.id}) in {workspace.name}", ""]
    lines.extend(f"{status.label:26} {status.state}" for status in statuses)
    lines.append("")
    if annotated:
        lines.extend(
            table(
                ["CAPABILITY", "SCOPE", "CONNECTION", "STATUS"],
                [
                    [
                        row.capability,
                        _scope_text(row.scope_json),
                        name or str(row.scope_json.get("connection_id") or "-"),
                        "ok" if not problems else "needs attention: " + " ".join(problems),
                    ]
                    for row, problems, name in annotated
                ],
            )
        )
    else:
        lines.append("No grants: this agent cannot call any tool.")
    lines.append("")
    if rules:
        lines.extend(
            f"rule  {rule.capability} risk={rule.risk.value if rule.risk else 'any'} -> "
            f"{rule.action.value}"
            for rule in rules
        )
    else:
        lines.append("No approval rules: risk-level defaults apply.")
    lines.append("")
    lines.append(
        "Would be offered (definition catalog, before task-kind scoping): "
        f"{len(offered)} tools: {', '.join(offered)}"
    )
    lines.append(f"Dangling grants: {len(dangling)}")
    return Result(
        data={
            "agent": _agent_out(agent),
            "workspace": _workspace_out(workspace),
            "bundles": [status.model_dump(mode="json") for status in statuses],
            "grants": [
                {
                    "id": str(row.id),
                    "capability": row.capability,
                    "scope": row.scope_json,
                    "effect": row.effect,
                    "connection_name": name,
                    "problems": problems,
                }
                for row, problems, name in annotated
            ],
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "would_be_offered": offered,
            "dangling_grants": len(dangling),
        },
        lines=lines,
    )


async def agent_revoke(rt: Runtime) -> Result:
    args = rt.args
    workspace = await _resolve_workspace_or_only(rt.db, args.workspace)
    agent = await _resolve_agent(rt.db, workspace, args.agent)
    actor = await _workspace_owner(rt.db, workspace)
    ctx = _console_context(actor, workspace.id)
    provenance = _provenance("agent revoke")
    base: dict[str, Any] = {"agent": _agent_out(agent), "workspace": _workspace_out(workspace)}

    if args.grant:
        try:
            grant_id = UUID(args.grant)
        except ValueError:
            raise CommandError(f"{args.grant!r} is not a grant id.") from None
        row = await rt.db.scalar(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.id == grant_id, AgentCapabilityGrant.agent_id == agent.id
            )
        )
        if row is None:
            raise CommandError(
                f"No grant {grant_id} on {agent.name}. `{PROGRAM} agent access` lists them."
            )
        if not confirm(
            f"Revoke {row.capability} ({_scope_text(row.scope_json)}) from {agent.name}?",
            assume_yes=args.yes,
        ):
            raise CommandError("Cancelled. Nothing was changed.")
        await revoke_grant(
            rt.db,
            ctx,
            agent.id,
            grant_id,
            request_id=rt.request_id,
            ip_hash=NO_CLIENT_ADDRESS,
            actor_type=ActorType.SYSTEM,
            extra_metadata=provenance,
        )
        return Result(
            data={**base, "revoked": [str(grant_id)]},
            lines=[f"revoked  {row.capability}  {_scope_text(row.scope_json)}"],
        )

    bundle = bundle_by_id(args.bundle)
    if bundle is None:
        raise CommandError(
            f"No capability bundle '{args.bundle}'. Choose one of: {', '.join(BUNDLE_IDS)}."
        )

    async def remove(*, dry_run: bool) -> BundleRemoveOut:
        return await remove_bundle(
            rt.db,
            ctx,
            agent.id,
            bundle.id,
            dry_run=dry_run,
            request_id=rt.request_id,
            ip_hash=NO_CLIENT_ADDRESS,
            actor_type=ActorType.SYSTEM,
            extra_metadata=provenance,
        )

    preview = await remove(dry_run=True)
    if not preview.revoked:
        return Result(
            data={**base, **preview.model_dump(mode="json")},
            lines=[f"Nothing to revoke: {bundle.label} is not on for {agent.name}."],
        )
    hand_made = ""
    if preview.hand_made:
        names = ", ".join(row.capability for row in preview.hand_made)
        hand_made = f", including {len(preview.hand_made)} you added by hand: {names}"
    if not confirm(
        f"Turn off {bundle.label} for {agent.name} in {workspace.name}? Revokes "
        f"{len(preview.revoked)} grants{hand_made}. Anything else the agent can do stays as "
        "it is.",
        assume_yes=args.yes,
    ):
        raise CommandError("Cancelled. Nothing was changed.")
    result = await remove(dry_run=False)
    return Result(
        data={**base, **result.model_dump(mode="json")},
        lines=[
            f"revoked  {row.capability}  {_scope_text(row.scope_json)}" for row in result.revoked
        ],
    )


COMMANDS: dict[str, Callable[[Runtime], Awaitable[Result]]] = {
    "doctor": doctor,
    "owner create": owner_create,
    "user create": user_create,
    "user list": user_list,
    "user set-password": user_set_password,
    "user set-role": user_set_role,
    "workspace create": workspace_create,
    "workspace list": workspace_list,
    "invite create": invite_create,
    "agent list": agent_list,
    "agent access": agent_access,
    "agent grant": agent_grant,
    "agent revoke": agent_revoke,
}
