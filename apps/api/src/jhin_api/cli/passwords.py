"""Reading a new password without ever putting it on the command line.

There is deliberately no ``--password`` option anywhere in ``jhin-admin``, and
none may be added: every argument is visible in the process table to every
other user on the host, and shells write it to their history file. So the
value arrives one of two ways — typed at a prompt that echoes nothing, or
piped in behind ``--password-stdin``, the shape ``docker login`` uses for the
same reason.

The policy the value is held to is the account policy
(:mod:`jhin_api.security.passwords`), unchanged: an account made from the
console is exactly as strong as one made in the browser.
"""

from __future__ import annotations

import getpass
import sys

from jhin_api.cli.runtime import CommandError
from jhin_api.security.passwords import PasswordPolicyError, validate_password_strength


def _from_stdin() -> str:
    raw = sys.stdin.read()
    if not raw:
        raise CommandError("--password-stdin was given but nothing arrived on stdin.")
    # The trailing newline belongs to the shell, not to the password. Anything
    # else piped in is the operator's and is left alone.
    return raw.removesuffix("\n").removesuffix("\r")


def _from_prompt(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise CommandError(
            "There is no terminal to prompt on. Pipe the password in instead, for "
            'example: printf %s "$NEW_PASSWORD" | jhin-admin ... --password-stdin'
        )
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat password: ") != first:
        # Asked twice because a typo here locks somebody out of an account
        # that has just been created and has no other way back in.
        raise CommandError("The two passwords did not match. Nothing was changed.")
    return first


def read_new_password(*, email: str, from_stdin: bool, prompt: str = "New password: ") -> str:
    """Obtain a password for ``email`` and hold it to the account policy."""
    password = _from_stdin() if from_stdin else _from_prompt(prompt)
    try:
        validate_password_strength(password, email=email)
    except PasswordPolicyError as exc:
        raise CommandError(str(exc)) from exc
    return password
