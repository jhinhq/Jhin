"""Category derivation for browse-installed skills (docs/architecture/skills.md).

Browse installs are the one creation path where a category is *inferred*
rather than typed. Deriving it purely from folder structure turned out to be
useless against the real repositories in the catalog: both
``anthropics/skills`` and ``obra/superpowers`` nest every skill under one
generic ``skills/`` wrapper, so a naive "humanize the parent folder" rule
put every single skill in one bucket named "Skills".

The rule is therefore a three-step cascade, most-authoritative first:

1. **A category the skill declares itself** — ``category`` in the SKILL.md
   frontmatter (some repositories provide one; neither catalog default does
   today, but honoring it costs nothing and is the most accurate signal).
2. **A meaningful folder segment** — the nearest ancestor directory whose
   name is not a generic container (``skills/``, ``src/``, ``.github/``,
   …). This is what gives a genuinely categorized repository — one with
   real subject subfolders like ``document-skills/pdf`` — its own
   categories.
3. **A small fixed keyword taxonomy** over the skill's name and
   description. This is what rescues the flat repositories: a name and a
   description are the only real signal they carry, and they carry it well.

Anything still unmatched falls back to :data:`DEFAULT_CATEGORY`.
"""

from __future__ import annotations

import posixpath
import re

from jhin_skills.parser import MAX_CATEGORY_CHARS

DEFAULT_CATEGORY = "General"

_SEPARATORS_RE = re.compile(r"[-_]+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

# Folder names that describe *packaging*, not subject matter. A directory
# named one of these says nothing about what a skill is about, so the
# derivation walks past it looking for a real category folder. Compared
# after normalization (lowercased, leading dots stripped).
GENERIC_FOLDER_SEGMENTS: frozenset[str] = frozenset(
    {
        "skills",
        "skill",
        "agent-skills",
        "agentskills",
        "src",
        "lib",
        "libs",
        "library",
        "libraries",
        "packages",
        "package",
        "examples",
        "example",
        "samples",
        "sample",
        "github",
        "docs",
        "doc",
        "templates",
        "template",
        "plugins",
        "plugin",
        "agents",
        "agent",
        "catalog",
        "contrib",
        "community",
        "public",
        "assets",
        "resources",
        "shared",
        "common",
        "root",
        "main",
        "repo",
        "dist",
        "build",
        "content",
        "collection",
        "collections",
    }
)

# A small, fixed taxonomy. Each entry is (category, keywords); a keyword
# matching the skill's *name* weighs more than one matching its description,
# because the name is the stronger signal. Ties break toward the earlier
# entry, so the more specific categories are listed first.
CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Documents",
        (
            "pdf",
            "docx",
            "document",
            "documentation",
            "word doc",
            "pptx",
            "powerpoint",
            "slide",
            "deck",
            "presentation",
            "memo",
            "letterhead",
            "report",
            "proposal",
            "spec",
            "adr",
        ),
    ),
    (
        "Data & analysis",
        (
            "spreadsheet",
            "xlsx",
            "csv",
            "tsv",
            "excel",
            "dataset",
            "analytics",
            "dashboard",
            "chart",
            "sql",
            "query",
            "metric",
            "tabular",
        ),
    ),
    (
        "Design & creative",
        (
            "design",
            "brand",
            "typography",
            "color",
            "colour",
            "visual",
            "art",
            "poster",
            "theme",
            "aesthetic",
            "styling",
            "layout",
            "illustration",
            "gif",
            "canvas",
            "gallery",
        ),
    ),
    (
        "Testing & QA",
        (
            "test",
            "testing",
            "tdd",
            "qa",
            "playwright",
            "verification",
            "verify",
            "regression",
            "coverage",
            "lint",
            "assertion",
        ),
    ),
    (
        "Security",
        (
            "security",
            "secret",
            "credential",
            "vulnerability",
            "hardening",
            "auth",
            "exploit",
            "threat",
            "leak",
        ),
    ),
    (
        "Operations",
        (
            "incident",
            "monitor",
            "observability",
            "kubernetes",
            "terraform",
            "infrastructure",
            "sre",
            "oncall",
            "outage",
            "deployment",
            "deploy",
            "release",
            "rollout",
            "ci/cd",
            "pipeline",
            "cost",
        ),
    ),
    (
        "AI & agents",
        (
            "agent",
            "subagent",
            "llm",
            "prompt",
            "claude",
            "model context protocol",
            "skill",
            "context window",
            "superpowers",
        ),
    ),
    (
        "Engineering",
        (
            "code",
            "coding",
            "debug",
            "debugging",
            "bug",
            "git",
            "worktree",
            "branch",
            "refactor",
            "api",
            "sdk",
            "server",
            "mcp",
            "frontend",
            "backend",
            "react",
            "typescript",
            "python",
            "database",
            "migration",
            "engineering",
            "implementation",
            "programming",
            "webapp",
            "html",
        ),
    ),
    (
        "Communication",
        (
            "comms",
            "communication",
            "email",
            "slack",
            "message",
            "newsletter",
            "announcement",
            "update",
            "status",
            "meeting",
            "notes",
            "stakeholder",
            "writing",
            "onboarding",
        ),
    ),
    (
        "Product & planning",
        (
            "plan",
            "planning",
            "brainstorm",
            "roadmap",
            "requirement",
            "prioritize",
            "prioritization",
            "priority",
            "backlog",
            "idea",
            "product",
            "scoping",
            "breakdown",
            "estimate",
        ),
    ),
    (
        "Learning & enablement",
        (
            "academy",
            "course",
            "tutorial",
            "teach",
            "training",
            "learning",
            "learn to",
            "curriculum",
            "lesson",
            "discernment",
        ),
    ),
)


