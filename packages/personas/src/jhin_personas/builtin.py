"""The curated cast of personas shipped with Jhin.

Each lives as one folder under ``jhin_personas/builtins`` holding a
``persona.toml``, so the same loader that will read a person's file reads
the shipped ones — the data files are the source of truth, not code. Six
professional cards and six fun ones; the fun ones are still competent
colleagues, with the humour in how they sound and never in how they work.
"""

from __future__ import annotations

from pathlib import Path

from jhin_personas.toml_loader import BuiltinPersona, PersonaTomlError, load_persona_toml

_BUILTINS_DIR = Path(__file__).resolve().parent / "builtins"

# The roster, professional then fun. The loader refuses to start when the
# folders on disk and this tuple disagree: a card added without a roster
# entry (or the reverse) is a packaging mistake, not a quiet default.
BUILTIN_PERSONA_NAMES: tuple[str, ...] = (
    "the-straight-shooter",
    "the-patient-explainer",
    "the-skeptic",
    "the-host",
    "the-editor",
    "the-coach",
    "mission-control",
    "field-naturalist",
    "game-show-host",
    "cozy-innkeeper",
    "sports-commentator",
    "victorian-explorer",
)


def load_builtin_personas() -> tuple[BuiltinPersona, ...]:
    """Every shipped persona, sorted by name. Never empty.

    Raises ``RuntimeError`` when any shipped file is invalid, the same
    posture ``jhin_skills.load_builtin_skills`` takes: a broken card must
    fail the process that ships it, not the workspace that installs it.
    """
    loaded: list[BuiltinPersona] = []
    folders = sorted(
        path
        for path in _BUILTINS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    )
    for folder in folders:
        source = folder / "persona.toml"
        if not source.is_file():
            raise RuntimeError(f"built-in persona folder {folder.name!r} has no persona.toml")
        try:
            built = load_persona_toml(source.read_text(encoding="utf-8"), default_name=folder.name)
        except PersonaTomlError as exc:
            raise RuntimeError(f"built-in persona {folder.name!r} is invalid: {exc}") from exc
        if built.card.name != folder.name:
            raise RuntimeError(
                f"built-in persona folder {folder.name!r} declares name {built.card.name!r}"
            )
        loaded.append(built)

    names = sorted(built.card.name for built in loaded)
    if names != sorted(BUILTIN_PERSONA_NAMES):
        raise RuntimeError(
            f"built-in persona folders {names} do not match BUILTIN_PERSONA_NAMES "
            f"{sorted(BUILTIN_PERSONA_NAMES)}"
        )
    return tuple(sorted(loaded, key=lambda built: built.card.name))
