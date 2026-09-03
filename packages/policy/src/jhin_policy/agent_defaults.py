"""What every new agent starts able to do.

Deny-by-default is unchanged by this module: the gateway still re-decides
every call against live grants, scopes, rules, and tool validators. What
changes is the *default grant set* a creation site applies — the difference
between an agent an admin has to hand-configure before it is a colleague and
one that can already talk to people and remember what they tell it.

This module is pure (no I/O).
"""

from __future__ import annotations

from typing import Any

from jhin_policy.ask_person import ASK_PERSON_CAPABILITY
from jhin_policy.memory import MEMORY_PROPOSE_CAPABILITY, MEMORY_READ_CAPABILITY
from jhin_policy.personas import persona_grant_specs
from jhin_policy.work_requests import collaboration_grant_specs


def memory_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Remembering is not a privilege. An agent that cannot remember is a
    colleague with amnesia, and the product promise is a company of agents
    that learn how it works. Nothing here widens what memory *policy*
    allows: ``memory.propose`` still routes through ``jhin_memory.policy``,
    an agent-scoped memory is the only one an ordinary chat can reach on its
    own, and a wider one still needs a person's answer."""
    return ((MEMORY_READ_CAPABILITY, {}), (MEMORY_PROPOSE_CAPABILITY, {}))


def ask_person_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Asking the person you are talking to is not an escalation. The tool
    is advertised only on a chat turn and denied by ``validate_ask_person``
    anywhere else, it is bounded to three questions a run, and a repeat is
    refused without reaching anyone."""
    return ((ASK_PERSON_CAPABILITY, {}),)


def default_agent_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Every grant a new agent starts with. Deny-by-default is unchanged:
    this is the platform's default *grant set*, and the gateway still
    re-decides every call against live grants."""
    return (
        collaboration_grant_specs()
        + memory_grant_specs()
        + ask_person_grant_specs()
        + persona_grant_specs()
    )


__all__ = [
    "ask_person_grant_specs",
    "default_agent_grant_specs",
    "memory_grant_specs",
    "persona_grant_specs",
]
