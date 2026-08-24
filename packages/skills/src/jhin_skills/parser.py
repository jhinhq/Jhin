"""Parsing and validation for the open Agent Skills format.

A skill is a folder holding a ``SKILL.md`` file — YAML frontmatter with
``name``, ``description``, and optionally ``license`` and ``allowed-tools``,
followed by markdown instructions — plus optional extra reference files
(the format Anthropic ships at github.com/anthropics/skills).

The frontmatter parser here is deliberately minimal and bounded: it accepts
the flat ``key: value`` mappings the format uses (including flow lists like
``[a, b]`` and block lists), never executes tags/anchors, and refuses
frontmatter larger than 8 KB. No YAML library is needed or used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 500
MAX_CONTENT_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_FILES = 20
MAX_FRONTMATTER_BYTES = 8 * 1024
MAX_FILE_PATH_CHARS = 255

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n(.*))?\Z", re.DOTALL)
_KEY_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s?(.*)$")
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

# Obvious credential material may never enter the skills library. The check
# is intentionally cheap and conservative (same posture as jhin_memory's
# screening): reject loudly instead of storing then redacting.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("provider_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer_header", re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)


class SkillParseError(ValueError):
    """The text is not a valid skill (bad frontmatter, name, or size)."""


@dataclass(frozen=True)
class ParsedSkill:
    """One parsed SKILL.md: identity plus the markdown instruction body."""

    name: str
    description: str
    content: str
    license: str = ""
    # Advisory in Jhin: the tool gateway (grants + policy), not this list,
    # decides what an agent may call. Parsed so imports don't lose it.
    allowed_tools: tuple[str, ...] = ()


def is_valid_skill_name(name: str) -> bool:
    """Lowercase letters, digits, and inner hyphens; at most 64 chars."""
    return bool(_NAME_RE.fullmatch(name))


def find_secret(text: str) -> str | None:
    """The label of the first obvious credential pattern found, if any."""
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_flow_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    return [_unquote(item) for item in inner.split(",") if item.strip()]


def parse_frontmatter(block: str) -> dict[str, str | list[str]]:
    """Flat ``key: value`` mapping with flow (``[a, b]``) and block lists."""
    if len(block.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise SkillParseError("frontmatter is larger than 8 KB")
    result: dict[str, str | list[str]] = {}
    pending_list_key: str | None = None
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        if stripped.startswith("- ") and pending_list_key is not None:
            existing = result.get(pending_list_key)
            if not isinstance(existing, list):
                existing = []
            existing.append(_unquote(stripped[2:]))
            result[pending_list_key] = existing
            continue
        match = _KEY_LINE_RE.match(line)
        if match is None:
            # Nested mappings (e.g. a metadata block) are skipped, not fatal:
            # the open format allows extra keys this parser does not model.
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if value == "":
            pending_list_key = key
            result.setdefault(key, "")
            continue
        pending_list_key = None
        if value.startswith("[") and value.endswith("]"):
            result[key] = _parse_flow_list(value)
        else:
            result[key] = _unquote(value)
    return result


def _string_field(fields: dict[str, str | list[str]], key: str) -> str:
    value = fields.get(key, "")
    if isinstance(value, list):
        return ", ".join(value)
    return value


def parse_skill_md(text: str, *, default_name: str = "") -> ParsedSkill:
    """Parse one SKILL.md document into a validated :class:`ParsedSkill`.

    ``default_name`` (usually the folder name) is used when the frontmatter
    has no ``name`` key, matching the format's folder-name convention.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillParseError(
            "SKILL.md must start with a '---' YAML frontmatter block "
            "containing at least 'name' and 'description'"
        )
    fields = parse_frontmatter(match.group(1))
    body = (match.group(2) or "").strip()

    name = _string_field(fields, "name").strip() or default_name.strip()
    if not is_valid_skill_name(name):
        raise SkillParseError(
            f"skill name {name!r} is invalid: use lowercase letters, digits, "
            f"and hyphens (at most {MAX_NAME_CHARS} characters)"
        )
    description = _string_field(fields, "description").strip()
    if not description:
        raise SkillParseError(f"skill {name!r} has no description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillParseError(
            f"skill {name!r} description is longer than {MAX_DESCRIPTION_CHARS} characters"
        )
    if len(body.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise SkillParseError(f"skill {name!r} instructions are larger than 64 KB")

    allowed_raw = fields.get("allowed-tools", [])
    if isinstance(allowed_raw, str):
        allowed = tuple(item.strip() for item in allowed_raw.split(",") if item.strip())
    else:
        allowed = tuple(item.strip() for item in allowed_raw if item.strip())

    secret = find_secret(text)
    if secret is not None:
        raise SkillParseError(f"skill {name!r} contains credential-like content ({secret})")

    return ParsedSkill(
        name=name,
        description=description,
        content=body,
        license=_string_field(fields, "license").strip(),
        allowed_tools=allowed,
    )


def validate_file_path(path: str) -> str:
    """A safe, relative reference-file path (no traversal, no absolutes)."""
    if len(path) > MAX_FILE_PATH_CHARS or not _FILE_PATH_RE.fullmatch(path):
        raise SkillParseError(f"skill file path {path!r} is invalid")
    if any(part == ".." for part in path.split("/")):
        raise SkillParseError(f"skill file path {path!r} is invalid")
    return path
