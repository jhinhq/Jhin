"""What a ``jhin-admin`` command is handed, what it hands back, and how it
asks before doing something consequential.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# Audit rows carry a hash of the caller's address (jhin_api.deps). There is no
# remote caller behind a console command, so the column stays null rather than
# recording the hash of a fiction.
NO_CLIENT_ADDRESS: str | None = None


class CommandError(Exception):
    """A failure the operator can act on, shown as a sentence.

    Everything a command can foresee — an unknown workspace, a password the
    policy rejects, a bootstrap that already happened — is raised as this and
    reaches the terminal as one line of advice rather than a traceback out of
    a container log.
    """


@dataclass(frozen=True)
class Runtime:
    """Everything a command needs, opened once per invocation."""

    args: argparse.Namespace
    db: AsyncSession
    # ``doctor`` inspects the connection itself (the applied Alembic revision
    # lives outside the ORM), so commands get the engine as well as a session.
    engine: AsyncEngine
    database_url: str
    # One request id shared by every audit row this invocation writes, so the
    # rows of a single command read back together — as the dev seed does.
    request_id: UUID


@dataclass(frozen=True)
class Result:
    """What a command produced.

    ``data`` is what ``--json`` prints and ``lines`` is what a person reads;
    both are built from the same run, so a script and an operator can never be
    told different things.
    """

    data: dict[str, Any]
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0


def confirm(question: str, *, assume_yes: bool) -> bool:
    """Ask before granting privilege or overwriting a credential.

    Only asked at a terminal: a piped stdin belongs to the password on it, and
    an automation that has no way to answer must pass ``--yes`` rather than
    hang forever on a prompt nobody will ever see.
    """
    if assume_yes or not sys.stdin.isatty():
        return True
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Left-aligned columns, each as wide as the widest thing in it."""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]

    def render(cells: Sequence[str]) -> str:
        padded = (cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
        return "  ".join(padded).strip()

    return [render(headers), render(["-" * width for width in widths]), *(render(r) for r in rows)]


def fields(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """A block of ``label   value`` lines with the values lined up."""
    width = max(len(label) for label, _ in pairs)
    return [f"{label.ljust(width)}   {value}" for label, value in pairs]


def emit(result: Result, *, as_json: bool) -> None:
    """Print the result.

    What a command prints stays ASCII. This runs on whatever terminal an
    operator happens to have, and a mangled dash in the middle of "migrations
    pending" helps nobody — the typography belongs in the comments.
    """
    if as_json:
        print(json.dumps(result.data, indent=2, sort_keys=True, default=str))
        return
    for line in result.lines:
        print(line)
