"""The ``persona.toml`` file format.

One file per persona, parsed with the standard library's ``tomllib``:

.. code-block:: toml

    schema = 1
    name = "the-skeptic"          # optional: defaults to the folder name
    display_name = "The Skeptic"
    description = "Checks the claim before it becomes the plan."
    tags = ["professional", "review", "risk"]
    version = 1                   # bump on any wording change

    [facets]
    voice = "..."
    stance = "..."
    pace = "..."
    when_unsure = "..."
    with_people = "..."
    with_teammates = "..."
    signature = "..."
    never = ["...", "..."]

The loader is strict about keys on purpose: a misspelt facet must fail
loudly rather than silently ship a card with that facet blank.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from jhin_personas.card import FACET_NAMES, PersonaCard

SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset(
    {"schema", "name", "display_name", "description", "tags", "version", "facets"}
)
_FACET_KEYS = frozenset(FACET_NAMES)


class PersonaTomlError(ValueError):
    """The text is not a valid persona file (bad TOML, schema, keys, or card)."""


@dataclass(frozen=True)
class BuiltinPersona:
    """One persona as loaded from a ``persona.toml``: the validated card,
    the file's ``version``, and the folder it came from.

    Defined here rather than beside the shipped pack because the loader is
    what produces it; :mod:`jhin_personas.builtin` re-exports it as the
    type of every entry in the cast.
    """

    card: PersonaCard
    version: int
    folder: str

    def as_pack_entry(self) -> dict[str, Any]:
        """The plain-data shape the API installer and the migration's
        inlined pack snapshot both consume, so the two can be compared."""
        return {
            "name": self.card.name,
            "display_name": self.card.display_name,
            "description": self.card.description,
            "tags": list(self.card.tags),
            "facets": self.card.facets.model_dump(),
            "version": self.version,
        }


def _describe_validation_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors():
        location = ".".join(str(piece) for piece in item["loc"]) or "card"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def load_persona_toml(text: str, *, default_name: str = "") -> BuiltinPersona:
    """Parse one ``persona.toml`` document into a validated card.

    ``default_name`` (usually the folder name) is used when the file has no
    ``name`` key, matching the folder-name convention skills use.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PersonaTomlError(f"persona.toml is not valid TOML: {exc}") from exc

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise PersonaTomlError(f"persona.toml has unknown keys: {', '.join(unknown)}")
    if data.get("schema") != SCHEMA_VERSION:
        raise PersonaTomlError(f"persona.toml must declare schema = {SCHEMA_VERSION}")

    version = data.get("version")
    # bool is an int in Python; ``version = true`` is a mistake, not a 1.
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PersonaTomlError("persona.toml needs an integer version of 1 or more")

    facets = data.get("facets")
    if not isinstance(facets, dict):
        raise PersonaTomlError("persona.toml needs a [facets] table")
    unknown_facets = sorted(set(facets) - _FACET_KEYS)
    if unknown_facets:
        raise PersonaTomlError(
            f"persona.toml [facets] has unknown keys: {', '.join(unknown_facets)} "
            f"(the facets are {', '.join(FACET_NAMES)})"
        )

    name = data.get("name", "")
    if not isinstance(name, str):
        raise PersonaTomlError("persona.toml name must be a string")
    name = name.strip() or default_name.strip()

    try:
        card = PersonaCard.model_validate(
            {
                "name": name,
                "display_name": data.get("display_name", ""),
                "description": data.get("description", ""),
                "tags": data.get("tags", []),
                "facets": facets,
            }
        )
    except ValidationError as exc:
        raise PersonaTomlError(
            f"persona {name or '?'!r} is invalid: {_describe_validation_error(exc)}"
        ) from exc
    return BuiltinPersona(card=card, version=version, folder=default_name)
