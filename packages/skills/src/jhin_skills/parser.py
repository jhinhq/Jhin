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
# Real-world skills exceed 500 easily: anthropics/skills' own docx (837),
# pptx (740), and xlsx (952) descriptions all did, and were being dropped
# outright. The agent prompt truncates descriptions to 300 chars of its
# own accord (jhin_agents.context), so a larger stored cap costs nothing
# there.
MAX_DESCRIPTION_CHARS = 2000
MAX_CONTENT_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_FILES = 20
MAX_FRONTMATTER_BYTES = 8 * 1024
MAX_FILE_PATH_CHARS = 255
MAX_CATEGORY_CHARS = 64

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n(.*))?\Z", re.DOTALL)
_KEY_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s?(.*)$")
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
# A block-scalar header: folded (>) or literal (|), an optional explicit
# indent indicator, and an optional chomping indicator (- strip, + keep).
_BLOCK_SCALAR_RE = re.compile(r"^([|>])([1-9]?)([+-]?)$")

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
    # A category the repository declares for itself. Empty for most skills;
    # when present it is the most authoritative categorization signal
    # (jhin_skills.category.derive_category).
    category: str = ""
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


_DOUBLE_QUOTE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
}


def _unescape_double_quoted(inner: str) -> str:
    """The handful of backslash escapes a double-quoted YAML scalar uses.

    Real skills in the wild embed quotes in their descriptions (e.g.
    ``anthropics/skills``' pptx skill quotes "deck," and "slides,"), which
    arrive as ``\\"``. Anything this does not model is passed through
    verbatim rather than dropped.
    """
    if "\\" not in inner:
        return inner
    out: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner):
            replacement = _DOUBLE_QUOTE_ESCAPES.get(inner[index + 1])
            if replacement is not None:
                out.append(replacement)
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return _unescape_double_quoted(inner)
        # Single-quoted YAML escapes one quote by doubling it.
        return inner.replace("''", "'")
    return value


def _parse_flow_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    return [_unquote(item) for item in inner.split(",") if item.strip()]


def _render_block_scalar(lines: list[str], *, style: str, indent_hint: str, chomp: str) -> str:
    """Render a collected block-scalar body (``>`` folded / ``|`` literal)."""
    if indent_hint:
        indent = int(indent_hint)
    else:
        indent = next(
            (len(line) - len(line.lstrip()) for line in lines if line.strip()),
            0,
        )
    cleaned = [line[indent:] if len(line) >= indent else line.lstrip() for line in lines]

    if style == "|":
        text = "\n".join(cleaned)
    else:
        # Folded: consecutive plain lines join with spaces; a blank line is a
        # paragraph break; a *more-indented* line keeps its own line, per the
        # format's folding rules.
        parts: list[str] = []
        buffer: list[str] = []
        for entry in cleaned:
            if not entry.strip():
                if buffer:
                    parts.append(" ".join(buffer))
                    buffer = []
                parts.append("")
            elif entry[:1].isspace():
                if buffer:
                    parts.append(" ".join(buffer))
                    buffer = []
                parts.append(entry)
            else:
                buffer.append(entry.strip())
        if buffer:
            parts.append(" ".join(buffer))
        text = "\n".join(parts)

    if chomp == "+":  # keep every trailing newline
        return text
    text = text.rstrip("\n")
    if chomp == "-":  # strip them all
        return text
    return f"{text}\n" if text else text  # clip: exactly one


def parse_frontmatter(block: str) -> dict[str, str | list[str]]:
    """Flat ``key: value`` mapping with flow (``[a, b]``) lists, block lists,
    and block scalars (``key: >`` / ``key: |``, with optional chomping and
    explicit-indent indicators)."""
    if len(block.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise SkillParseError("frontmatter is larger than 8 KB")
    result: dict[str, str | list[str]] = {}
    pending_list_key: str | None = None
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
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

        scalar = _BLOCK_SCALAR_RE.match(value)
        if scalar is not None:
            # Collect the indented body: every following line that is blank
            # or indented past the key (which _KEY_LINE_RE anchors at
            # column 0).
            body: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip():
                    body.append("")
                    index += 1
                    continue
                if len(candidate) - len(candidate.lstrip()) == 0:
                    break
                body.append(candidate)
                index += 1
            pending_list_key = None
            result[key] = _render_block_scalar(
                body,
                style=scalar.group(1),
                indent_hint=scalar.group(2),
                chomp=scalar.group(3),
            )
            continue

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
        category=_string_field(fields, "category").strip()[:MAX_CATEGORY_CHARS],
        allowed_tools=allowed,
    )


def validate_file_path(path: str) -> str:
    """A safe, relative reference-file path (no traversal, no absolutes)."""
    if len(path) > MAX_FILE_PATH_CHARS or not _FILE_PATH_RE.fullmatch(path):
        raise SkillParseError(f"skill file path {path!r} is invalid")
    if any(part == ".." for part in path.split("/")):
        raise SkillParseError(f"skill file path {path!r} is invalid")
    return path
