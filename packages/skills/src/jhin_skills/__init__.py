"""jhin-skills: the open Agent Skills format (SKILL.md) for Jhin.

Parsing, validation, bundle loading (directory / zip / GitHub archive), and
the built-in starter library. See docs/architecture/skills.md.
"""

from jhin_skills.builtin import load_builtin_skills
from jhin_skills.bundle import (
    MAX_SKILLS_PER_BUNDLE,
    MAX_ZIP_BYTES,
    BundleError,
    BundleResult,
    LoadedSkill,
    SkillFile,
    load_directory,
    load_zip,
)
from jhin_skills.github import (
    SkillImportError,
    fetch_github_repo_zip,
    parse_github_ref,
    source_url_for,
)
from jhin_skills.parser import (
    MAX_CONTENT_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NAME_CHARS,
    MAX_TOTAL_BYTES,
    ParsedSkill,
    SkillParseError,
    find_secret,
    is_valid_skill_name,
    parse_skill_md,
    validate_file_path,
)

__all__ = [
    "MAX_CONTENT_BYTES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_NAME_CHARS",
    "MAX_SKILLS_PER_BUNDLE",
    "MAX_TOTAL_BYTES",
    "MAX_ZIP_BYTES",
    "BundleError",
    "BundleResult",
    "LoadedSkill",
    "ParsedSkill",
    "SkillFile",
    "SkillImportError",
    "SkillParseError",
    "fetch_github_repo_zip",
    "find_secret",
    "is_valid_skill_name",
    "load_builtin_skills",
    "load_directory",
    "load_zip",
    "parse_github_ref",
    "parse_skill_md",
    "source_url_for",
    "validate_file_path",
]
