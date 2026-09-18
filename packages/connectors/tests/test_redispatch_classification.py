"""Which tools say a repeat is safe, spelled out rather than inferred.

``redispatch_is_safe`` decides what recovery does with a call whose executor
was entered and whose outcome nobody can prove: run it again, or stop the run
and ask a person. That makes it a security-shaped declaration, and the wrong
kind of change to it is the quiet kind — a new tool copied from a neighbour,
or a ``True`` added to make a flaky test go away.

So the whole table is written out here, tool by tool, and the invariants that
give it meaning are asserted alongside it: nothing outside the two connectors
opts in by accident, and the flag is not a synonym for ``risk``.
"""

from __future__ import annotations

import pytest

from jhin_connectors import build_default_catalog
from jhin_connectors.cli.tools import CLI_TOOLS
from jhin_connectors.ghost.archive import ARCHIVE_TOOLS
from jhin_connectors.ghost.assignments import ASSIGNMENT_TOOLS
from jhin_connectors.ghost.setup import GHOST_SETUP_TOOLS
from jhin_connectors.ghost.tools import GHOST_TOOLS
from jhin_connectors.github.tools import GITHUB_TOOLS
from jhin_connectors.unsplash.tools import UNSPLASH_TOOLS
from jhin_policy import RiskLevel, ToolDefinition

# Yes: every effect stays on a disk Jhin owns, or is a read of somebody
# else's system. No: the call can change the outside world, and a second one
# could change it twice.
CLI_EXPECTED = {
    "cli.command.execute": False,  # arbitrary command, and may hold the internet open
    "cli.repository.checkout": True,  # egress, but every byte of it is a read
    "cli.repository.push": False,  # the tool at-most-once was written for
    "cli.test.run": True,  # arbitrary command, but no network and no credential
    "cli.file.list": True,  # a find over Jhin's own disk
    "cli.file.search": True,  # a grep over Jhin's own disk
    "cli.file.read": True,  # a read of Jhin's own disk
    "cli.file.edit": True,  # writes Jhin's disk, and refuses a repeat on its own
    "cli.file.write": True,  # writes Jhin's disk, guarded by the read token
    "cli.file.publish": True,  # immutable managed snapshot, idempotent by tool invocation
}

GITHUB_EXPECTED = {
    "github.repository.read": True,
    "github.repository.list": True,
    "github.branch.list": True,
    "github.file.read": True,
    "github.branch.create": False,
    "github.issue.read": True,
    "github.issue.comment": False,
    "github.pull_request.create": False,
    "github.pull_request.read": True,
    "github.pull_request.comment": False,
    "github.pull_request.merge": False,
    "github.check.read": True,
    "github.workflow.dispatch": False,
    "github.workflow_run.read": True,
}

GHOST_EXPECTED = {
    "ghost.connection.bind": False,
    "ghost.post.list": True,
    "ghost.post.read": True,
    "ghost.draft.create": False,
    "ghost.draft.update": False,
    "ghost.review.request": False,
    "ghost.review.read": True,  # immutable package chunks; repeated receipt is harmless
    "ghost.review.decide": False,
    "ghost.post.publish": False,
    "ghost.assignment.create": True,  # workspace lock and task identity deduplicate creation
    "ghost.assignment.read": True,
    "ghost.assignment.revise": True,  # expected version prevents applying a mutation twice
    "ghost.assignment.cancel": True,
    "ghost.assignment.attach_evidence": True,
    "ghost.archive.sync": True,  # attaches to a durable ingestion; provider effects are reads
    "ghost.archive.status": True,
    "ghost.archive.search": True,
    "ghost.archive.read": True,
}

GHOST_ALL_TOOLS = (*GHOST_TOOLS, *GHOST_SETUP_TOOLS, *ASSIGNMENT_TOOLS, *ARCHIVE_TOOLS)

UNSPLASH_EXPECTED = {
    "unsplash.connection.bind": False,
    "unsplash.photos.search": True,
    "unsplash.photos.select": False,  # download tracking is an external effect
}


def _definitions(tools: tuple[tuple[ToolDefinition, object], ...]) -> dict[str, ToolDefinition]:
    return {definition.name: definition for definition, _ in tools}


@pytest.mark.parametrize(("name", "expected"), sorted(CLI_EXPECTED.items()))
def test_cli_tools_declare_the_reviewed_redispatch_answer(name: str, expected: bool) -> None:
    assert _definitions(CLI_TOOLS)[name].redispatch_is_safe is expected


@pytest.mark.parametrize(("name", "expected"), sorted(GITHUB_EXPECTED.items()))
def test_github_tools_declare_the_reviewed_redispatch_answer(name: str, expected: bool) -> None:
    assert _definitions(GITHUB_TOOLS)[name].redispatch_is_safe is expected


@pytest.mark.parametrize(("name", "expected"), sorted(GHOST_EXPECTED.items()))
def test_ghost_redispatch_contract(name: str, expected: bool) -> None:
    assert _definitions(GHOST_ALL_TOOLS)[name].redispatch_is_safe is expected


@pytest.mark.parametrize(("name", "expected"), sorted(UNSPLASH_EXPECTED.items()))
def test_unsplash_redispatch_contract(name: str, expected: bool) -> None:
    assert _definitions(UNSPLASH_TOOLS)[name].redispatch_is_safe is expected


def test_the_table_covers_every_tool_these_connectors_register() -> None:
    """A tool added without a line in the table above fails here rather than
    inheriting somebody else's answer in silence."""
    assert set(_definitions(CLI_TOOLS)) == set(CLI_EXPECTED)
    assert set(_definitions(GITHUB_TOOLS)) == set(GITHUB_EXPECTED)
    assert set(_definitions(GHOST_ALL_TOOLS)) == set(GHOST_EXPECTED)
    assert set(_definitions(UNSPLASH_TOOLS)) == set(UNSPLASH_EXPECTED)


def test_nothing_else_in_the_catalog_opts_in_by_default() -> None:
    """The default is no. Every other connector and system tool keeps the
    behaviour it had before this field existed, which is what makes the
    change a narrowing rather than a loosening."""
    reviewed = (
        set(CLI_EXPECTED) | set(GITHUB_EXPECTED) | set(GHOST_EXPECTED) | set(UNSPLASH_EXPECTED)
    )
    opted_in = {
        definition.name
        for definition in build_default_catalog().definitions()
        if definition.redispatch_is_safe
    }
    assert opted_in <= reviewed


def test_the_answer_is_not_a_restatement_of_risk() -> None:
    """Two WRITE tools, opposite answers — which is the whole reason this is
    a field and not a lookup on ``risk``."""
    cli = _definitions(CLI_TOOLS)
    checkout, command = cli["cli.repository.checkout"], cli["cli.command.execute"]
    assert checkout.risk is RiskLevel.WRITE
    assert command.risk is RiskLevel.WRITE
    assert (checkout.redispatch_is_safe, command.redispatch_is_safe) == (True, False)


def test_no_elevated_tool_may_be_repeated() -> None:
    """Not the rule — the rule is about effects, not levels — but a true
    consequence of it, and a cheap tripwire if the two ever disagree."""
    for definition, _ in (*CLI_TOOLS, *GITHUB_TOOLS):
        if definition.risk is RiskLevel.ELEVATED:
            assert definition.redispatch_is_safe is False
