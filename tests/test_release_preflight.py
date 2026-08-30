"""Unit tests for the Phase 11 release preflight and bundle renderer."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

preflight = importlib.import_module("scripts.release_preflight")
renderer = importlib.import_module("scripts.render_release_bundle")


def test_compose_placeholders_extract_names_with_defaults_and_required_markers() -> None:
    text = "a: ${APP_NAME:-Jhin}\nb: ${POSTGRES_PASSWORD:?set it}\nc: ${PLAIN}\nd: $NOT_BRACED"
    assert preflight.compose_placeholders(text) == {"APP_NAME", "POSTGRES_PASSWORD", "PLAIN"}


def test_env_example_names_include_commented_assignments() -> None:
    text = "APP_NAME=Jhin\n# SANDBOX_NETWORK=jhin_sandbox\n#   FOO=bar\nnot a var\n"
    assert preflight.env_example_names(text) == {"APP_NAME", "SANDBOX_NETWORK", "FOO"}


def test_markdown_link_targets_skip_images_and_keep_paths() -> None:
    text = "see [docs](docs/deployment.md#sizing) and ![shot](img.png) and [web](https://x.y)"
    assert preflight.markdown_link_targets(text) == ["docs/deployment.md#sizing", "https://x.y"]


def test_check_markdown_links_reports_missing_targets(tmp_path: Path) -> None:
    (tmp_path / "present.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[good](present.md) [anchor](present.md#top) "
        "[ext](https://example.com) [bad](missing.md)\n",
        encoding="utf-8",
    )
    findings = preflight.check_markdown_links(tmp_path, [Path("README.md")])
    assert [finding.message for finding in findings] == ["README.md: broken link -> missing.md"]


def test_scan_for_secrets_flags_tokens_but_skips_tests_and_allowlist(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text('TOKEN = "ghp_' + "a" * 36 + '"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.py").write_text('T = "ghp_' + "b" * 36 + '"\n', encoding="utf-8")
    (tmp_path / "docs.md").write_text(
        "aws key AKIAIOSFODNN7EXAMPLE is the documented example\n", encoding="utf-8"
    )
    findings = preflight.scan_for_secrets(
        tmp_path, [Path("app.py"), Path("tests/fixture.py"), Path("docs.md")]
    )
    assert [finding.message for finding in findings] == ["app.py:1: possible GitHub token"]


def test_check_tracked_content_rejects_forbidden_path_and_superseded_name(tmp_path: Path) -> None:
    (tmp_path / "orgforge-production-implementation-plan.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "note.md").write_text("historical OrgForge mention\n", encoding="utf-8")
    (tmp_path / "docs" / "superpowers").mkdir(parents=True)
    (tmp_path / "docs" / "superpowers" / "plan.md").write_text(
        "OrgForge allowed here\n", encoding="utf-8"
    )
    findings = preflight.check_tracked_content(
        tmp_path,
        [
            Path("orgforge-production-implementation-plan.md"),
            Path("note.md"),
            Path("docs/superpowers/plan.md"),
        ],
    )
    assert {finding.message for finding in findings} == {
        "orgforge-production-implementation-plan.md must never be tracked",
        "note.md: superseded product identifier",
    }


def test_check_versions_detects_drift(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "packages" / "a").mkdir(parents=True)
    (tmp_path / "packages" / "a" / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text(
        json.dumps({"version": "0.1.0"}), encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    messages = {finding.message for finding in preflight.check_versions(tmp_path, expected="0.1.1")}
    assert messages == {
        "requested 0.1.1 but VERSION is 0.1.0",
        "packages/a/pyproject.toml declares '0.2.0', expected '0.1.0'",
        "CHANGELOG.md has no '## [0.1.0]' section",
    }


def test_changelog_excerpt_returns_one_section() -> None:
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n- wip\n\n## [0.1.0] - 2026-01-01\n\n"
        "### Added\n\n- thing\n\n## [0.0.1]\n\n- old\n"
    )
    assert preflight.changelog_excerpt(changelog, "0.1.0") == "### Added\n\n- thing\n"
    with pytest.raises(ValueError, match="no section"):
        preflight.changelog_excerpt(changelog, "9.9.9")


def test_repository_preflight_passes_for_this_checkout() -> None:
    findings = preflight.run_checks(ROOT)
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_cli_changelog_excerpt_matches_version_file() -> None:
    version = preflight.read_version_file(ROOT)
    result = subprocess.run(
        # sys.executable, not a bare "python": on Windows the bare name can
        # resolve to a system interpreter without the workspace deps.
        [sys.executable, "scripts/release_preflight.py", "changelog-excerpt", "--version", version],
        check=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.stdout.decode().strip()


def _lock(tmp_path: Path) -> Path:
    images = {
        f"jhin-{component}": {
            "repository": f"ghcr.io/teachmetech/jhin-{component}",
            "digest": "sha256:" + f"{index:064x}",
        }
        for index, component in enumerate(renderer.COMPONENTS, start=1)
    }
    lock = tmp_path / "image-lock.json"
    lock.write_text(
        json.dumps({"schema_version": 1, "version": "0.1.0", "images": images}), encoding="utf-8"
    )
    return lock


def test_render_bundle_pins_every_first_party_image_and_writes_manifest(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    renderer.render_bundle(
        ROOT, output, "0.1.0", _lock(tmp_path), "Teachmetech/Jhin", allow_unpinned=False
    )
    compose = (output / "compose.yaml").read_text(encoding="utf-8")
    assert ":${JHIN_VERSION" not in compose
    assert "build:" not in compose
    for component in renderer.COMPONENTS:
        if component == "sandbox":
            continue
        assert f"ghcr.io/teachmetech/jhin-{component}@sha256:" in compose
    env = (output / ".env.release.example").read_text(encoding="utf-8")
    assert "JHIN_VERSION=0.1.0" in env
    assert "SANDBOX_DEFAULT_IMAGE=ghcr.io/teachmetech/jhin-sandbox@sha256:" in env
    assert "SANDBOX_RUNNER_IMAGE=ghcr.io/teachmetech/jhin-sandbox-runner@sha256:" in env
    rootless = (output / "compose.rootless.yaml").read_text(encoding="utf-8")
    assert "ghcr.io/teachmetech/jhin-sandbox-runner@sha256:" in rootless
    manifest = (output / "MANIFEST.SHA256").read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1] for line in manifest}
    assert "MANIFEST.SHA256" not in listed
    assert {
        "compose.yaml",
        "VERIFY.md",
        "config/nats.conf",
        "docs/deployment.md",
        "LICENSE",
    } <= listed
    assert all(len(line.split("  ", 1)[0]) == 64 for line in manifest)


def test_render_bundle_refuses_tag_only_images_without_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing digests"):
        renderer.render_bundle(
            ROOT, tmp_path / "out", "0.1.0", None, "Teachmetech/Jhin", allow_unpinned=False
        )


def test_render_bundle_dry_run_uses_version_tags(tmp_path: Path) -> None:
    output = renderer.render_bundle(
        ROOT, tmp_path / "out", "0.1.0", None, "Teachmetech/Jhin", allow_unpinned=True
    )
    compose = (output / "compose.yaml").read_text(encoding="utf-8")
    assert "ghcr.io/teachmetech/jhin-api:0.1.0" in compose


def test_validate_compose_rejects_build_keys_and_dev_markers() -> None:
    with pytest.raises(ValueError, match="build key"):
        renderer.validate_compose(
            "services:\n  api:\n    build: .\n    healthcheck: {}\n", allow_unpinned=True
        )
    with pytest.raises(ValueError, match="development marker"):
        renderer.validate_compose("services:\n  fake-linear:\n    image: x\n", allow_unpinned=True)
