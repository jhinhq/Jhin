"""Fetch a GitHub repository as a zip for skill import.

Uses the public ``codeload.github.com`` archive endpoint over HTTPS — no
git binary, no clone, no credentials. The reference is a plain
``owner/repo`` or ``owner/repo/path`` string typed by an admin; redirects
are never followed and both declared and streamed sizes are capped at 5 MB
(the same posture as the shared bounded provider response handling).
"""

from __future__ import annotations

import re

import httpx

from jhin_skills.bundle import MAX_ZIP_BYTES

_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/(.+))?$")
_TIMEOUT_SECONDS = 30.0


class SkillImportError(ValueError):
    """A display-safe import failure (bad reference, fetch failed, too big)."""


def parse_github_ref(ref: str) -> tuple[str, str, str]:
    """``owner/repo[/path]`` → (owner, repo, path). Raises on anything else."""
    match = _REF_RE.fullmatch(ref.strip().strip("/"))
    if match is None:
        raise SkillImportError(
            "use the form owner/repo or owner/repo/path (e.g. anthropics/skills)"
        )
    owner, repo, path = match.group(1), match.group(2), (match.group(3) or "").strip("/")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise SkillImportError("invalid repository reference")
    if any(part == ".." for part in path.split("/")):
        raise SkillImportError("invalid repository path")
    return owner, repo, path


def codeload_url(owner: str, repo: str) -> str:
    """The default-branch zip archive URL for a public repository."""
    return f"https://codeload.github.com/{owner}/{repo}/zip/HEAD"


def source_url_for(owner: str, repo: str, path: str) -> str:
    base = f"https://github.com/{owner}/{repo}"
    return f"{base}/tree/HEAD/{path}" if path else base


async def fetch_github_repo_zip(ref: str) -> tuple[bytes, str, str]:
    """Fetch the repo zip for ``owner/repo[/path]``.

    Returns ``(zip_bytes, path_prefix, source_url)``. Failures raise
    :class:`SkillImportError` with a bounded, credential-free message.
    """
    owner, repo, path = parse_github_ref(ref)
    url = codeload_url(owner, repo)
    try:
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT_SECONDS) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise SkillImportError(
                    f"GitHub returned status {response.status_code} for {owner}/{repo} "
                    "(is it a public repository?)"
                )
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > MAX_ZIP_BYTES:
                raise SkillImportError("the repository archive is larger than 5 MB")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_ZIP_BYTES:
                    raise SkillImportError("the repository archive is larger than 5 MB")
                body.extend(chunk)
    except SkillImportError:
        raise
    except httpx.HTTPError:
        raise SkillImportError(f"could not reach GitHub for {owner}/{repo}") from None
    return bytes(body), path, source_url_for(owner, repo, path)
