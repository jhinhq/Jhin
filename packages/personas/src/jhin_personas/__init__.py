"""jhin-personas: how an agent acts and sounds.

A persona is a structured card of named facets — never a free-text prompt
file. This package holds the card model and its content rules, the
``persona.toml`` loader, and the curated cast Jhin ships. Rendering into
the prompt lives in ``jhin_agents``; storage and assignment live with the
API and the tool gateway.
"""

from jhin_personas.builtin import BUILTIN_PERSONA_NAMES, load_builtin_personas
from jhin_personas.card import (
    FACET_NAMES,
    FUN_TAG,
    MAX_CARD_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_FACET_CHARS,
    MAX_NAME_CHARS,
    MAX_NEVER_ITEM_CHARS,
    MAX_NEVER_ITEMS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    PERSONA_SOURCES,
    PROFESSIONAL_TAG,
    ContentRuleError,
    PersonaCard,
    PersonaFacets,
    check_content,
    is_valid_persona_name,
)
from jhin_personas.toml_loader import (
    SCHEMA_VERSION,
    BuiltinPersona,
    PersonaTomlError,
    load_persona_toml,
)

__all__ = [
    "BUILTIN_PERSONA_NAMES",
    "FACET_NAMES",
    "FUN_TAG",
    "MAX_CARD_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_FACET_CHARS",
    "MAX_NAME_CHARS",
    "MAX_NEVER_ITEMS",
    "MAX_NEVER_ITEM_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "PERSONA_SOURCES",
    "PROFESSIONAL_TAG",
    "SCHEMA_VERSION",
    "BuiltinPersona",
    "ContentRuleError",
    "PersonaCard",
    "PersonaFacets",
    "PersonaTomlError",
    "check_content",
    "is_valid_persona_name",
    "load_builtin_personas",
    "load_persona_toml",
]
