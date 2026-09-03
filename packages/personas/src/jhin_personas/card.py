"""The persona card: how an agent acts and sounds, as named facets.

A persona is deliberately *not* a free-text prompt file. It is a small,
bounded card of facets — voice, stance, pace, how it handles uncertainty,
how it registers with the person it serves versus with colleagues, one
optional flourish, and a short list of things to avoid. Each facet is
capped, the card is capped as a whole, and every string passes the content
rules below before it can be stored or rendered.

The content rules exist because persona text is composed into the *system*
prompt on every run. Nothing else scrubs that layer: the gateway's payload
sanitiser and the memory screen see tool arguments and recalled facts, not
what an admin or an agent wrote into a card. So a card must never name a
tool, carry override phrasing, point at a URL, or talk about approvals and
permissions — it shapes how an agent says things, never what it may do.
"""

from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

MAX_NAME_CHARS = 64
MAX_DISPLAY_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 200
MAX_FACET_CHARS = 240
MAX_NEVER_ITEMS = 6
MAX_NEVER_ITEM_CHARS = 120
# The seven facet strings plus every ``never`` item, after whitespace
# collapse. Small enough that a card can never crowd out the role prompt.
MAX_CARD_CHARS = 1500
MAX_TAGS = 8
MAX_TAG_CHARS = 32

FUN_TAG = "fun"
PROFESSIONAL_TAG = "professional"

# built_in: shipped with Jhin, read-only. custom: written by a person.
# agent: an agent wrote it for itself and a person let it through.
PERSONA_SOURCES: tuple[str, ...] = ("built_in", "custom", "agent")

# Prompt order. ``with_people`` and ``with_teammates`` are rendered one at a
# time depending on who the agent is talking with this turn.
FACET_NAMES: tuple[str, ...] = (
    "voice",
    "stance",
    "pace",
    "when_unsure",
    "with_people",
    "with_teammates",
    "signature",
    "never",
)
_TEXT_FACETS: tuple[str, ...] = tuple(name for name in FACET_NAMES if name != "never")

# Same slug rule as a skill name (jhin_skills.parser._NAME_RE): the name is
# the agent-facing identifier and appears in tool arguments.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Any dotted identifier, in any case, with segments of two or more characters:
# ``skills.read``, ``organization.ask_person``. Shape-based on purpose — this
# package cannot import the tool catalog, and a card must never steer tool
# use by name. The plain word "tool" is fine; "e.g." and "etc." do not match.
_TOOL_NAME_RE = re.compile(r"(?i)\b[a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+)+\b")
_OVERRIDE_RE = re.compile(
    r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}"
    r"\b(previous|prior|above|earlier|all|these|your)\b.{0,30}"
    r"\b(instructions?|rules?|prompts?|guidelines?)\b"
)
_INJECTION_MARKER_RE = re.compile(
    r"(?i)\b(system prompt|you are now|new instructions?|developer message)\b"
)
_URL_SCHEME_RE = re.compile(r"(?i)\b(https?://|www\.)")
_BARE_DOMAIN_RE = re.compile(r"(?i)\b[a-z0-9-]+\.(com|net|org|io|ai|dev|app)\b")
_PERMISSIONS_RE = re.compile(
    r"(?i)\b(approv(?:e|al|als|ed)|permission(?:s)?|capabilit(?:y|ies)|grant(?:s|ed)?"
    r"|polic(?:y|ies)|bypass|jailbreak)\b"
)


class ContentRuleError(ValueError):
    """A string broke one of the content rules a persona must respect.

    ``code`` is stable and machine-readable: ``tool_name``,
    ``override_phrasing``, ``url``, or ``permissions``.
    """

    def __init__(self, *, code: str, field: str, matched: str, reason: str) -> None:
        self.code = code
        self.field = field
        self.matched = matched
        super().__init__(f"{field} {reason} ({matched!r})")


def _collapse(value: str) -> str:
    return " ".join(value.split())


def check_content(text: str, *, field: str) -> None:
    """Raise :class:`ContentRuleError` when ``text`` breaks a content rule.

    Applied to the display name, the description, every facet, and every
    ``never`` item. Returns nothing on success.
    """
    # Links first: a bare domain is also a dotted identifier, and "this is a
    # link" is the more useful thing to tell whoever wrote it.
    url = _URL_SCHEME_RE.search(text) or _BARE_DOMAIN_RE.search(text)
    if url is not None:
        raise ContentRuleError(
            code="url",
            field=field,
            matched=url.group(0),
            reason="must not contain a link or a domain",
        )
    tool = _TOOL_NAME_RE.search(text)
    if tool is not None:
        raise ContentRuleError(
            code="tool_name",
            field=field,
            matched=tool.group(0),
            reason="must not name a tool; a persona shapes how an agent sounds, not what it calls",
        )
    override = _OVERRIDE_RE.search(text) or _INJECTION_MARKER_RE.search(text)
    if override is not None:
        raise ContentRuleError(
            code="override_phrasing",
            field=field,
            matched=override.group(0),
            reason="must not try to override other instructions",
        )
    permissions = _PERMISSIONS_RE.search(text)
    if permissions is not None:
        raise ContentRuleError(
            code="permissions",
            field=field,
            matched=permissions.group(0),
            reason="must not touch approvals, permissions, or policy; those are not its to shape",
        )


