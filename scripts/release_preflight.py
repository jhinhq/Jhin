"""Repository-completable publication gate for a Jhin release (Phase 11, spec 7.1).

Usage:
    uv run python scripts/release_preflight.py [--version X.Y.Z] [--only a,b]
    uv run python scripts/release_preflight.py changelog-excerpt --version X.Y.Z

Every check reads tracked repository content only and writes nothing. The
script exits non-zero with one line per failure; it never creates a tag,
release, package, or credential.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "VERSION",
    ".env.example",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/e2e.yml",
    ".github/workflows/security.yml",
    ".github/workflows/release.yml",
    "deploy/compose.release.yaml",
    "deploy/compose.release.rootful.yaml",
    "deploy/compose.release.rootless.yaml",
    "deploy/.env.release.example",
    "deploy/README.md",
    "docs/README.md",
    "docs/deployment.md",
    "docs/demo.md",
    "docs/templates.md",
    "docs/architecture/README.md",
    "config/nats.conf",
)

# The untracked, user-owned production-plan reference must never be staged
# (spec 3.2). Superseded product identifiers are allowed only in historical
# plan/evidence documents.
FORBIDDEN_TRACKED_PATHS = ("orgforge-production-implementation-plan.md",)
SUPERSEDED_NAME_PATTERN = re.compile(r"orgforge", re.IGNORECASE)
NAMING_ALLOWLIST_PREFIXES = (
    "docs/superpowers/",
    "docs/architecture/vercel-and-supabase.md",
    "scripts/release_preflight.py",
    "tests/test_release_preflight.py",
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("Slack token", re.compile(r"\bxox[abp]-[0-9A-Za-z-]{20,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Linear API key", re.compile(r"\blin_api_[A-Za-z0-9]{30,}\b")),
    (
        "private key block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----\s*\n[A-Za-z0-9+/=]{40,}"
        ),
    ),
    ("Sigstore/JWT-looking token", re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{40,}\.")),
)
SECRET_SCAN_SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2")
SECRET_SCAN_SKIP_PARTS = ("tests", "testing", "fixtures", "superpowers")
SECRET_SCAN_ALLOWLIST = ("AKIAIOSFODNN7EXAMPLE",)

MARKDOWN_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
COMPOSE_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::?[-?+][^}]*)?\}")
ENV_ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)

COMPOSE_ENV_CONTRACTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "compose.yaml",
            "compose.dev.yaml",
            "compose.rootful.yaml",
            "compose.rootless.yaml",
            "compose.desktop.yaml",
        ),
        ".env.example",
    ),
    (
        (
            "deploy/compose.release.yaml",
            "deploy/compose.release.rootful.yaml",
            "deploy/compose.release.rootless.yaml",
        ),
        "deploy/.env.release.example",
    ),
)

WORKSPACE_MEMBER_GLOBS = (
    "apps/*/pyproject.toml",
    "services/*/pyproject.toml",
    "packages/*/pyproject.toml",
)


@dataclass(frozen=True)
class Finding:
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.message}"


def tracked_files(root: Path) -> list[Path]:
    """Return tracked, still-present repository files relative to ``root``."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    names = [name for name in result.stdout.decode().split("\0") if name]
    return [Path(name) for name in names if (root / name).is_file()]


