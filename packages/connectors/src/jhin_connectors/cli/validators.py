"""The repository allow-list, as a :class:`ToolValidator` (plan 7.5).

A grant's ``repository`` scope is per agent. This is the other half: a
per-connection statement of which repositories *this instance* may touch at
all, so an operator has one place to answer "what can these agents reach"
without auditing every agent's grants.

It is deny-by-default. A CLI connection with no ``allowed_repositories`` does
no repository work, and the denial names the allowed set so the model
self-corrects in one step instead of retrying blind.

The gateway runs validators at three points — policy decision, approval
resume, and execution bind — so narrowing the list invalidates an approval
that is already parked, exactly as rotating the connection's credential does.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import cast

from pydantic import BaseModel

from jhin_connectors.execution import ConnectionResolutionError
from jhin_policy import DecisionType, Grant, PolicyDecision
from jhin_tools.builtin import ToolExecutionContext

# The one entry that means "every repository", written by migration 0038 for
# connections that predate the list. Every other entry is matched segment by
# segment.
ANY_REPOSITORY = "*"

# What a repository segment may be made of, stated positively.
#
# The negative form — "not '', not '.', not '..'" — only refuses the three
# spellings of a traversal somebody thought to list, and a URL path has more
# than three: ``..%2fevil`` is one segment to ``str.split('/')`` and two to
# every server that percent-decodes it, and ``.%2e`` is ``..`` to the same
# server. Neither is refused by naming spellings, and both are refused by
# saying what a name *is*. That is the whole of GitHub's own owner and
# repository character set, minus the two dot-only names, so nothing a real
# repository can be called is lost.
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_plain_repository(repository: str) -> bool:
    """Is this ``owner/name`` a pair of ordinary names?

    True only for a value that stays a name when it is joined onto a URL —
    which is what every caller does with it: the clone URL Jhin builds, the
    ``/repos/<repository>`` API paths, and the credential scope both are
    written around. Deliberately stricter than the schema's pattern, and
    checked again beside each of those joins, so a caller that never passed
    through the schema cannot skip it.
    """
    segments = repository.split("/")
    return len(segments) == 2 and all(
        segment not in (".", "..") and _SEGMENT.match(segment) is not None for segment in segments
    )


class _RepositoryCall(BaseModel):
    connection_id: str
    repository: str


def repository_matches(pattern: str, repository: str) -> bool:
    """One allow-list entry against one ``owner/name``.

    ``*`` on its own is the deliberate "every repository" entry. Every other
    pattern is matched a segment at a time, because ``fnmatch``'s ``*`` also
    matches ``/``: ``octo*`` would otherwise cover ``octo-labs/anything``, and
    a bare ``*`` would cover anything a repository name could be made to look
    like. An entry naming a different number of segments matches nothing.

    Anything that is not a plain ``owner/name`` is refused whatever the entry
    says, ``*`` included: ``../evil`` and ``..%2fevil/x`` are not repositories
    this connection is allowed *broadly*, they are values that stop being names
    as soon as they are joined onto a URL.
    """
    if not is_plain_repository(repository):
        return False
    if pattern == ANY_REPOSITORY:
        return True
    pattern_parts = pattern.split("/")
    repository_parts = repository.split("/")
    if len(pattern_parts) != len(repository_parts):
        return False
    return all(
        fnmatchcase(part, expected)
        for expected, part in zip(pattern_parts, repository_parts, strict=True)
    )


async def repository_allow_list_validator(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    grants: Sequence[Grant],
) -> PolicyDecision | None:
    """Deny a checkout or push whose repository the connection does not allow."""
    from jhin_connectors.cli.tools import _load_cli_connection, allowed_repositories

    data = cast(_RepositoryCall, payload)
    repository = getattr(data, "repository", "")
    connection_id = getattr(data, "connection_id", "")
    if not repository:
        return None

    try:
        connection = await _load_cli_connection(ctx, connection_id)
    except ConnectionResolutionError as exc:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="sandbox_connection_unavailable",
            reason=str(exc),
        )

    allowed = allowed_repositories(connection)
    if not allowed:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="repository_not_allowed",
            reason=(
                f"connection '{connection.name}' allows no repositories; an "
                "admin must list them in the connection's settings"
            ),
        )
    if any(repository_matches(pattern, repository) for pattern in allowed):
        return None
    return PolicyDecision(
        decision=DecisionType.DENY,
        code="repository_not_allowed",
        reason=(f"connection '{connection.name}' allows only: " + ", ".join(sorted(allowed))),
    )


__all__ = ["is_plain_repository", "repository_allow_list_validator", "repository_matches"]