def is_valid_persona_name(name: str) -> bool:
    """Lowercase letters, digits, and inner hyphens; at most 64 chars."""
    return bool(_NAME_RE.fullmatch(name))


class PersonaFacets(BaseModel):
    """The card's facets. Every string is whitespace-collapsed, capped at
    :data:`MAX_FACET_CHARS`, and content-checked; only ``voice`` is required.

    Frozen and closed: an unknown facet is a typo that would otherwise
    silently drop, and this one object is what gets stored, frozen into a
    run's snapshot, and rendered — so it is validated once, here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # How they sound, in one or two sentences.
    voice: str = Field(min_length=1, max_length=MAX_FACET_CHARS)
    # How they take positions and handle disagreement.
    stance: str = Field(default="", max_length=MAX_FACET_CHARS)
    # Brevity versus depth, and when to go long.
    pace: str = Field(default="", max_length=MAX_FACET_CHARS)
    # State assumptions versus ask the person.
    when_unsure: str = Field(default="", max_length=MAX_FACET_CHARS)
    # The register with the person they serve.
    with_people: str = Field(default="", max_length=MAX_FACET_CHARS)
    # The register with colleagues — Jhin is a company of agents.
    with_teammates: str = Field(default="", max_length=MAX_FACET_CHARS)
    # One small recurring flourish.
    signature: str = Field(default="", max_length=MAX_FACET_CHARS)
    # What to avoid: short, distinct items.
    never: list[str] = Field(default_factory=list, max_length=MAX_NEVER_ITEMS)

    @field_validator(*_TEXT_FACETS, mode="before")
    @classmethod
    def _collapse_whitespace(cls, value: object) -> object:
        # Before the length constraint runs, so padding never counts against
        # the cap and never sneaks past it either.
        return _collapse(value) if isinstance(value, str) else value

    @field_validator(*_TEXT_FACETS)
    @classmethod
    def _check_facet_content(cls, value: str, info: ValidationInfo) -> str:
        check_content(value, field=str(info.field_name))
        return value

    @field_validator("never")
    @classmethod
    def _check_never_items(cls, items: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in items:
            item = _collapse(raw)
            if not item:
                raise ValueError("never items must not be empty")
            if len(item) > MAX_NEVER_ITEM_CHARS:
                raise ValueError(f"never items must be at most {MAX_NEVER_ITEM_CHARS} characters")
            check_content(item, field="never")
            key = item.casefold()
            if key in seen:
                raise ValueError(f"never items must be distinct ({item!r} repeats)")
            seen.add(key)
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _check_card_total(self) -> PersonaFacets:
        total = self.facet_chars()
        if total > MAX_CARD_CHARS:
            raise ValueError(
                f"the card runs to {total} characters across its facets; "
                f"the limit is {MAX_CARD_CHARS}"
            )
        return self

    def facet_chars(self) -> int:
        """The seven facet strings plus every ``never`` item, in characters."""
        return sum(len(getattr(self, name)) for name in _TEXT_FACETS) + sum(
            len(item) for item in self.never
        )


class PersonaCard(BaseModel):
    """One persona: identity, tags, and the facets. The same shape whether
    it came from a shipped TOML file, an admin's form, or an agent's tool
    call — validation lives here, once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    facets: PersonaFacets

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not is_valid_persona_name(value):
            raise ValueError(
                f"use lowercase letters, digits, and hyphens (at most {MAX_NAME_CHARS} characters)"
            )
        return value

    @field_validator("display_name", "description", mode="before")
    @classmethod
    def _collapse_whitespace(cls, value: object) -> object:
        return _collapse(value) if isinstance(value, str) else value

    @field_validator("display_name", "description")
    @classmethod
    def _check_text_content(cls, value: str, info: ValidationInfo) -> str:
        check_content(value, field=str(info.field_name))
        return value

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, tags: list[str]) -> list[str]:
        # Order is kept: the first tags are the ones a gallery shows first.
        seen: set[str] = set()
        for tag in tags:
            if not _TAG_RE.fullmatch(tag):
                raise ValueError(
                    f"tag {tag!r} is invalid: use lowercase letters, digits, and hyphens "
                    f"(at most {MAX_TAG_CHARS} characters)"
                )
            if tag in seen:
                raise ValueError(f"tag {tag!r} repeats")
            seen.add(tag)
        return tags

    @property
    def is_fun(self) -> bool:
        return FUN_TAG in self.tags
