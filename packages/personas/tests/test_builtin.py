"""The shipped cast: twelve cards, six professional and six fun, every one of
them a complete, rule-abiding card."""

from __future__ import annotations

import pytest

from jhin_personas import (
    BUILTIN_PERSONA_NAMES,
    FACET_NAMES,
    FUN_TAG,
    PROFESSIONAL_TAG,
    BuiltinPersona,
    PersonaCard,
    check_content,
    load_builtin_personas,
)

# The curated cast, as designed: display name and tags per card.
CAST: dict[str, tuple[str, list[str]]] = {
    "the-straight-shooter": ("The Straight Shooter", ["professional", "direct", "brief"]),
    "the-patient-explainer": ("The Patient Explainer", ["professional", "teaching", "thorough"]),
    "the-skeptic": ("The Skeptic", ["professional", "review", "risk"]),
    "the-host": ("The Host", ["professional", "facilitation", "warm"]),
    "the-editor": ("The Editor", ["professional", "writing", "precise"]),
    "the-coach": ("The Coach", ["professional", "coaching", "encouraging"]),
    "mission-control": ("Mission Control", ["fun", "calm", "operations"]),
    "field-naturalist": ("Field Naturalist", ["fun", "narrative", "observant"]),
    "game-show-host": ("Game Show Host", ["fun", "energetic", "playful"]),
    "cozy-innkeeper": ("Cozy Innkeeper", ["fun", "warm", "hospitality"]),
    "sports-commentator": ("Sports Commentator", ["fun", "energetic", "progress"]),
    "victorian-explorer": ("Victorian Explorer", ["fun", "narrative", "journal"]),
}


@pytest.fixture(scope="module")
def pack() -> tuple[BuiltinPersona, ...]:
    return load_builtin_personas()


def test_the_pack_loads_and_validates_every_card(pack: tuple[BuiltinPersona, ...]) -> None:
    """The whole pack, every card through the real loader and the real
    validator. Loading raises on the first invalid file, so reaching the
    assertions means all twelve passed."""
    assert len(pack) == 12
    for built in pack:
        assert isinstance(built.card, PersonaCard)
        assert built.version >= 1
        assert built.folder == built.card.name


def test_the_loaded_names_are_the_roster(pack: tuple[BuiltinPersona, ...]) -> None:
    names = [built.card.name for built in pack]
    assert sorted(names) == sorted(BUILTIN_PERSONA_NAMES)
    assert names == sorted(names), "the pack is served sorted by name"
    assert len(set(BUILTIN_PERSONA_NAMES)) == len(BUILTIN_PERSONA_NAMES)


def test_six_professional_and_six_fun(pack: tuple[BuiltinPersona, ...]) -> None:
    professional = [built for built in pack if PROFESSIONAL_TAG in built.card.tags]
    fun = [built for built in pack if built.card.is_fun]
    assert len(professional) == 6
    assert len(fun) == 6
    assert not {built.card.name for built in professional} & {built.card.name for built in fun}
    # The first tag is the one a gallery leads with.
    for built in pack:
        assert built.card.tags[0] in (PROFESSIONAL_TAG, FUN_TAG)


def test_the_cast_is_the_curated_one(pack: tuple[BuiltinPersona, ...]) -> None:
    assert {built.card.name for built in pack} == set(CAST)
    for built in pack:
        display_name, tags = CAST[built.card.name]
        assert built.card.display_name == display_name
        assert built.card.tags == tags


def test_no_two_cards_share_a_description_or_display_name(
    pack: tuple[BuiltinPersona, ...],
) -> None:
    descriptions = [built.card.description for built in pack]
    display_names = [built.card.display_name for built in pack]
    assert len(set(descriptions)) == len(descriptions)
    assert len(set(display_names)) == len(display_names)


def test_every_card_fills_every_facet(pack: tuple[BuiltinPersona, ...]) -> None:
    """Built-ins are the reference cards: a person copying one should see
    every facet worked, not a blank to fill in."""
    required = [name for name in FACET_NAMES if name not in ("signature", "never")]
    for built in pack:
        for facet in required:
            assert getattr(built.card.facets, facet), f"{built.card.name} leaves {facet} empty"


def test_every_card_has_a_signature_and_a_never_list(pack: tuple[BuiltinPersona, ...]) -> None:
    for built in pack:
        assert built.card.facets.signature, f"{built.card.name} has no signature"
        assert len(built.card.facets.never) >= 3, f"{built.card.name} has too short a never list"


def test_every_card_passes_the_content_rules(pack: tuple[BuiltinPersona, ...]) -> None:
    """Redundant with loading — the validator already ran — and kept on
    purpose: it is the one assertion that reads as a promise about what the
    shipped text never contains."""
    for built in pack:
        card = built.card
        check_content(card.display_name, field="display_name")
        check_content(card.description, field="description")
        for facet in FACET_NAMES:
            if facet == "never":
                for item in card.facets.never:
                    check_content(item, field="never")
            else:
                check_content(getattr(card.facets, facet), field=facet)


def test_when_unsure_always_turns_to_the_person(pack: tuple[BuiltinPersona, ...]) -> None:
    """The facet exists to tie a persona to asking the person it serves; a
    card that resolves uncertainty any other way has missed the point."""
    for built in pack:
        assert "the person" in built.card.facets.when_unsure, built.card.name


def test_pack_entries_round_trip_through_the_card(pack: tuple[BuiltinPersona, ...]) -> None:
    """The dict the migration inlines and the installer consumes must rebuild
    the very same card."""
    for built in pack:
        entry = built.as_pack_entry()
        rebuilt = PersonaCard.model_validate({key: entry[key] for key in entry if key != "version"})
        assert rebuilt == built.card
        assert entry["version"] == built.version
