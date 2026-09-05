"""What a repository name may look like, shared by grants and allow-lists."""

from __future__ import annotations

import pytest

from jhin_connectors.cli.validators import (
    is_plain_repository as connector_is_plain_repository,
)
from jhin_connectors.cli.validators import (
    repository_matches as connector_repository_matches,
)
from jhin_policy import (
    is_plain_repository,
    is_repository_pattern,
    repository_covered_by_allow_list,
    repository_matches,
)


@pytest.mark.parametrize("value", ["*", "octo/alpha", "octo/*", "*/*", "octo/alpha.js"])
def test_repository_patterns_that_are_accepted(value: str) -> None:
    assert is_repository_pattern(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/octo/alpha",
        "../x",
        "a/b/c",
        "",
        "octo",
        "octo/",
        "/alpha",
        "octo/..",
        "./alpha",
        "octo/al pha",
    ],
)
def test_repository_patterns_that_are_refused(value: str) -> None:
    assert not is_repository_pattern(value)


def test_star_in_the_allow_list_covers_everything() -> None:
    assert repository_covered_by_allow_list("octo/alpha", ["*"])
    assert repository_covered_by_allow_list("octo/*", ["*"])
    assert repository_covered_by_allow_list("*", ["*"])


def test_a_plain_entry_is_covered_through_repository_matches() -> None:
    assert repository_covered_by_allow_list("octo/alpha", ["octo/*"])
    assert repository_covered_by_allow_list("octo/alpha", ["octo/alpha", "other/x"])
    assert not repository_covered_by_allow_list("octo/alpha", ["other/*"])
    assert not repository_covered_by_allow_list("octo/alpha", [])


def test_a_pattern_entry_is_covered_only_verbatim() -> None:
    assert repository_covered_by_allow_list("octo/*", ["octo/*"])
    # ``octo/*`` is wider than ``octo/alpha``; no guessing about globs.
    assert not repository_covered_by_allow_list("octo/*", ["octo/alpha"])
    assert not repository_covered_by_allow_list("*/*", ["octo/*"])


def test_plain_repository_and_matches_moved_intact() -> None:
    """The connector re-exports the same functions it used to own."""
    assert connector_is_plain_repository is is_plain_repository
    assert connector_repository_matches is repository_matches
    assert is_plain_repository("octo/alpha")
    assert not is_plain_repository("../x")
    assert not is_plain_repository("octo/alpha/extra")
    assert repository_matches("*", "octo/alpha")
    assert repository_matches("octo/*", "octo/alpha")
    assert not repository_matches("octo*", "octo-labs/anything")
    assert not repository_matches("*", "../evil")