def humanize_folder_name(name: str) -> str:
    """``"document-skills"`` -> ``"Document skills"``.

    Hyphens and underscores become spaces; only the first character is
    capitalized (the rest is left as-is, so an already-lowercase word stays
    lowercase rather than becoming Title Case).
    """
    words = _SEPARATORS_RE.sub(" ", name.strip().lstrip(".")).strip()
    if not words:
        return ""
    return (words[0].upper() + words[1:])[:MAX_CATEGORY_CHARS]


def _is_generic_segment(segment: str) -> bool:
    normalized = segment.strip().lstrip(".").lower()
    return not normalized or normalized in GENERIC_FOLDER_SEGMENTS


def category_from_path(folder: str) -> str:
    """The nearest ancestor folder that names a subject, humanized.

    Walks up from the skill's own folder, skipping generic container
    directories, and returns ``""`` when no meaningful ancestor exists (the
    skill sits at the repository root, or every ancestor is a generic
    wrapper like ``skills/``).
    """
    cleaned = (folder or "").strip("/")
    parent = posixpath.dirname(cleaned)
    while parent:
        segment = posixpath.basename(parent)
        if not _is_generic_segment(segment):
            return humanize_folder_name(segment)
        parent = posixpath.dirname(parent)
    return ""


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """A whole-word matcher for one keyword, tolerating common inflections.

    Word boundaries matter: plain substring matching put "art" inside
    "artifacts" and "test" inside "latest". The optional suffix group keeps
    the keyword lists short — "test" still matches "tests"/"testing".
    """
    normalized = _NON_WORD_RE.sub(" ", keyword.lower()).strip()
    return re.compile(rf"\b{re.escape(normalized)}(?:s|es|ing|ed)?\b")


_COMPILED_KEYWORDS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (category, tuple(_keyword_pattern(keyword) for keyword in keywords))
    for category, keywords in CATEGORY_KEYWORDS
)


def category_from_text(name: str, description: str) -> str:
    """Best-matching taxonomy category for a skill's name + description.

    Scores every taxonomy entry (a name hit counts triple, since the name is
    the stronger signal) and returns the highest scorer, ties going to the
    earlier — more specific — entry. Returns ``""`` when nothing matches.
    """
    name_text = _NON_WORD_RE.sub(" ", name.lower()).strip()
    description_text = _NON_WORD_RE.sub(" ", description.lower()).strip()

    best_category = ""
    best_score = 0
    for category, patterns in _COMPILED_KEYWORDS:
        score = 0
        for pattern in patterns:
            if pattern.search(name_text):
                score += 3
            if pattern.search(description_text):
                score += 1
        if score > best_score:
            best_category = category
            best_score = score
    return best_category


def derive_category(
    folder: str,
    *,
    name: str = "",
    description: str = "",
    declared: str = "",
) -> str:
    """The display category for a skill found at ``folder``.

    Cascades: a category the SKILL.md declares itself, then the nearest
    meaningful (non-generic) ancestor folder, then a keyword taxonomy over
    the name and description, then :data:`DEFAULT_CATEGORY`.
    """
    if declared.strip():
        return humanize_folder_name(declared) or DEFAULT_CATEGORY
    from_path = category_from_path(folder)
    if from_path:
        return from_path
    return category_from_text(name, description) or DEFAULT_CATEGORY
