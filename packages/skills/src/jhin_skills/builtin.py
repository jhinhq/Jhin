"""The built-in starter skills shipped with Jhin.

Each lives as a real skill folder under ``jhin_skills/builtins`` in the
open SKILL.md format, so the same parser and loader that handle imports
also load the starters — the data files are the source of truth, not code.
"""

from __future__ import annotations

from pathlib import Path

from jhin_skills.bundle import BundleResult, LoadedSkill, load_directory

_BUILTINS_DIR = Path(__file__).resolve().parent / "builtins"


def load_builtin_skills() -> tuple[LoadedSkill, ...]:
    """Every shipped starter skill, sorted by name. Never empty."""
    result: BundleResult = load_directory(_BUILTINS_DIR)
    if result.warnings:
        raise RuntimeError(f"built-in skills are invalid: {result.warnings}")
    return tuple(sorted(result.skills, key=lambda loaded: loaded.skill.name))
