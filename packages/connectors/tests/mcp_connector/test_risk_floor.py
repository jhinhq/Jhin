"""The name-based risk floor: what a tool is called, when the server said
nothing about it.

``write`` is the default for an unannotated tool and ``write`` runs without
asking, so a server that ships no annotations would have its deletions and
its SQL auto-executed. These tests pin the two verb families that raise that
default, the whole-word matching that keeps them from over-firing, and the
two directions the floor must never move: down from an annotation, and up
past an explicit ``readOnlyHint``.
"""

import pytest
from mcp import types as mcp_types

from jhin_connectors.mcp.discovery import discovered_from_mcp, risk_floor_for_name
from jhin_policy import RiskLevel


@pytest.mark.parametrize(
    "name",
    [
        "delete_project",
        "remove_member",
        "drop_table",
        "purge_cache",
        "destroy_environment",
        "truncate_table",
        "revoke_token",
        "uninstall_app",
        "cancel_deployment",
        "archive_issue",
        # camelCase and punctuation are the same words wearing different
        # clothes; the provider's spelling should not decide the risk.
        "deleteIssue",
        "Delete-Branch",
    ],
)
def test_destructive_verbs_floor_at_destructive(name: str) -> None:
    assert risk_floor_for_name(name) == RiskLevel.DESTRUCTIVE


@pytest.mark.parametrize(
    "name",
    [
        "execute_sql",
        "run_query",
        "execute",
        "exec_statement",
        "run_command",
        "eval_expression",
        "SQLQuery",
    ],
)
def test_arbitrary_execution_shapes_floor_at_elevated(name: str) -> None:
    assert risk_floor_for_name(name) == RiskLevel.ELEVATED


@pytest.mark.parametrize(
    "name",
    [
        # A listing of things already deleted reads nothing; only the verb
        # itself counts, never a word that merely contains it.
        "list_deleted_issues",
        "get_archived_projects",
        "undelete_page",
        "executor_status",
        "get_issue",
        "create_page",
        "update_document",
        "search_docs",
    ],
)
def test_names_that_only_look_dangerous_set_no_floor(name: str) -> None:
    assert risk_floor_for_name(name) is None


def test_a_destructive_verb_wins_over_an_execution_shape() -> None:
    """One name can say both. The more severe reading is the safe one."""
    assert risk_floor_for_name("execute_delete_sql") == RiskLevel.DESTRUCTIVE


def _discover(tool: mcp_types.Tool) -> RiskLevel:
    discovered = discovered_from_mcp([tool])
    assert len(discovered) == 1
    return discovered[0].derived_risk


def test_discovery_raises_an_unannotated_destructive_name() -> None:
    """Without the floor this is ``write``, which the policy defaults
    auto-execute — the hole that opens as soon as remote MCP servers become
    the default way curated apps connect."""
    tool = mcp_types.Tool(name="delete_project", inputSchema={"type": "object"})
    assert _discover(tool) == RiskLevel.DESTRUCTIVE


def test_an_explicit_read_only_hint_beats_the_name() -> None:
    """A provider saying its own tool only reads is better evidence than our
    reading of what it called it."""
    tool = mcp_types.Tool(
        name="query_documents",
        inputSchema={"type": "object"},
        annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
    )
    assert _discover(tool) == RiskLevel.READ


def test_an_annotated_destructive_tool_is_never_lowered() -> None:
    tool = mcp_types.Tool(
        name="run_query",
        inputSchema={"type": "object"},
        annotations=mcp_types.ToolAnnotations(destructiveHint=True),
    )
    assert _discover(tool) == RiskLevel.DESTRUCTIVE


def test_an_ordinary_unannotated_tool_keeps_the_write_default() -> None:
    tool = mcp_types.Tool(name="create_issue", inputSchema={"type": "object"})
    assert _discover(tool) == RiskLevel.WRITE
