"""Bundle loading: zip layouts (flat, GitHub-wrapped, multi-skill),
size caps, warnings, and the built-in starter library."""

from __future__ import annotations

import io
import zipfile

import pytest

from jhin_skills import (
    MAX_FILE_BYTES,
    BundleError,
    load_builtin_skills,
    load_zip,
    parse_github_ref,
    source_url_for,
)
from jhin_skills.github import SkillImportError, codeload_url

GOOD_SKILL = "---\nname: {name}\ndescription: Description of {name}.\n---\n\nInstructions.\n"


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


class TestLoadZip:
    def test_github_wrapped_multi_skill_repo_layout(self) -> None:
        """The anthropics/skills layout: one wrapper dir, category folders."""
        data = make_zip(
            {
                "skills-HEAD/README.md": "# repo readme (not a skill)",
                "skills-HEAD/writing/blog-posts/SKILL.md": GOOD_SKILL.format(name="blog-posts"),
                "skills-HEAD/writing/blog-posts/outline.md": "## Outline template",
                "skills-HEAD/coding/debugging/SKILL.md": GOOD_SKILL.format(name="debugging"),
            }
        )
        result = load_zip(data)
        names = [loaded.skill.name for loaded in result.skills]
        assert names == ["debugging", "blog-posts"] or sorted(names) == ["blog-posts", "debugging"]
        blog = next(loaded for loaded in result.skills if loaded.skill.name == "blog-posts")
        assert [file.path for file in blog.files] == ["outline.md"]
        assert blog.files[0].content == "## Outline template"
        assert result.warnings == ()

    def test_path_prefix_selects_a_subtree(self) -> None:
        data = make_zip(
            {
                "repo-HEAD/skills/one/SKILL.md": GOOD_SKILL.format(name="one"),
                "repo-HEAD/other/two/SKILL.md": GOOD_SKILL.format(name="two"),
            }
        )
        result = load_zip(data, path_prefix="skills")
        assert [loaded.skill.name for loaded in result.skills] == ["one"]

    def test_flat_upload_without_wrapper(self) -> None:
        data = make_zip({"my-skill/SKILL.md": GOOD_SKILL.format(name="my-skill")})
        result = load_zip(data)
        assert [loaded.skill.name for loaded in result.skills] == ["my-skill"]

    def test_root_level_skill_md(self) -> None:
        data = make_zip(
            {"SKILL.md": GOOD_SKILL.format(name="root-skill"), "extra.md": "extra text"}
        )
        result = load_zip(data)
        assert result.skills[0].skill.name == "root-skill"
        assert [file.path for file in result.skills[0].files] == ["extra.md"]

    def test_nested_skill_files_are_not_absorbed_by_the_parent(self) -> None:
        data = make_zip(
            {
                "parent/SKILL.md": GOOD_SKILL.format(name="parent"),
                "parent/child/SKILL.md": GOOD_SKILL.format(name="child"),
                "parent/child/ref.md": "belongs to child",
            }
        )
        result = load_zip(data)
        by_name = {loaded.skill.name: loaded for loaded in result.skills}
        assert [file.path for file in by_name["parent"].files] == []
        assert [file.path for file in by_name["child"].files] == ["ref.md"]

    def test_broken_skill_is_skipped_with_a_warning(self) -> None:
        data = make_zip(
            {
                "good/SKILL.md": GOOD_SKILL.format(name="good"),
                "bad/SKILL.md": "no frontmatter at all",
            }
        )
        result = load_zip(data)
        assert [loaded.skill.name for loaded in result.skills] == ["good"]
        assert any("bad" in warning for warning in result.warnings)

    def test_duplicate_names_keep_the_first(self) -> None:
        data = make_zip(
            {
                "a/SKILL.md": GOOD_SKILL.format(name="same"),
                "b/SKILL.md": GOOD_SKILL.format(name="same"),
            }
        )
        result = load_zip(data)
        assert len(result.skills) == 1
        assert any("duplicate" in warning for warning in result.warnings)

    def test_non_utf8_reference_file_is_skipped(self) -> None:
        data = make_zip(
            {
                "s/SKILL.md": GOOD_SKILL.format(name="s"),
                "s/binary.dat": b"\xff\xfe\x00binary",
            }
        )
        result = load_zip(data)
        assert result.skills[0].files == ()
        assert any("not UTF-8" in warning for warning in result.warnings)

    def test_no_skill_md_anywhere(self) -> None:
        with pytest.raises(BundleError, match=r"no SKILL\.md"):
            load_zip(make_zip({"readme.md": "just a file"}))

    def test_not_a_zip(self) -> None:
        with pytest.raises(BundleError, match="valid zip"):
            load_zip(b"definitely not a zip")

    def test_zip_too_large(self) -> None:
        with pytest.raises(BundleError, match="5 MB"):
            load_zip(b"0" * (5 * 1024 * 1024 + 1))

    def test_oversize_member_is_dropped(self) -> None:
        data = make_zip(
            {
                "s/SKILL.md": GOOD_SKILL.format(name="s"),
                "s/huge.md": "x" * (MAX_FILE_BYTES + 1),
            }
        )
        result = load_zip(data)
        assert result.skills[0].files == ()

    def test_empty_prefix_match(self) -> None:
        data = make_zip({"a/SKILL.md": GOOD_SKILL.format(name="a")})
        with pytest.raises(BundleError, match="nothing found"):
            load_zip(data, path_prefix="does-not-exist")


class TestBuiltins:
    def test_five_starters_load_cleanly(self) -> None:
        skills = load_builtin_skills()
        assert [loaded.skill.name for loaded in skills] == [
            "bug-report-triage",
            "code-review-checklist",
            "meeting-notes-summary",
            "release-notes",
            "writing-clear-updates",
        ]
        for loaded in skills:
            assert loaded.skill.description
            assert len(loaded.skill.content) > 200
        release = next(loaded for loaded in skills if loaded.skill.name == "release-notes")
        assert [file.path for file in release.files] == ["template.md"]


class TestGithubRef:
    def test_parse_owner_repo(self) -> None:
        assert parse_github_ref("anthropics/skills") == ("anthropics", "skills", "")
        assert parse_github_ref(" anthropics/skills/ ") == ("anthropics", "skills", "")

    def test_parse_with_path(self) -> None:
        owner, repo, path = parse_github_ref("anthropics/skills/document-skills")
        assert (owner, repo, path) == ("anthropics", "skills", "document-skills")

    @pytest.mark.parametrize(
        "ref",
        ["", "justonepart", "owner//repo", "own er/repo", "../x/y", "owner/repo/../up"],
    )
    def test_bad_refs(self, ref: str) -> None:
        with pytest.raises(SkillImportError):
            parse_github_ref(ref)

    def test_urls(self) -> None:
        assert (
            codeload_url("anthropics", "skills")
            == "https://codeload.github.com/anthropics/skills/zip/HEAD"
        )
        assert source_url_for("a", "b", "") == "https://github.com/a/b"
        assert source_url_for("a", "b", "sub/dir") == "https://github.com/a/b/tree/HEAD/sub/dir"
