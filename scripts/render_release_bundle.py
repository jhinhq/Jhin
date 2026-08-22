"""Render the digest-pinned release Compose bundle (Phase 11, spec 6.3).

Usage:
    uv run python scripts/render_release_bundle.py --version 0.1.0 \
        --image-lock image-lock.json --output dist/jhin-0.1.0-compose
    uv run python scripts/render_release_bundle.py --version 0.1.0 \
        --output /tmp/bundle --allow-unpinned   # CI dry run without digests

The bundle root contains compose.yaml (digest-pinned first-party images),
the rootful/rootless overlays, config/nats.conf, .env.release.example,
image-lock.json, VERIFY.md, MANIFEST.SHA256, LICENSE, README.md, SECURITY.md,
and docs/deployment.md. Rendering fails if any first-party image is still
tag-only (unless --allow-unpinned), any service has a build key, any required
service lacks a healthcheck, or a development-only marker appears.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = (
    "web",
    "api",
    "workflow-worker",
    "agent-worker",
    "tool-worker",
    "event-worker",
    "sandbox-runner",
    "sandbox",
)
DEFAULT_NAMESPACE = "ghcr.io/teachmetech"
IMAGE_REFERENCE = re.compile(
    r"\$\{JHIN_IMAGE_NAMESPACE:-(?P<namespace>[^}]+)\}/(?P<name>jhin-[a-z-]+):\$\{JHIN_VERSION[^}]*\}"
)
FORBIDDEN_MARKERS = (
    "fake-",
    "supabase_fixture",
    "reader-pass",
    "writer-pass",
    "phase9-fixture-admin-only",
    "dev-sandbox-runner-token",
    "jhin-dev-password",
    "JHIN_TEST_CRASH_BARRIER",
)
REQUIRED_HEALTHCHECKS = (
    "web",
    "api",
    "workflow-worker",
    "agent-worker",
    "tool-worker",
    "event-worker",
    "sandbox-runner",
    "postgres",
    "nats",
    "temporal",
)
COPIED_FILES = (
    ("LICENSE", "LICENSE"),
    ("README.md", "README.md"),
    ("SECURITY.md", "SECURITY.md"),
    ("docs/deployment.md", "docs/deployment.md"),
    ("config/nats.conf", "config/nats.conf"),
    ("deploy/.env.release.example", ".env.release.example"),
    ("deploy/compose.release.rootful.yaml", "compose.rootful.yaml"),
)


def load_image_lock(path: Path | None) -> dict[str, str]:
    """Return ``{component: digest}`` from an image-lock.json, or an empty map."""
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    images = data.get("images")
    if not isinstance(images, dict):
        raise ValueError("image-lock.json must contain an 'images' mapping")
    digests: dict[str, str] = {}
    for name, entry in images.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
            raise ValueError(f"image-lock.json entry {name!r} lacks a digest")
        component = name.removeprefix("jhin-")
        digest = entry["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"image-lock.json entry {name!r} has a malformed digest")
        digests[component] = digest
    return digests


def pin_images(
    compose_text: str, version: str, digests: dict[str, str], allow_unpinned: bool
) -> str:
    """Replace ``${JHIN_IMAGE_NAMESPACE}/jhin-x:${JHIN_VERSION}`` references."""

    def replace(match: re.Match[str]) -> str:
        namespace = match.group("namespace")
        name = match.group("name")
        component = name.removeprefix("jhin-")
        digest = digests.get(component)
        if digest is None:
            if not allow_unpinned:
                raise ValueError(f"no digest for {name}; refusing to render a tag-only bundle")
            return f"{namespace}/{name}:{version}"
        return f"{namespace}/{name}@{digest}"

    return IMAGE_REFERENCE.sub(replace, compose_text)


def validate_compose(text: str, allow_unpinned: bool) -> None:
    document = yaml.safe_load(text)
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ValueError("rendered compose has no services mapping")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError(f"service {name} is not a mapping")
        if "build" in service:
            raise ValueError(f"service {name} has a build key")
        if name in REQUIRED_HEALTHCHECKS and "healthcheck" not in service:
            raise ValueError(f"service {name} lacks a healthcheck")
        image = service.get("image")
        if (
            isinstance(image, str)
            and "/jhin-" in image
            and "@sha256:" not in image
            and not allow_unpinned
        ):
            raise ValueError(f"service {name} image is not digest-pinned: {image}")
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            raise ValueError(f"rendered compose contains development marker: {marker}")


def render_env_example(text: str, version: str, digests: dict[str, str], namespace: str) -> str:
    """Fill the release identity and pinned sandbox image values."""
    runner = digests.get("sandbox-runner")
    sandbox = digests.get("sandbox")
    runner_ref = (
        f"{namespace}/jhin-sandbox-runner@{runner}"
        if runner
        else f"{namespace}/jhin-sandbox-runner:{version}"
    )
    sandbox_ref = (
        f"{namespace}/jhin-sandbox@{sandbox}" if sandbox else f"{namespace}/jhin-sandbox:{version}"
    )
    replacements = {
        "JHIN_VERSION=": f"JHIN_VERSION={version}",
        "SANDBOX_RUNNER_IMAGE=": f"SANDBOX_RUNNER_IMAGE={runner_ref}",
        "SANDBOX_DEFAULT_IMAGE=": f"SANDBOX_DEFAULT_IMAGE={sandbox_ref}",
    }
    lines = []
    for line in text.splitlines():
        replacement = replacements.get(line)
        lines.append(replacement if replacement is not None else line)
    return "\n".join(lines) + "\n"


def render_rootless_overlay(
    text: str, version: str, digests: dict[str, str], namespace: str
) -> str:
    runner = digests.get("sandbox-runner")
    ref = (
        f"{namespace}/jhin-sandbox-runner@{runner}"
        if runner
        else f"{namespace}/jhin-sandbox-runner:{version}"
    )
    return text.replace("${SANDBOX_RUNNER_IMAGE:?set SANDBOX_RUNNER_IMAGE}", ref)


def render_verify_md(version: str, repository: str, digests: dict[str, str], namespace: str) -> str:
    identity = f"^https://github.com/{repository}/.github/workflows/release.yml@refs/tags/v"
    lines = [
        f"# Verify Jhin {version}",
        "",
        "Run these commands from the directory that contains the downloaded release",
        "assets before starting anything. Each step fails closed.",
        "",
        "```bash",
        "cosign verify-blob \\",
        "  --bundle SHA256SUMS.sigstore.json \\",
        f"  --certificate-identity-regexp '{identity}' \\",
        "  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
        "  SHA256SUMS",
        "sha256sum --check --ignore-missing SHA256SUMS",
        f"tar -xzf jhin-{version}-compose.tar.gz",
        f"cd jhin-{version}-compose && sha256sum --check MANIFEST.SHA256",
        "```",
        "",
        "Image signatures (one command per image; digests are in image-lock.json):",
        "",
        "```bash",
    ]
    for component in COMPONENTS:
        digest = digests.get(component)
        reference = (
            f"{namespace}/jhin-{component}@{digest}"
            if digest
            else f"{namespace}/jhin-{component}:{version}"
        )
        lines.extend(
            [
                "cosign verify \\",
                f"  --certificate-identity-regexp '{identity}' \\",
                "  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
                f"  {reference}",
            ]
        )
    lines.extend(["```", ""])
    return "\n".join(lines)


def write_manifest(bundle: Path) -> None:
    """Write MANIFEST.SHA256 over every regular file except itself (byte-sorted)."""
    entries: list[tuple[str, str]] = []
    for path in sorted(bundle.rglob("*"), key=lambda p: p.relative_to(bundle).as_posix().encode()):
        if path.is_symlink():
            raise ValueError(f"bundle must not contain symlinks: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(bundle).as_posix()
        if rel == "MANIFEST.SHA256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, rel))
    (bundle / "MANIFEST.SHA256").write_text(
        "".join(f"{digest}  {rel}\n" for digest, rel in entries), encoding="utf-8"
    )


def render_bundle(
    root: Path,
    output: Path,
    version: str,
    image_lock: Path | None,
    repository: str,
    allow_unpinned: bool,
) -> Path:
    digests = load_image_lock(image_lock)
    if not allow_unpinned and set(digests) != set(COMPONENTS):
        missing = sorted(set(COMPONENTS) - set(digests))
        raise ValueError(f"image-lock.json is missing digests for: {', '.join(missing)}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    output.mkdir(parents=True)

    compose_source = (root / "deploy" / "compose.release.yaml").read_text(encoding="utf-8")
    namespace_match = IMAGE_REFERENCE.search(compose_source)
    namespace = namespace_match.group("namespace") if namespace_match else DEFAULT_NAMESPACE
    compose_text = pin_images(compose_source, version, digests, allow_unpinned)
    validate_compose(compose_text, allow_unpinned)
    (output / "compose.yaml").write_text(compose_text, encoding="utf-8")

    rootless = (root / "deploy" / "compose.release.rootless.yaml").read_text(encoding="utf-8")
    (output / "compose.rootless.yaml").write_text(
        render_rootless_overlay(rootless, version, digests, namespace), encoding="utf-8"
    )
    for source, target in COPIED_FILES:
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        if target == ".env.release.example":
            env_text = (root / source).read_text(encoding="utf-8")
            destination.write_text(
                render_env_example(env_text, version, digests, namespace), encoding="utf-8"
            )
        else:
            shutil.copyfile(root / source, destination)
    if image_lock is not None:
        shutil.copyfile(image_lock, output / "image-lock.json")
    (output / "VERIFY.md").write_text(
        render_verify_md(version, repository, digests, namespace), encoding="utf-8"
    )
    write_manifest(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-lock", type=Path, default=None)
    parser.add_argument("--repository", default="Teachmetech/Jhin")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--allow-unpinned", action="store_true", help="render tag references (dry run)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle = render_bundle(
        args.root.resolve(),
        args.output,
        args.version,
        args.image_lock,
        args.repository,
        args.allow_unpinned,
    )
    print(f"rendered release bundle at {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
