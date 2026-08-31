"""The ``jhin-admin`` command line.

Standard-library argparse: the API has no CLI framework in its dependency tree
and an administration tool is not a reason to put one there.
"""

from __future__ import annotations

import argparse

from jhin_domain import WorkspaceRole

PROGRAM = "jhin-admin"

_ROLE_NAMES = ", ".join(role.value for role in WorkspaceRole)


def workspace_role(value: str) -> WorkspaceRole:
    try:
        return WorkspaceRole(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a workspace role; choose one of {_ROLE_NAMES}"
        ) from None


def _json_flag() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--json", action="store_true", help="print the result as a JSON object instead"
    )
    return parent


def _password_flag() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    # Deliberately not --password. An argument is readable by every process on
    # the host and is written to shell history; the password arrives on stdin
    # or from a prompt. Please do not add one (see jhin_api.cli.passwords).
    parent.add_argument(
        "--password-stdin",
        action="store_true",
        dest="password_stdin",
        help="read the password from stdin instead of prompting, for automation",
    )
    return parent


def _confirmation_flag() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--yes", action="store_true", help="answer the confirmation prompt in advance"
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _json_flag()
    password = _password_flag()
    confirmation = _confirmation_flag()

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Administer a self-hosted Jhin from the machine it runs on: check the "
            "install, create accounts and workspaces, recover a locked-out owner."
        ),
        epilog=f"Run `{PROGRAM} <group> --help` for the arguments of one command.",
    )
    groups = parser.add_subparsers(dest="group", metavar="<group>")

    doctor = groups.add_parser(
        "doctor",
        parents=[common],
        help="report on this install: database, migrations, master key, contents",
    )
    doctor.set_defaults(command="doctor")

    owner = groups.add_parser(
        "owner", help="the first owner account (what the /setup page does)"
    ).add_subparsers(dest="action", metavar="<action>", required=True)
    owner_create = owner.add_parser(
        "create",
        parents=[common, password],
        help="create the first owner and their workspace on an empty install",
    )
    owner_create.add_argument("--email", required=True, help="the owner's email address")
    owner_create.add_argument("--name", required=True, help="the owner's display name")
    owner_create.add_argument("--workspace", required=True, help="name for the first workspace")
    owner_create.set_defaults(command="owner create")

    user = groups.add_parser("user", help="accounts and their workspace roles").add_subparsers(
        dest="action", metavar="<action>", required=True
    )
    user_create = user.add_parser(
        "create",
        parents=[common, password, confirmation],
        help="create an account and add it to an existing workspace",
    )
    user_create.add_argument("--email", required=True, help="the new account's email address")
    user_create.add_argument("--name", required=True, help="the new account's display name")
    user_create.add_argument("--workspace", required=True, help="workspace slug or id to join")
    user_create.add_argument(
        "--role", required=True, type=workspace_role, help=f"role in that workspace: {_ROLE_NAMES}"
    )
    user_create.set_defaults(command="user create")

    user_list = user.add_parser("list", parents=[common], help="list accounts")
    user_list.add_argument(
        "--workspace", default=None, help="only members of this workspace (slug or id)"
    )
    user_list.set_defaults(command="user list")

    user_set_password = user.add_parser(
        "set-password",
        parents=[common, password, confirmation],
        help="set a new password and sign the account out everywhere",
    )
    user_set_password.add_argument("--email", required=True, help="the account to recover")
    user_set_password.set_defaults(command="user set-password")

    user_set_role = user.add_parser(
        "set-role", parents=[common, confirmation], help="change an account's role in a workspace"
    )
    user_set_role.add_argument("--email", required=True, help="the account to change")
    user_set_role.add_argument("--workspace", required=True, help="workspace slug or id")
    user_set_role.add_argument(
        "--role", required=True, type=workspace_role, help=f"the new role: {_ROLE_NAMES}"
    )
    user_set_role.set_defaults(command="user set-role")

    workspace = groups.add_parser("workspace", help="workspaces").add_subparsers(
        dest="action", metavar="<action>", required=True
    )
    workspace_create = workspace.add_parser(
        "create", parents=[common], help="create a workspace owned by an existing account"
    )
    workspace_create.add_argument("--name", required=True, help="name for the workspace")
    workspace_create.add_argument("--owner", required=True, help="email of the owning account")
    workspace_create.set_defaults(command="workspace create")

    workspace_list = workspace.add_parser("list", parents=[common], help="list workspaces")
    workspace_list.set_defaults(command="workspace list")

    invite = groups.add_parser(
        "invite", help="invitation links, for people who should choose their own password"
    ).add_subparsers(dest="action", metavar="<action>", required=True)
    invite_create = invite.add_parser(
        "create", parents=[common], help="mint an invitation link and print it once"
    )
    invite_create.add_argument("--email", required=True, help="who the invitation is for")
    invite_create.add_argument("--workspace", required=True, help="workspace slug or id")
    invite_create.add_argument(
        "--role", required=True, type=workspace_role, help=f"role they will hold: {_ROLE_NAMES}"
    )
    invite_create.set_defaults(command="invite create")

    return parser
