"""The tool-name → sentence mapping the chat shows instead of "Working…"."""

import inspect

import pytest

from jhin_domain import activity_phrase, waiting_for_colleague_phrase


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("memory.propose", "Saving this to memory"),
        ("memory.search", "Checking what it remembers"),
        ("skills.read", "Reading a skill"),
        ("skills.create", "Writing a skill"),
        ("organization.request_work", "Asking a colleague"),
        ("organization.review.submit", "Reviewing work"),
        ("organization.create_team", "Changing the organization"),
        ("web.search", "Searching the web"),
        ("web.fetch", "Reading a page"),
        ("cli.file.write", "Editing the code"),
        ("cli.test.run", "Running the tests"),
        ("system.time", "Checking the time"),
    ],
)
def test_named_tools_get_the_words_for_the_thing_not_the_tool(
    tool_name: str, expected: str
) -> None:
    assert activity_phrase(tool_name) == expected


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        # Reading and changing are the only distinction a person watching
        # needs, so a connector can grow tools without touching the mapping.
        ("github.issue.read", "Reading from GitHub"),
        ("github.branch.list", "Reading from GitHub"),
        ("github.pull_request.create", "Making a change in GitHub"),
        ("github.pull_request.merge", "Making a change in GitHub"),
        ("linear.issue.search", "Reading from Linear"),
        ("linear.comment.create", "Updating Linear"),
        ("supabase.database.read", "Reading the database"),
        ("supabase.logs.read", "Reading the database"),
        ("supabase.database.destructive", "Changing the database"),
        ("supabase.function.deploy", "Changing the database"),
        ("vercel.deployment.read", "Working with the deployment"),
        ("vercel.deployment.promote", "Working with the deployment"),
    ],
)
def test_a_tool_the_mapping_never_heard_of_falls_back_to_its_family(
    tool_name: str, expected: str
) -> None:
    assert activity_phrase(tool_name) == expected


def test_an_mcp_tool_names_the_server_never_the_raw_identifier() -> None:
    """A raw identifier like `mcp devmcp echo` is not a sentence. The slug is."""
    assert activity_phrase("mcp.devmcp.echo") == "Using devmcp"
    assert activity_phrase("mcp.notion.search_pages") == "Using notion"


@pytest.mark.parametrize(
    "tool_name",
    [
        # A denied tool call persists whatever name the model asked for, so
        # the slug is checked against the connector manifest's own shape
        # rather than trusted into a sentence on somebody's screen.
        "mcp.<img src=x>.echo",
        "mcp.Drop Table.echo",
        "mcp." + "a" * 33 + ".echo",
        "mcp..echo",
    ],
)
def test_a_server_name_that_is_not_a_slug_is_not_rendered(tool_name: str) -> None:
    assert activity_phrase(tool_name) is None


@pytest.mark.parametrize(
    "tool_name",
    ["system.echo", "system.note.append", "system.demo.elevated", "totally.unknown", "", "   "],
)
def test_plumbing_and_strangers_say_nothing_at_all(tool_name: str) -> None:
    """No label, so the caller falls through to the generic "Working" rather
    than showing a raw identifier."""
    assert activity_phrase(tool_name) is None


def test_the_phrase_takes_a_tool_name_and_nothing_else() -> None:
    """Tool arguments carry workspace content and credentials-adjacent
    material. This surface cannot leak one because it cannot accept one, and
    that is a signature guarantee rather than a discipline."""
    parameters = inspect.signature(activity_phrase).parameters
    assert list(parameters) == ["tool_name"]
    assert parameters["tool_name"].annotation == "str"


def test_no_phrase_trails_off_because_the_ui_owns_the_animation() -> None:
    for tool_name in ("memory.propose", "cli.test.run", "mcp.notion.x", "github.issue.read"):
        phrase = activity_phrase(tool_name)
        assert phrase is not None
        assert not phrase.endswith((".", "…", "..."))
        assert phrase[0].isupper()


def test_waiting_on_a_colleague_uses_their_name_and_survives_not_having_one() -> None:
    assert waiting_for_colleague_phrase("Nova") == "Waiting for Nova"
    assert waiting_for_colleague_phrase("  Nova  ") == "Waiting for Nova"
    assert waiting_for_colleague_phrase("") == "Waiting for a colleague"
