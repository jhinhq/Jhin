"""SKILL.md parsing: frontmatter variants, bad input, limits, secrets."""

from __future__ import annotations

import pytest

from jhin_skills import (
    MAX_CONTENT_BYTES,
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
        with pytest.raises(SkillParseError, match="500"):
            parse_skill_md(doc(f"name: x2\ndescription: {'d' * 501}"))

    def test_body_too_large(self) -> None:
        body = "x" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(SkillParseError, match="64 KB"):
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
