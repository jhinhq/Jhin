"""What an agent is called, and the capability for changing it.

``organization.identity.self`` gates the one tool an agent uses to set its
own name. It is a twin of ``organization.persona.self``: a separate
capability rather than a corner of ``organization.manage_agents``, so an
admin can answer "may this agent decide what it is called" with one deny
grant without also taking away its ability to remember or to ask a
colleague.

It is deliberately **not** in the ``agent.`` namespace. ``agent.permission``,
``agent.grant`` and ``agent.policy`` are forbidden prefixes, and a capability
called ``agent.identity.self`` would have made ``agent.*`` look like an
ordinary grantable subtree to anyone writing grants by hand — see
``grant_pattern_problem``, which now refuses a subtree that is a proper
ancestor of a forbidden prefix.

The other half of this module is the name itself. An agent's name is the
first agent-written string that lands in **layer 1** of its own system
prompt, unhedged ("You are Bisby, an AI teammate…"), and it is read back by
every colleague through the workspace roster. Nothing downstream scrubs that
layer. So the rule here is an allow-list, not a deny-list:

1. NFKC normalize and collapse whitespace (a normalization, so a name typed
   with a curly apostrophe or a non-breaking space keeps working rather than
   being refused);
1b. reject a line break or a tab, read *before* the collapse. Collapsing is
   what hid them: "Bisby\\nDoorstop" was stored as "Bisby Doorstop" with a
   200, so a name shaped like an injection payload was quietly repaired into
   a name nobody typed instead of being refused;
2. reject every character in Unicode category C — control, format,
   surrogate, private-use, unassigned. That is where zero-width joiners and
   right-to-left overrides live, and a name is never the place for them;
3. allow letters, digits, spaces, hyphens, apostrophes and full stops, and
   nothing else, with at least one letter in it;
4. at most 48 characters and at most three words;
5. finally the persona content rules (``jhin_personas.check_content``) —
   the same bar a persona facet clears, because the name sits *above* the
   persona block in the prompt. "You are now" and "New instructions" are
   two words each and would otherwise fit inside the limits above.

:func:`render_safe_agent_name` is the render-time backstop and is
deliberately *narrower* than :func:`agent_name_problem`. It asks only "is
this text safe to splice into the prompt", because a name that predates
these rules, or one an admin set through some other path, must keep
rendering as itself rather than being silently replaced. Only text that
could break the prompt open — invisible characters, nothing at all, or an
unbounded string — falls back to the canonical name (the agent's slug,
which is generated from ``[a-z0-9-]`` and cannot carry any of that).

This module is pure (no I/O).
"""

from __future__ import annotations

import unicodedata
from typing import Any

from jhin_personas import ContentRuleError, check_content

IDENTITY_SELF_CAPABILITY = "organization.identity.self"

# Short enough to read as a name in one glance at the top of a prompt and in
# a roster of 85 colleagues, long enough for "Marketing Director".
MAX_AGENT_NAME_CHARS = 48
MAX_AGENT_NAME_WORDS = 3
# The ``agent.name`` column. A stored name longer than this cannot exist, but
# the render-time guard is a backstop and states its own bound anyway.
MAX_STORED_NAME_CHARS = 200

# Everything outside the letters and digits.
_ALLOWED_PUNCTUATION = frozenset(" -'.")
# Apostrophes people and keyboards actually type (U+2019 RIGHT SINGLE
# QUOTATION MARK, U+02BC MODIFIER LETTER APOSTROPHE), folded to the ASCII
# one so a name typed on a phone is a valid name rather than a refused one.
# NFKC leaves both alone.
_APOSTROPHES = ("\u2019", "\u02bc")

# Why one of the persona content rules refused a name, said as a reason a
# person and a model can both act on. The codes are ``check_content``'s.
_CONTENT_REASON: dict[str, str] = {
    "override_phrasing": "that name reads as an instruction rather than a name",
    "permissions": (
        "a name cannot be a word about approvals, permissions or policy; that is "
        "what an agent may do, not what it is called"
    ),
    "url": "a name cannot be a link or a domain",
    "tool_name": "a name cannot look like a tool name",
}


