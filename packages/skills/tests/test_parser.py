"""SKILL.md parsing: frontmatter variants, bad input, limits, secrets."""

from __future__ import annotations

import pytest

from jhin_skills import (
    MAX_CONTENT_BYTES,
    MAX_DESCRIPTION_CHARS,
    SkillParseError,
    find_secret,
    is_valid_skill_name,
    parse_skill_md,
    validate_file_path,
)
from jhin_skills.parser import parse_frontmatter


def doc(front: str, body: str = "Do the thing.") -> str:
    return f"---\n{front}\n---\n\n{body}\n"


class TestFrontmatter:
    def test_minimal_skill_parses(self) -> None:
        skill = parse_skill_md(doc("name: my-skill\ndescription: Helps with things."))
        assert skill.name == "my-skill"
        assert skill.description == "Helps with things."
        assert skill.content == "Do the thing."
        assert skill.license == ""
        assert skill.allowed_tools == ()

    def test_optional_fields_and_quoting(self) -> None:
        skill = parse_skill_md(
            doc(
                'name: "quoted-name"\n'
                "description: 'Single quoted description.'\n"
                "license: Apache-2.0\n"
                "allowed-tools: [Bash, WebFetch]"
            )
        )
        assert skill.name == "quoted-name"
        assert skill.description == "Single quoted description."
        assert skill.license == "Apache-2.0"
        assert skill.allowed_tools == ("Bash", "WebFetch")

    def test_block_list_allowed_tools(self) -> None:
        skill = parse_skill_md(doc("name: x1\ndescription: D.\nallowed-tools:\n  - Bash\n  - Read"))
        assert skill.allowed_tools == ("Bash", "Read")

    def test_crlf_line_endings(self) -> None:
        text = "---\r\nname: crlf-skill\r\ndescription: Windows line endings.\r\n---\r\nBody.\r\n"
        skill = parse_skill_md(text)
        assert skill.name == "crlf-skill"
        assert "Body." in skill.content

    def test_unknown_and_nested_keys_are_ignored(self) -> None:
        skill = parse_skill_md(
            doc(
                "name: extras\ndescription: D.\nversion: 3\nmetadata:\n"
                "  author: someone\n  category: writing"
            )
        )
        assert skill.name == "extras"

    def test_name_falls_back_to_the_folder_name(self) -> None:
        skill = parse_skill_md(doc("description: No name key."), default_name="folder-name")
        assert skill.name == "folder-name"

    def test_comments_and_blank_lines(self) -> None:
        fields = parse_frontmatter("# comment\n\nname: a\ndescription: b")
        assert fields == {"name": "a", "description": "b"}


class TestRejections:
    def test_missing_frontmatter(self) -> None:
        with pytest.raises(SkillParseError, match="frontmatter"):
            parse_skill_md("# Just markdown\n\nNo frontmatter here.")

    def test_unclosed_frontmatter(self) -> None:
        with pytest.raises(SkillParseError, match="frontmatter"):
            parse_skill_md("---\nname: x\ndescription: y\nno closing fence")

    def test_missing_description(self) -> None:
        with pytest.raises(SkillParseError, match="description"):
            parse_skill_md(doc("name: no-description"))

    @pytest.mark.parametrize(
        "name", ["Has Spaces", "UPPER", "-leading", "trailing-", "a" * 65, "under_score", ""]
    )
    def test_invalid_names(self, name: str) -> None:
        assert not is_valid_skill_name(name)
        with pytest.raises(SkillParseError, match=r"invalid|no description"):
            parse_skill_md(doc(f"name: {name}\ndescription: D."))

    def test_description_too_long(self) -> None:
        with pytest.raises(SkillParseError, match=str(MAX_DESCRIPTION_CHARS)):
            parse_skill_md(doc(f"name: x2\ndescription: {'d' * (MAX_DESCRIPTION_CHARS + 1)}"))

    def test_a_real_world_long_description_is_accepted(self) -> None:
        """anthropics/skills' xlsx description is 952 chars — under the old
        500-char cap these skills were rejected outright."""
        parsed = parse_skill_md(doc(f"name: xlsx\ndescription: {'d' * 952}"))
        assert len(parsed.description) == 952

    def test_body_too_large(self) -> None:
        body = "x" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(SkillParseError, match="256 KB"):
            parse_skill_md(doc("name: big\ndescription: D.", body))

    def test_frontmatter_too_large(self) -> None:
        with pytest.raises(SkillParseError, match="8 KB"):
            parse_skill_md(doc("name: y\ndescription: D.\njunk: " + "j" * 9000))

    def test_secret_content_is_rejected(self) -> None:
        with pytest.raises(SkillParseError, match="credential"):
            parse_skill_md(
                doc("name: leaky\ndescription: D.", "Use token ghp_" + "a" * 36 + " to auth.")
            )


