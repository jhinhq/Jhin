"""persona.toml parsing: the schema key, closed key sets, folder-name default,
versions, and how card validation failures surface."""

from __future__ import annotations

import pytest

from jhin_personas import BuiltinPersona, PersonaTomlError, load_persona_toml

# The complete card the format documentation shows, wrapped with TOML's
# line-ending backslash (which trims the newline and the indent after it).
DOCUMENTED_CARD = """\
schema = 1
name = "the-skeptic"
display_name = "The Skeptic"
description = "Checks the claim before it becomes the plan."
tags = ["professional", "review", "risk"]
version = 1

[facets]
voice = "Dry, precise, quietly friendly. Sounds like the colleague who read the footnotes."
stance = \"""\\
    Separates what is known from what is assumed and says which is which. \\
    Disagrees early and in one sentence, then helps make the idea work.\"""
pace = \"""\\
    Short by default. Goes long only when a decision hinges on a detail, \\
    and then shows the detail rather than the adjective.\"""
when_unsure = \"""\\
    Names the assumption it would have to make, then asks the person one \\
    bounded question instead of guessing. If nobody answers, states the \\
    assumption and proceeds.\"""
with_people = \"""\\
    Warm and plain. Leads with the answer, follows with the caveat that \\
    matters, never the whole list.\"""
with_teammates = \"""\\
    Terse and structured: claim, evidence, gap. Asks colleagues for the \\
    source, not the summary.\"""
signature = "Closes with one line starting 'Assumes:' when an answer rests on something unverified."
never = [
  "Hedge every sentence",
  "Call something verified that was only read",
  "Bury the risk under the good news",
]
"""

MINIMAL = """\
schema = 1
display_name = "Minimal"
description = "The smallest card that loads."
version = 1

[facets]
voice = "Plain."
"""


def test_the_documented_card_loads() -> None:
    built = load_persona_toml(DOCUMENTED_CARD, default_name="the-skeptic")
    assert isinstance(built, BuiltinPersona)
    assert built.card.name == "the-skeptic"
    assert built.card.display_name == "The Skeptic"
    assert built.card.tags == ["professional", "review", "risk"]
    # The wrapped multi-line string arrives as one line of prose.
    assert built.card.facets.stance == (
        "Separates what is known from what is assumed and says which is which. "
        "Disagrees early and in one sentence, then helps make the idea work."
    )
    assert built.card.facets.signature.startswith("Closes with one line starting 'Assumes:'")
    assert len(built.card.facets.never) == 3
    assert built.version == 1
    assert built.folder == "the-skeptic"
    assert built.card.is_fun is False


def test_name_defaults_to_the_folder_name() -> None:
    built = load_persona_toml(MINIMAL, default_name="from-folder")
    assert built.card.name == "from-folder"
    assert built.folder == "from-folder"
    # An explicit name wins over the folder.
    named = load_persona_toml('name = "explicit"\n' + MINIMAL, default_name="from-folder")
    assert named.card.name == "explicit"


def test_a_card_needs_a_name_from_somewhere() -> None:
    with pytest.raises(PersonaTomlError, match="name"):
        load_persona_toml(MINIMAL)


@pytest.mark.parametrize("schema_line", ["", "schema = 2", 'schema = "1"'])
def test_schema_must_be_one(schema_line: str) -> None:
    text = MINIMAL.replace("schema = 1", schema_line)
    with pytest.raises(PersonaTomlError, match="schema = 1"):
        load_persona_toml(text, default_name="x")


def test_an_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(PersonaTomlError, match="unknown keys: colour"):
        load_persona_toml('colour = "blue"\n' + MINIMAL, default_name="x")


def test_an_unknown_facet_key_is_rejected_and_the_real_ones_are_listed() -> None:
    """A misspelt facet must not silently ship a card with that facet blank."""
    text = MINIMAL + 'tone = "cheerful"\n'
    with pytest.raises(PersonaTomlError, match="unknown keys: tone") as caught:
        load_persona_toml(text, default_name="x")
    assert "when_unsure" in str(caught.value)


def test_the_facets_table_is_required() -> None:
    text = MINIMAL.split("[facets]")[0]
    with pytest.raises(PersonaTomlError, match=r"\[facets\]"):
        load_persona_toml(text, default_name="x")


@pytest.mark.parametrize(
    "version_line", ["", "version = 0", "version = -1", "version = true", 'version = "1"']
)
def test_version_must_be_a_positive_integer(version_line: str) -> None:
    text = MINIMAL.replace("version = 1", version_line)
    with pytest.raises(PersonaTomlError, match="version"):
        load_persona_toml(text, default_name="x")


def test_version_is_carried_through() -> None:
    assert (
        load_persona_toml(MINIMAL.replace("version = 1", "version = 7"), default_name="x").version
        == 7
    )


def test_invalid_toml_is_reported_as_a_persona_error() -> None:
    with pytest.raises(PersonaTomlError, match="not valid TOML"):
        load_persona_toml("schema = 1\nthis is not toml", default_name="x")


def test_a_content_rule_failure_names_the_facet() -> None:
    text = MINIMAL.replace('voice = "Plain."', 'voice = "Reads with skills.read first."')
    with pytest.raises(PersonaTomlError, match=r"facets\.voice") as caught:
        load_persona_toml(text, default_name="x")
    assert "skills.read" in str(caught.value)


def test_a_cap_failure_names_the_field() -> None:
    text = MINIMAL.replace('display_name = "Minimal"', f'display_name = "{"M" * 81}"')
    with pytest.raises(PersonaTomlError, match="display_name"):
        load_persona_toml(text, default_name="x")


def test_as_pack_entry_is_plain_data() -> None:
    entry = load_persona_toml(DOCUMENTED_CARD).as_pack_entry()
    assert set(entry) == {"name", "display_name", "description", "tags", "facets", "version"}
    assert entry["name"] == "the-skeptic"
    assert entry["tags"] == ["professional", "review", "risk"]
    assert entry["version"] == 1
    assert set(entry["facets"]) == {
        "voice",
        "stance",
        "pace",
        "when_unsure",
        "with_people",
        "with_teammates",
        "signature",
        "never",
    }
    assert isinstance(entry["facets"]["never"], list)