def read_version_file(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def check_required_files(root: Path) -> list[Finding]:
    return [
        Finding("files", f"missing required file: {name}")
        for name in REQUIRED_FILES
        if not (root / name).is_file()
    ]


def check_license(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    license_path = root / "LICENSE"
    if not license_path.is_file():
        return [Finding("license", "LICENSE is missing")]
    text = license_path.read_text(encoding="utf-8")
    if "Apache License" not in text or "Version 2.0" not in text:
        findings.append(Finding("license", "LICENSE is not the Apache License, Version 2.0"))
    readme = root / "README.md"
    if readme.is_file() and "Apache-2.0" not in readme.read_text(encoding="utf-8"):
        findings.append(Finding("license", "README.md does not state the Apache-2.0 license"))
    return findings


def _load_yaml(path: Path) -> object:
    with path.open("rb") as handle:
        return yaml.safe_load(handle)


def check_workflows(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        rel = path.relative_to(root).as_posix()
        try:
            document = _load_yaml(path)
        except yaml.YAMLError as error:
            findings.append(Finding("workflows", f"{rel}: invalid YAML: {error}"))
            continue
        if not isinstance(document, dict):
            findings.append(Finding("workflows", f"{rel}: not a mapping"))
            continue
        # PyYAML parses the bare key `on` as boolean True.
        if "on" not in document and True not in document:
            findings.append(Finding("workflows", f"{rel}: missing 'on' trigger"))
        jobs = document.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            findings.append(Finding("workflows", f"{rel}: missing jobs"))
            continue
        for name, job in jobs.items():
            if not isinstance(job, dict):
                findings.append(Finding("workflows", f"{rel}: job {name} is not a mapping"))
                continue
            if "runs-on" not in job and "uses" not in job:
                findings.append(Finding("workflows", f"{rel}: job {name} has no runs-on"))
            if "uses" not in job and not job.get("steps"):
                findings.append(Finding("workflows", f"{rel}: job {name} has no steps"))
    for path in sorted((root / ".github" / "ISSUE_TEMPLATE").glob("*.yml")):
        rel = path.relative_to(root).as_posix()
        try:
            document = _load_yaml(path)
        except yaml.YAMLError as error:
            findings.append(Finding("workflows", f"{rel}: invalid YAML: {error}"))
            continue
        if not isinstance(document, dict):
            findings.append(Finding("workflows", f"{rel}: not a mapping"))
            continue
        if path.name == "config.yml":
            if document.get("blank_issues_enabled") is not False:
                findings.append(Finding("workflows", f"{rel}: blank issues must be disabled"))
            continue
        for key in ("name", "description", "body"):
            if key not in document:
                findings.append(Finding("workflows", f"{rel}: issue form missing '{key}'"))
    dependabot = root / ".github" / "dependabot.yml"
    if dependabot.is_file():
        document = _load_yaml(dependabot)
        if not isinstance(document, dict) or document.get("version") != 2:
            findings.append(Finding("workflows", "dependabot.yml must declare version: 2"))
    return findings


def _pyproject_version(path: Path) -> str | None:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def changelog_has_version(changelog: str, version: str) -> bool:
    return re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE) is not None


def check_versions(root: Path, expected: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    version_path = root / "VERSION"
    if not version_path.is_file():
        return [Finding("versions", "VERSION file is missing")]
    version = read_version_file(root)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-rc\.\d+)?", version):
        findings.append(Finding("versions", f"VERSION '{version}' is not MAJOR.MINOR.PATCH[-rc.N]"))
    if expected is not None and expected != version:
        findings.append(Finding("versions", f"requested {expected} but VERSION is {version}"))

    pyprojects = [root / "pyproject.toml"]
    for pattern in WORKSPACE_MEMBER_GLOBS:
        pyprojects.extend(sorted(root.glob(pattern)))
    for path in pyprojects:
        if not path.is_file():
            continue
        found = _pyproject_version(path)
        if found != version:
            rel = path.relative_to(root).as_posix()
            findings.append(Finding("versions", f"{rel} declares {found!r}, expected {version!r}"))

    package_json = root / "apps" / "web" / "package.json"
    if package_json.is_file():
        data = json.loads(package_json.read_text(encoding="utf-8"))
        if data.get("version") != version:
            findings.append(
                Finding(
                    "versions",
                    f"apps/web/package.json declares {data.get('version')!r}, expected {version!r}",
                )
            )

    changelog = root / "CHANGELOG.md"
    if changelog.is_file() and not changelog_has_version(
        changelog.read_text(encoding="utf-8"), version
    ):
        findings.append(Finding("versions", f"CHANGELOG.md has no '## [{version}]' section"))
    return findings


def compose_placeholders(text: str) -> set[str]:
    return {match.group(1) for match in COMPOSE_PLACEHOLDER.finditer(text)}


def env_example_names(text: str) -> set[str]:
    return {match.group(1) for match in ENV_ASSIGNMENT.finditer(text)}


def check_env_example(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for compose_names, env_name in COMPOSE_ENV_CONTRACTS:
        env_path = root / env_name
        if not env_path.is_file():
            findings.append(Finding("env", f"{env_name} is missing"))
            continue
        documented = env_example_names(env_path.read_text(encoding="utf-8"))
        for compose_name in compose_names:
            compose_path = root / compose_name
            if not compose_path.is_file():
                continue
            used = compose_placeholders(compose_path.read_text(encoding="utf-8"))
            for name in sorted(used - documented):
                findings.append(
                    Finding("env", f"{compose_name} uses ${{{name}}} but {env_name} omits it")
                )
    return findings


def _secret_scan_skipped(path: Path) -> bool:
    if path.suffix in SECRET_SCAN_SKIP_SUFFIXES:
        return True
    return any(part in SECRET_SCAN_SKIP_PARTS for part in path.parts)


def scan_for_secrets(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files:
        if _secret_scan_skipped(rel):
            continue
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                if match.group(0) in SECRET_SCAN_ALLOWLIST:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(Finding("secrets", f"{rel.as_posix()}:{line}: possible {label}"))
                break
    return findings


def check_secrets(root: Path) -> list[Finding]:
    return scan_for_secrets(root, tracked_files(root))


def markdown_link_targets(text: str) -> list[str]:
    return [match.group(1) for match in MARKDOWN_LINK.finditer(text)]


def check_markdown_links(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        for target in markdown_link_targets(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if "://" in target:
                continue
            cleaned = target.split("#", 1)[0]
            if not cleaned:
                continue
            resolved = (path.parent / cleaned).resolve()
            if not resolved.exists():
                findings.append(Finding("links", f"{rel.as_posix()}: broken link -> {target}"))
    return findings


def docs_markdown_files(root: Path) -> list[Path]:
    candidates = [
        Path("README.md"),
        Path("CONTRIBUTING.md"),
        Path("SECURITY.md"),
        Path("SUPPORT.md"),
        Path("CODE_OF_CONDUCT.md"),
        Path("CHANGELOG.md"),
        Path("deploy/README.md"),
        Path(".github/pull_request_template.md"),
    ]
    candidates.extend(
        path.relative_to(root)
        for path in sorted((root / "docs").rglob("*.md"))
        if "superpowers" not in path.parts
    )
    return [rel for rel in candidates if (root / rel).is_file()]


def check_links(root: Path) -> list[Finding]:
    return check_markdown_links(root, docs_markdown_files(root))


def check_tracked_content(root: Path, files: Iterable[Path] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    tracked = list(files) if files is not None else tracked_files(root)
    tracked_names = {rel.as_posix() for rel in tracked}
    for forbidden in FORBIDDEN_TRACKED_PATHS:
        if forbidden in tracked_names:
            findings.append(Finding("tracked", f"{forbidden} must never be tracked"))
    for rel in tracked:
        name = rel.as_posix()
        if name.startswith(NAMING_ALLOWLIST_PREFIXES) or rel.suffix in SECRET_SCAN_SKIP_SUFFIXES:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if SUPERSEDED_NAME_PATTERN.search(text):
            findings.append(Finding("tracked", f"{name}: superseded product identifier"))
    return findings


def changelog_excerpt(changelog: str, version: str) -> str:
    """Return the body of the ``## [version]`` section without its heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    start = heading.search(changelog)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    rest = changelog[start.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    body = rest if next_heading is None else rest[: next_heading.start()]
    return body.strip() + "\n"


CHECKS: dict[str, Callable[[Path], list[Finding]]] = {
    "files": check_required_files,
    "license": check_license,
    "workflows": check_workflows,
    "versions": check_versions,
    "env": check_env_example,
    "secrets": check_secrets,
    "links": check_links,
    "tracked": check_tracked_content,
}


def run_checks(
    root: Path, only: Iterable[str] | None = None, version: str | None = None
) -> list[Finding]:
    selected = list(only) if only else list(CHECKS)
    unknown = sorted(set(selected) - set(CHECKS))
    if unknown:
        raise SystemExit(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(CHECKS)}")
    findings: list[Finding] = []
    for name in selected:
        if name == "versions":
            findings.extend(check_versions(root, version))
        else:
            findings.extend(CHECKS[name](root))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: this checkout)"
    )
    parser.add_argument("--version", help="expected release version; must equal VERSION")
    parser.add_argument("--only", help="comma-separated subset of checks to run")
    subcommands = parser.add_subparsers(dest="command")
    excerpt = subcommands.add_parser("changelog-excerpt", help="print one CHANGELOG.md section")
    excerpt.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.root.resolve()
    if args.command == "changelog-excerpt":
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        sys.stdout.write(changelog_excerpt(changelog, args.version))
        return 0
    only = [name.strip() for name in args.only.split(",")] if args.only else None
    findings = run_checks(root, only, args.version)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"release preflight failed: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    selected = ", ".join(only) if only else "all checks"
    print(f"release preflight passed ({selected}) for version {read_version_file(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
