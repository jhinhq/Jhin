"""The capability for choosing how you act and sound.

``organization.persona.self`` gates the persona tools an agent uses on its
own behalf: listing the workspace's personas, wearing one, taking one off,
and proposing a new card of its own. It is its own capability rather than a
corner of ``organization.manage_agents`` because those are different
questions — "may this agent change who it sounds like" is something an
admin should be able to answer with one deny grant, without also taking
away its ability to remember or to ask a colleague.

The capability is not the whole lock. Creating a card is elevated risk and
parks on a human under the default policy, and a persona can never widen
what an agent may do: the rendered block is prefixed with a guardrail
saying so, and every card passes ``jhin_personas``' content rules before it
is stored. Assigning a persona to *another* agent stays under
``organization.manage_agents`` plus the manager-chain rule, exactly like
rewriting a report's system prompt.

This module is pure (no I/O).
"""

from __future__ import annotations

from typing import Any

PERSONA_SELF_CAPABILITY = "organization.persona.self"


def persona_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Choosing a persona is not a privilege: the card shapes how an agent
    says things, never what it may do, so every new agent may pick one.
    Unscoped, because the bound lives in the tools — a card must pass the
    content rules, creating one needs a person's approval, and dressing a
    colleague needs the manager-chain rule under a different capability."""
    return ((PERSONA_SELF_CAPABILITY, {}),)


__all__ = ["PERSONA_SELF_CAPABILITY", "persona_grant_specs"]
