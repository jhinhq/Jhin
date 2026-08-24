"""Load a *bundle* of skill folders from a directory or a zip archive.

A bundle is any tree containing one or more skill folders (a folder with a
``SKILL.md``), e.g. the layout of github.com/anthropics/skills where skills
sit in top-level or nested category folders. Loading is tolerant: a broken
or oversized skill is skipped with a warning instead of failing the whole
bundle, so an import surfaces exactly what was accepted and why the rest
was not.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path

from jhin_skills.parser import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    ParsedSkill,
    SkillParseError,
    parse_skill_md,
    validate_file_path,
)

MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_SKILLS_PER_BUNDLE = 50

SKILL_FILENAME = "SKILL.md"


class BundleError(ValueError):
    """The bundle as a whole is unusable (bad zip, too large, empty)."""


@dataclass(frozen=True)
class SkillFile:
    path: str
    content: str


@dataclass(frozen=True)
class LoadedSkill:
    skill: ParsedSkill
    files: tuple[SkillFile, ...]
    folder: str


@dataclass(frozen=True)
class BundleResult:
    skills: tuple[LoadedSkill, ...]
    warnings: tuple[str, ...]


def _assemble(
    folder: str,
    documents: dict[str, str],
    warnings: list[str],
) -> LoadedSkill | None:
    """One skill folder → LoadedSkill, or None (with a warning) if invalid."""
    skill_md = documents.pop(SKILL_FILENAME, None)
    if skill_md is None:
        return None
    default_name = posixpath.basename(folder) if folder else ""
    try:
        parsed = parse_skill_md(skill_md, default_name=default_name)
    except SkillParseError as error:
        warnings.append(f"{folder or '.'}: {error}")
        return None

    files: list[SkillFile] = []
    total = len(parsed.content.encode("utf-8"))
    for path in sorted(documents):
        content = documents[path]
        try:
            validate_file_path(path)
        except SkillParseError as error:
            warnings.append(f"{parsed.name}: {error}")
            continue
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            warnings.append(f"{parsed.name}: file {path!r} is larger than 64 KB; skipped")
            continue
        if len(files) >= MAX_FILES:
            warnings.append(f"{parsed.name}: more than {MAX_FILES} reference files; rest skipped")
            break
        if total + size > MAX_TOTAL_BYTES:
            warnings.append(f"{parsed.name}: total size exceeds 256 KB; file {path!r} skipped")
            continue
        total += size
        files.append(SkillFile(path=path, content=content))
    return LoadedSkill(skill=parsed, files=tuple(files), folder=folder)


def _collect(
    entries: dict[str, bytes],
) -> BundleResult:
    """Group flat ``path -> bytes`` entries into skill folders and load them."""
    warnings: list[str] = []
    skill_dirs = sorted(
        {posixpath.dirname(path) for path in entries if posixpath.basename(path) == SKILL_FILENAME}
    )
    if not skill_dirs:
        raise BundleError("no SKILL.md found in the bundle")
    if len(skill_dirs) > MAX_SKILLS_PER_BUNDLE:
        raise BundleError(f"bundle holds more than {MAX_SKILLS_PER_BUNDLE} skills")

    loaded: list[LoadedSkill] = []
    seen_names: set[str] = set()
    for folder in skill_dirs:
        prefix = f"{folder}/" if folder else ""
        documents: dict[str, str] = {}
        for path, data in entries.items():
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix) :]
            # A file belongs to the *nearest* skill folder: anything inside a
            # nested skill folder is that skill's file, not this one's.
            owner = posixpath.dirname(path)
            while owner and owner != folder and f"{owner}/{SKILL_FILENAME}" not in entries:
                owner = posixpath.dirname(owner)
            if owner != folder:
                continue
            try:
                documents[relative] = data.decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(f"{folder or '.'}: file {relative!r} is not UTF-8 text; skipped")
        result = _assemble(folder, documents, warnings)
        if result is None:
            continue
        if result.skill.name in seen_names:
            warnings.append(f"duplicate skill name {result.skill.name!r}; keeping the first")
            continue
        seen_names.add(result.skill.name)
        loaded.append(result)
    return BundleResult(skills=tuple(loaded), warnings=tuple(warnings))


def load_zip(data: bytes, *, path_prefix: str = "") -> BundleResult:
    """Load skill folders from zip bytes (an upload or a GitHub repo zip).

    GitHub archive zips wrap everything in one ``{repo}-{ref}/`` directory;
    when every entry shares a single top-level folder it is stripped so
    ``path_prefix`` (a repo-relative folder like ``skills``) applies cleanly.
    """
    if len(data) > MAX_ZIP_BYTES:
        raise BundleError("zip archive is larger than 5 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise BundleError("not a valid zip archive") from error

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        tops = {name.split("/", 1)[0] for name in names}
        wrapped = len(tops) == 1 and all("/" in name for name in names)
        prefix = f"{next(iter(tops))}/" if wrapped else ""
        wanted = path_prefix.strip("/")

        entries: dict[str, bytes] = {}
        for info in infos:
            name = info.filename[len(prefix) :] if wrapped else info.filename
            if name.startswith("/") or any(part == ".." for part in name.split("/")):
                continue
            if wanted and not (name == wanted or name.startswith(f"{wanted}/")):
                continue
            if info.file_size > MAX_FILE_BYTES:
                # Oversized members are dropped here; text members within the
                # limit are size-checked again after decoding.
                continue
            entries[name] = archive.read(info.filename)
    if not entries:
        raise BundleError(
            f"nothing found under {path_prefix!r} in the archive"
            if path_prefix
            else "the archive is empty"
        )
    return _collect(entries)


def load_directory(root: Path) -> BundleResult:
    """Load skill folders from a directory tree on disk."""
    entries: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        entries[relative] = path.read_bytes()
    if not entries:
        raise BundleError(f"no files under {root}")
    return _collect(entries)
