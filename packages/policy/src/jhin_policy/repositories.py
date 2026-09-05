"""What a repository name may look like, stated positively (plan 7.5).

Shared by the CLI connector's allow-list validator, the grant writers, and
the bundle planner, so every place that joins a repository onto a URL or
compares it against a connection's allow-list agrees on what a name *is*.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

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

# A pattern segment is a name segment that may also contain ``*``.
_PATTERN_SEGMENT = re.compile(r"^[A-Za-z0-9_.*-]+$")


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


def is_repository_pattern(value: str) -> bool:
    """``*`` alone, or exactly two ``/``-separated segments of name characters
    plus ``*``, neither segment ``.`` or ``..``.

    Accepts ``octo/alpha``, ``octo/*``, ``*/*``; refuses URLs, ``../x``,
    ``owner/name/extra`` and a blank value. This is the shape a grant's
    ``repository`` scope and a sandbox's allow-list entry may take.
    """
    if value == ANY_REPOSITORY:
        return True
    segments = value.split("/")
    return len(segments) == 2 and all(
        segment not in (".", "..") and _PATTERN_SEGMENT.match(segment) is not None
        for segment in segments
    )


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


def repository_covered_by_allow_list(entry: str, allowed: Sequence[str]) -> bool:
    """Is a grant's ``repository`` entry inside a sandbox's allow-list?

    ``*`` in ``allowed`` covers everything. A plain ``owner/name`` entry is
    covered when some allowed pattern matches it. A pattern entry (one that
    contains ``*``) is covered only when it appears verbatim in ``allowed`` —
    ``octo/*`` is not inside ``octo/alpha``, and deciding whether one glob is
    inside another is not something a grant writer should guess at.
    """
    if ANY_REPOSITORY in allowed:
        return True
    if "*" in entry:
        return entry in allowed
    return any(repository_matches(pattern, entry) for pattern in allowed)


__all__ = [
    "ANY_REPOSITORY",
    "is_plain_repository",
    "is_repository_pattern",
    "repository_covered_by_allow_list",
    "repository_matches",
]