def identity_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Knowing what you are called is not a privilege. Being told "your name
    is Bisby" and having no way to act on it is the failure this capability
    exists for, and an agent that cannot change its own name has to ask a
    human to run a PATCH for it. Unscoped, because the bound lives in the
    tool: the input names no target, so renaming a *colleague* is not
    expressible; the name passes the allow-list above; and every rename
    leaves a visible receipt and an audit row."""
    return ((IDENTITY_SELF_CAPABILITY, {}),)


def normalize_agent_name(raw: str) -> str:
    """The canonical form of a name: NFKC, folded apostrophes, whitespace
    collapsed. Pure text tidying — it never removes a character that would
    have been refused, so validation still sees it."""
    text = unicodedata.normalize("NFKC", raw)
    for apostrophe in _APOSTROPHES:
        text = text.replace(apostrophe, "'")
    return " ".join(text.split())


def _first_control_character(text: str) -> str:
    return next((ch for ch in text if unicodedata.category(ch).startswith("C")), "")


def _first_line_break(text: str) -> str:
    """The first whitespace character that is not a plain space.

    Read *before* whitespace is collapsed, because collapsing is what hid
    this: ``"Bisby\\nDoorstop"`` became ``"Bisby Doorstop"`` and was stored
    with a 200, so a name shaped like an injection payload was quietly
    repaired into a different name than the person typed rather than
    refused. NFKC has already folded the whitespace that *is* a space (a
    non-breaking space keeps working); what is left here is a line break, a
    tab, a vertical tab, or a Unicode line separator — none of which belong
    in a name, and all of which mean somebody typed more than one line.
    """
    return next((ch for ch in text if ch.isspace() and ch != " "), "")


def _first_disallowed_character(text: str) -> str:
    return next(
        (ch for ch in text if not (ch.isalpha() or ch.isdigit() or ch in _ALLOWED_PUNCTUATION)),
        "",
    )


def agent_name_problem(raw: str) -> str | None:
    """Why ``raw`` cannot be an agent's name, in a sentence, or ``None``.

    One function for every writer — the agent's own tool, the HTTP schema an
    admin's console drives — so a name accepted through one path is a name
    the other would accept too, and the sentence a person reads is the same
    sentence the model is handed.
    """
    name = normalize_agent_name(raw)
    if not name:
        return "a name cannot be blank"
    # Before the collapsed form is examined: everything below reads ``name``,
    # in which a line break has already become a space.
    line_break = _first_line_break(unicodedata.normalize("NFKC", raw))
    if line_break:
        return (
            "a name is a single line: it cannot contain a line break or a tab "
            f"(U+{ord(line_break):04X})"
        )
    control = _first_control_character(name)
    if control:
        return f"a name cannot contain invisible or control characters (U+{ord(control):04X})"
    disallowed = _first_disallowed_character(name)
    if disallowed:
        return (
            f"a name cannot contain {disallowed!r}: use letters, digits, spaces, "
            "hyphens, apostrophes and full stops"
        )
    if not any(ch.isalpha() for ch in name):
        return "a name needs at least one letter"
    if len(name) > MAX_AGENT_NAME_CHARS:
        return f"a name can be at most {MAX_AGENT_NAME_CHARS} characters ({len(name)} given)"
    words = name.split(" ")
    if len(words) > MAX_AGENT_NAME_WORDS:
        return (
            f"a name can be at most {MAX_AGENT_NAME_WORDS} words ({len(words)} given); "
            "a description belongs in the role title, not the name"
        )
    try:
        # The persona content rules, whole: a name is composed into the system
        # prompt above the persona block, so it clears at least the same bar.
        # It also means a future rule added there covers names for free.
        check_content(name, field="name")
    except ContentRuleError as error:
        reason = _CONTENT_REASON.get(error.code, _CONTENT_REASON["override_phrasing"])
        return f"{reason} ({error.matched!r})"
    return None


def render_safe_agent_name(name: str, *, fallback: str = "") -> str:
    """The name as it may be spliced into a system prompt.

    Deliberately the *safety* half of :func:`agent_name_problem` and not the
    whole rule (see the module docstring): a name that predates these rules
    keeps rendering as itself. ``fallback`` is the canonical name — the
    agent's slug, which cannot carry anything unsafe. Returns ``""`` when
    neither can be rendered, and the caller then drops the name clause rather
    than printing something it cannot vouch for.
    """
    for candidate in (name, fallback):
        text = normalize_agent_name(candidate)
        if text and not _first_control_character(text) and len(text) <= MAX_STORED_NAME_CHARS:
            return text
    return ""


__all__ = [
    "IDENTITY_SELF_CAPABILITY",
    "MAX_AGENT_NAME_CHARS",
    "MAX_AGENT_NAME_WORDS",
    "MAX_STORED_NAME_CHARS",
    "agent_name_problem",
    "identity_grant_specs",
    "normalize_agent_name",
    "render_safe_agent_name",
]
