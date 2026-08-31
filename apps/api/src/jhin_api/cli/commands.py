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
from sqlalchemy.engine import Connection
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
from jhin_api.security.passwords import hash_password
from jhin_api.settings import get_settings
from jhin_api.workspaces import service as workspaces
from jhin_db.migrate import alembic_config
from jhin_db.models import Agent, User, Workspace, WorkspaceMembership
from jhin_domain import ActorType, UserStatus, WorkspaceRole
from jhin_secrets import load_master_key
from jhin_secrets.crypto import MasterKeyError

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


def _applied_revision(connection: Connection) -> str | None:
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
}
