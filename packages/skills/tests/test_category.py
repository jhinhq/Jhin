"""Category derivation (docs/architecture/skills.md).

Path shapes and skill names here come from real, live-verified repositories
in the browse catalog rather than invented examples: ``skills/<name>`` is the
layout of anthropics/skills, obra/superpowers, addyosmani/agent-skills and
avizmarlon/agent-skills; ``.github/skills/<name>`` is jamestorrevillas/
dev-skills; ``template`` is anthropics/skills' lone repo-root skill.
"""

from __future__ import annotations

import pytest

from jhin_skills.category import (
    DEFAULT_CATEGORY,
    category_from_path,
    category_from_text,
    derive_category,
    humanize_folder_name,
)


class TestHumanizeFolderName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("skills", "Skills"),
            ("document-skills", "Document skills"),
            ("dev_ops_skills", "Dev ops skills"),
            ("SKILLS", "SKILLS"),
            (".github", "Github"),
            ("", ""),
            ("---", ""),
        ],
    )
    def test_humanizes(self, raw: str, expected: str) -> None:
        assert humanize_folder_name(raw) == expected


class TestCategoryFromPath:
    @pytest.mark.parametrize(
        ("folder", "expected"),
        [
            # A real category subfolder wins — this is the shape the rule is for.
            ("document-skills/pdf", "Document skills"),
            ("writing/blog-posts/announcement", "Blog posts"),
            # Generic wrappers are skipped: these are the real layouts of
            # anthropics/skills, obra/superpowers and jamestorrevillas/dev-skills,
            # and every one of them must yield "" so the taxonomy takes over.
            ("skills/pdf", ""),
            ("skills/brainstorming", ""),
            (".github/skills/ai-agent-design", ""),
            ("src/skills/thing", ""),
            ("packages/examples/thing", ""),
            # A repo-root skill has no ancestor at all.
            ("template", ""),
            ("", ""),
            # A generic wrapper below a real category still finds the category.
            ("legal/skills/contract-review", "Legal"),
            ("/skills/pdf/", ""),
        ],
    )
    def test_finds_the_nearest_meaningful_ancestor(self, folder: str, expected: str) -> None:
        assert category_from_path(folder) == expected


class TestCategoryFromText:
    @pytest.mark.parametrize(
        ("name", "description", "expected"),
        [
            ("pdf", "Work with PDF files, extract text and tables.", "Documents"),
            ("xlsx", "Open and edit a spreadsheet, compute formulas.", "Data & analysis"),
            (
                "brand-guidelines",
                "Apply official brand colors and typography.",
                "Design & creative",
            ),
            ("test-driven-development", "Write a failing test first.", "Testing & QA"),
            ("systematic-debugging", "Use when encountering any bug.", "Engineering"),
            ("internal-comms", "Write status reports and newsletters.", "Communication"),
            ("writing-plans", "Turn a spec into an implementation plan.", "Product & planning"),
            ("academy-guide", "Recommend matching courses and tutorials.", "Learning & enablement"),
            ("subagent-driven-development", "Dispatch parallel agents.", "AI & agents"),
        ],
    )
    def test_classifies_real_skills(self, name: str, description: str, expected: str) -> None:
        assert category_from_text(name, description) == expected

    def test_returns_empty_when_nothing_matches(self) -> None:
        assert category_from_text("zzz", "qqq wwww.") == ""

    def test_matches_whole_words_only(self) -> None:
        """ "art" must not match inside "artifacts", nor "test" inside "latest".

        This is a real regression: web-artifacts-builder (a React/HTML skill)
        was landing in "Design & creative" purely because "artifacts"
        contains "art".
        """
        assert (
            category_from_text(
                "web-artifacts-builder",
                "Creating claude.ai HTML artifacts with React and Tailwind CSS.",
            )
            == "Engineering"
        )

    def test_a_name_hit_outweighs_a_description_hit(self) -> None:
        # "pdf" in the name beats a single passing mention of "design".
        assert category_from_text("pdf", "A design-conscious PDF toolkit.") == "Documents"


class TestDeriveCategory:
    def test_a_declared_frontmatter_category_wins(self) -> None:
        assert (
            derive_category("skills/pdf", name="pdf", description="PDFs.", declared="legal-ops")
            == "Legal ops"
        )

    def test_a_meaningful_folder_beats_the_taxonomy(self) -> None:
        assert derive_category("document-skills/pdf", name="pdf", description="PDFs.") == (
            "Document skills"
        )

    def test_a_generic_wrapper_falls_through_to_the_taxonomy(self) -> None:
        assert derive_category("skills/pdf", name="pdf", description="Work with PDFs.") == (
            "Documents"
        )

    def test_unclassifiable_falls_back_to_general(self) -> None:
        assert derive_category("template", name="template-zzz", description="Replace me.") == (
            DEFAULT_CATEGORY
        )