class TestSecretsAndPaths:
    def test_find_secret_patterns(self) -> None:
        assert find_secret("-----BEGIN RSA PRIVATE KEY-----") == "private_key"
        assert find_secret("key AKIAABCDEFGHIJKLMNOP") == "aws_access_key"
        assert find_secret("Authorization: Bearer abcdefghijklmnop") == "bearer_header"
        assert find_secret("perfectly ordinary text about ski trips") is None

    def test_valid_file_paths(self) -> None:
        assert validate_file_path("template.md") == "template.md"
        assert validate_file_path("reference/deep/file.txt") == "reference/deep/file.txt"

    @pytest.mark.parametrize("path", ["/abs.md", "../up.md", "a/../b.md", "bad name.md", ""])
    def test_invalid_file_paths(self, path: str) -> None:
        with pytest.raises(SkillParseError):
            validate_file_path(path)


class TestBlockScalars:
    """YAML block scalars in real frontmatter.

    Every sample here is the shape of an actual SKILL.md in the browse
    catalog: ``anthropics/skills``' academy-guide and discernment-nudge use
    ``description: >``, and its claude-api skill uses ``description: |-``.
    Before block-scalar support these parsed to the literal ">" / "|-".
    """

    def test_folded_scalar_joins_lines_with_spaces(self) -> None:
        parsed = parse_skill_md(
            doc(
                "name: academy-guide\n"
                "description: >\n"
                "  Stop and check this skill before finishing any reply\n"
                "  about how to use Claude or a Claude product.\n"
                "license: Complete terms in LICENSE.txt"
            )
        )
        assert parsed.description == (
            "Stop and check this skill before finishing any reply "
            "about how to use Claude or a Claude product."
        )
        assert parsed.license == "Complete terms in LICENSE.txt"

    def test_literal_scalar_keeps_its_newlines(self) -> None:
        parsed = parse_skill_md(doc("name: n\ndescription: |\n  first line\n  second line"))
        assert parsed.description == "first line\nsecond line"

    def test_strip_chomping_indicator(self) -> None:
        parsed = parse_skill_md(doc("name: n\ndescription: |-\n  only line\n"))
        assert parsed.description == "only line"

    def test_folded_with_strip_chomping(self) -> None:
        parsed = parse_skill_md(doc("name: n\ndescription: >-\n  a\n  b\n"))
        assert parsed.description == "a b"

    def test_keep_chomping_indicator(self) -> None:
        fields = parse_frontmatter("name: n\ndescription: |+\n  line\n\n")
        assert fields["description"] == "line\n"

    def test_blank_line_is_a_paragraph_break_when_folded(self) -> None:
        parsed = parse_skill_md(doc("name: n\ndescription: >\n  para one\n\n  para two\n"))
        assert parsed.description == "para one\n\npara two"

    def test_a_following_top_level_key_ends_the_block(self) -> None:
        fields = parse_frontmatter("name: n\ndescription: >\n  folded text\nlicense: MIT\n")
        assert fields["description"] == "folded text\n"
        assert fields["license"] == "MIT"

    def test_explicit_indent_indicator(self) -> None:
        fields = parse_frontmatter("description: |2\n    indented by four\n")
        assert fields["description"] == "  indented by four\n"

    def test_block_scalar_still_respects_the_frontmatter_size_cap(self) -> None:
        huge = "\n".join("  padding padding padding" for _ in range(1000))
        with pytest.raises(SkillParseError):
            parse_frontmatter(f"description: >\n{huge}")


class TestQuotedScalarEscapes:
    """anthropics/skills' pptx/xlsx descriptions embed escaped quotes."""

    def test_double_quoted_escapes_are_unescaped(self) -> None:
        parsed = parse_skill_md(
            doc('name: pptx\ndescription: "Trigger on \\"deck,\\" or \\"slides.\\""')
        )
        assert parsed.description == 'Trigger on "deck," or "slides."'

    def test_single_quoted_doubling_is_unescaped(self) -> None:
        fields = parse_frontmatter("description: 'it''s fine'")
        assert fields["description"] == "it's fine"

    def test_a_plain_scalar_is_untouched(self) -> None:
        fields = parse_frontmatter("description: plain text, no quotes")
        assert fields["description"] == "plain text, no quotes"
