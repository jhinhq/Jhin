"""What an agent is doing right now, in words a person reads.

The chat said "Working…" for every kind of work — saving a memory, running
the tests, waiting on a colleague, all the same three dots. These are the
sentences it says instead.

Rendered here, on the server. The same vocabulary is wanted by the chat
header and by the approval and review cards, and a mapping kept in both
TypeScript and Python drifts until the two surfaces disagree about what the
agent is doing.

**Only a tool's name reaches a phrase.** Tool arguments carry workspace
content and credentials-adjacent material; the sanitizers in
``jhin_tools.sanitize`` exist for payloads, and this surface simply does not
take one — there is no argument to leak because none is passed in. That is
also why an unrecognized tool falls back to its family ("Reading from
GitHub") or to nothing at all, and never to the raw identifier: "mcp devmcp
echo" is not a sentence, and a name the model invented is not a fact.
"""

from __future__ import annotations

import re

# Present participle, no trailing ellipsis — the UI owns the animation — and
# naming the *thing* rather than the tool: a person cares that it is saving a
# memory, not that the capability is called ``memory.propose``.
_EXACT: dict[str, str] = {
    "memory.propose": "Saving this to memory",
    "memory.search": "Checking what it remembers",
    # The skill's own name is a tool argument, so it stays out. See the
    # module docstring: this surface takes tool names and nothing else.
    "skills.read": "Reading a skill",
    "skills.create": "Writing a skill",
    "skills.update": "Writing a skill",
    "organization.persona.list": "Looking through personas",
    "organization.persona.create": "Writing a persona",
    "organization.persona.assign_self": "Choosing a persona",
    "organization.persona.assign": "Choosing a persona",
    "organization.request_work": "Asking a colleague",
    "organization.respond_work_request": "Answering a colleague",
    "organization.colleague_status": "Checking what a colleague is working on",
    "organization.directory.search": "Looking someone up",
    "organization.delegate_task": "Handing work to a colleague",
    "organization.report_result": "Writing up the result",
    "organization.review.request": "Asking for a review",
    "organization.review.submit": "Reviewing work",
    "organization.create_agent": "Changing the organization",
    "organization.create_team": "Changing the organization",
    "organization.update_agent_profile": "Changing the organization",
    # The *parked* state belongs to the ask-a-question contract, which
    # projects it through the run status. This is only the moment the
    # question is being written, which would otherwise read as "Working".
    "organization.ask_person": "Asking you a question",
    "web.search": "Searching the web",
    "web.fetch": "Reading a page",
    "cli.file.read": "Reading the code",
    "cli.repository.checkout": "Reading the code",
    "cli.file.write": "Editing the code",
    "cli.command.execute": "Running a command",
    "cli.test.run": "Running the tests",
    "system.time": "Checking the time",
}

# Connector families, where the exact tool list grows without this file. The
# last segment says whether the call reads or changes something, which is the
# only distinction a person watching actually needs.
_FAMILIES: dict[str, tuple[str, str]] = {
    # family -> (reading, changing)
    "github": ("Reading from GitHub", "Making a change in GitHub"),
    "linear": ("Reading from Linear", "Updating Linear"),
    "supabase": ("Reading the database", "Changing the database"),
    # Every Vercel tool is about one thing, read or write alike.
    "vercel": ("Working with the deployment", "Working with the deployment"),
}

_READING_ACTIONS = frozenset({"read", "list", "search", "logs"})

_MCP_PREFIX = "mcp."
# The same shape the MCP connector manifest enforces on an admin-supplied
# server slug. Checked rather than trusted because a *denied* call persists
# whatever name the model asked for, and that name must not become a
# sentence on somebody's screen.
_MCP_SERVER = re.compile(r"^[a-z0-9_]{1,32}$")

# Tools with no label: internal plumbing and demo tools whose names would
# tell a person nothing. They fall through to the generic "Working".
_UNLABELLED = frozenset(
    {"system.echo", "system.note.append", "system.demo.elevated", "system.demo.destructive"}
)


def activity_phrase(tool_name: str) -> str | None:
    """The sentence for one tool call, or None when there is nothing worth saying.

    Takes a tool *name* and nothing else, by design.
    """
    name = tool_name.strip()
    if not name or name in _UNLABELLED:
        return None
    exact = _EXACT.get(name)
    if exact is not None:
        return exact
    if name.startswith(_MCP_PREFIX):
        server = name[len(_MCP_PREFIX) :].split(".", 1)[0]
        return f"Using {server}" if _MCP_SERVER.match(server) else None
    family = _FAMILIES.get(name.split(".", 1)[0])
    if family is None:
        return None
    reading, changing = family
    return reading if name.rsplit(".", 1)[-1] in _READING_ACTIONS else changing


def waiting_for_colleague_phrase(colleague_name: str) -> str:
    """Parked on a colleague's answer — a wait a person currently sees as
    "Working…" for up to two minutes."""
    name = colleague_name.strip()
    return f"Waiting for {name}" if name else "Waiting for a colleague"
