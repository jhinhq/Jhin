"""Conservative app identity for browsing; never connection or trust identity.

The import key identifies a registry record, not an app. Multiple registries
can describe the same endpoint, package, or repository. These tokens let a
reader choose one card without rewriting any imported handle or installation.
Skills deliberately do not participate: a repository can contain many skills.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jhin_catalog_sync.wire import safe_slug

_WORDS = re.compile(r"[^\w]+", re.UNICODE)
_DECORATIONS = frozenset({"mcp", "server", "official", "api"})
_SCOPED_PACKAGE = re.compile(r"^@[a-z0-9~._-]+/([a-z0-9~._-]+)$", re.IGNORECASE)
_SMITHERY_QUALIFIED = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$", re.IGNORECASE)
_MAX_ITEMS = 20

# Exact reviewed wrappers of built-in apps. Repository subpaths are part of
# identity: the Smithery monorepo itself and its other tools stay independent.
# Sources: github.com/{smithery-ai/mcp-servers,pipeworx-io/mcp-notion_connect,
# pipeworx-io/mcp-github_private,n24q02m/better-notion-mcp}.
_BUILTIN_REPOSITORIES: Mapping[str, str] = {
    "repo:github.com/smithery-ai/mcp-servers#notion": "notion",
    "repo:github.com/smithery-ai/mcp-servers#github": "github",
    "repo:github.com/pipeworx-io/mcp-notion_connect#": "notion",
    "repo:github.com/pipeworx-io/mcp-github_private#": "github",
    "repo:github.com/n24q02m/better-notion-mcp#": "notion",
}

# GitHub 301 verified 2026-09-08: https://github.com/Patrax/review-router-setup
# redirects to https://github.com/tenpace-app/review-router-setup. This does not
# equate arbitrary api.tenpace.com paths or other repositories/subpaths.
_REPOSITORY_TRANSFERS: Mapping[str, str] = {
    "repo:github.com/patrax/review-router-setup#": (
        "repo:github.com/tenpace-app/review-router-setup#"
    ),
}

# Reviewed connection wrappers in the published catalog. These are alternate
# ways to control Supabase itself, not products which happen to use Supabase.
# Keep this bounded and explicit: "ticketing", "health", and "supervisor"
# must never disappear just because their names contain the provider.
_BUILTIN_ALIASES: Mapping[str, tuple[str, ...]] = {
    "supabase": (
        "Supabase Admin",
        "Supabase Admin Self-Hosted",
        "supabase-mcp-selfhosted",
        "supabase-mcp-cloud-and-selfhosted",
        "selfhosted-supabase",
        "selfhosted-supabase-mcp",
        "supabase-mcp-lite",
        "supabase-godmode",
        "supabase-godmode-v2",
    ),
}


def _name(value: str) -> str:
    words = _WORDS.sub(
        " ", unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    ).split()
    while words and words[0] in _DECORATIONS:
        words.pop(0)
    while words and words[-1] in _DECORATIONS:
        words.pop()
    return "".join(words)


def _name_variants(item: AppIdentity) -> set[str]:
    variants = {_name(item.name), _name(item.slug)}
    package = _SCOPED_PACKAGE.fullmatch(unicodedata.normalize("NFKC", item.name).strip())
    if package:
        # Package scopes are publishers. Unwrap an MCP wrapper or a reviewed
        # alias only; @runtime/supabase may be a runtime adapter, not an app.
        basename = package.group(1)
        aliases = {_name(alias) for values in _BUILTIN_ALIASES.values() for alias in values}
        if "mcp" in _WORDS.sub(" ", basename.casefold()).split() or _name(basename) in aliases:
            variants.add(_name(basename))
    raw_repo = item.metadata.get("repo")
    if isinstance(raw_repo, dict) and not _string(raw_repo.get("subpath")):
        owner, repo = (_string(raw_repo.get(key)) for key in ("owner", "repo"))
        # Registry-generated titles sometimes prepend the publisher. Strip
        # it only when the complete title agrees with the structured repo.
        if owner and repo and item.name.casefold() == f"{owner}-{repo}".casefold():
            variants.add(_name(repo))
    return variants - {""}


def _url(value: str) -> str:
    """Normalize spelling only; keep path case, query, and scheme significant."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password or "{" in value or "}" in value:
            return ""
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if parsed.port and (parsed.scheme, parsed.port) not in {("https", 443), ("http", 80)}:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), parsed.query, ""))
    except ValueError:
        return ""


def _string(value: object) -> str:
    return value.strip()[:512] if isinstance(value, str) else ""


def _repo(raw: object, *, canonical: bool = True) -> tuple[str, str]:
    if not isinstance(raw, dict):
        return "", ""
    host, owner, name = (_string(raw.get(key)).casefold() for key in ("host", "owner", "repo"))
    path = _string(raw.get("subpath")).strip("/")
    if not host or not owner or not name or any("/" in part for part in (host, owner, name)):
        return "", ""
    name = name.removesuffix(".git")
    token = f"repo:{host}/{owner}/{name}#{path}"
    return (
        _REPOSITORY_TRANSFERS.get(token, token) if canonical else token,
        f"repo-owner:{host}/{owner}#{path}",
    )


def _repo_url(value: str) -> str:
    """Only a repository root is evidence; a GitHub host or docs path is not."""
    normalized = _url(value)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname not in {"github.com", "gitlab.com"} or len(parts) != 2:
        return ""
    token, _ = _repo({"host": parsed.hostname, "owner": parts[0], "repo": parts[1]})
    return token


def _smithery_token(value: str) -> str:
    return f"smithery:{value.casefold()}" if _SMITHERY_QUALIFIED.fullmatch(value) else ""


def _endpoint_tokens(value: str) -> set[str]:
    normalized = _url(value)
    if not normalized:
        return set()
    tokens = {f"endpoint:{normalized}"}
    parsed = urlsplit(normalized)
    # The official registry publishes a hosted endpoint while Smithery may
    # publish only its qualified name. Both encode the same publisher/server
    # pair. Never equate a whole hosting domain or a tenant-specific query.
    if parsed.netloc == "server.smithery.ai" and parsed.scheme == "https" and not parsed.query:
        path = parsed.path
        if path.startswith("/@") and path.endswith("/mcp"):
            token = _smithery_token(path[2:-4])
            if token:
                tokens.add(token)
    return tokens


@dataclass(frozen=True)
class AppIdentity:
    key: str
    slug: str
    name: str
    endpoint: str = ""
    docs_url: str = ""
    homepage: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    trust_rank: int = 0


def _tokens(item: AppIdentity) -> set[str]:
    tokens = _endpoint_tokens(item.endpoint)
    qualified = _smithery_token(_string(item.metadata.get("smithery_qualified_name")))
    if qualified:
        tokens.add(qualified)
    remotes = item.metadata.get("remotes")
    if isinstance(remotes, list):
        for remote in remotes[:_MAX_ITEMS]:
            if isinstance(remote, dict) and remote.get("templated") is not True:
                tokens.update(_endpoint_tokens(_string(remote.get("url"))))
    repo, provider = _repo(item.metadata.get("repo"))
    if repo:
        tokens.add(repo)
    else:
        # A monorepo subpath must never inherit the root identity through its
        # homepage. Root URL fallback is only for records without repo data.
        for value in (item.docs_url, item.homepage):
            root = _repo_url(value)
            if root:
                tokens.add(root)
    name = _name(item.name)
    if provider and name:
        tokens.add(f"provider-name:{provider}:{name}")
    packages = item.metadata.get("packages")
    if isinstance(packages, list):
        for package in packages[:_MAX_ITEMS]:
            if isinstance(package, dict):
                registry = _string(package.get("registry_type")).casefold()
                identifier = _string(package.get("identifier"))
                if registry and identifier:
                    tokens.add(f"package:{registry}:{identifier}")
    npm = _string(item.metadata.get("npm_package"))
    if npm:
        tokens.add(f"package:npm:{npm}")
    return tokens


def duplicate_app_keys(
    builtins: Sequence[AppIdentity], entries: Sequence[AppIdentity]
) -> tuple[str, ...]:
    """Keys hidden from browsing, with entries supplied in winner order.

    Built-ins always win. Reviewed repository transfer destinations win over
    legacy records within the same trust tier; other ties retain the caller's
    stable trust/popularity/key order, independently of search or filter.
    Shared name alone is sufficient only against a Jhin app; community names
    require the same provider, endpoint, package, or full repository/subpath.
    A union preserves aliases across registries even when a third record links
    two otherwise separate identities. No token grants a native connector or
    changes a stored risk floor.
    """
    all_items = [*builtins, *entries]
    parents = list(range(len(all_items)))
    destinations = frozenset(_REPOSITORY_TRANSFERS.values())
    priorities = [
        (0, 0, 0, index)
        if index < len(builtins)
        else (
            1,
            item.trust_rank,
            0 if _repo(item.metadata.get("repo"), canonical=False)[0] in destinations else 1,
            index,
        )
        for index, item in enumerate(all_items)
    ]

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    names = {_name(value): _name(value) for item in builtins for value in (item.name, item.slug)}
    names.pop("", None)
    for item in builtins:
        for alias in _BUILTIN_ALIASES.get(item.slug, ()):
            names[_name(alias)] = _name(item.slug)
    reserved = frozenset(item.slug for item in builtins)
    seen: dict[str, int] = {}
    for index, item in enumerate(all_items):
        tokens = _tokens(item)
        for token in tuple(tokens):
            builtin = _BUILTIN_REPOSITORIES.get(token)
            if builtin in reserved:
                tokens.add(f"builtin-name:{_name(builtin)}")
        for name in _name_variants(item):
            if name in names:
                tokens.add(f"builtin-name:{names[name]}")
        if index >= len(builtins):
            for slug in reserved:
                if item.slug.startswith(f"{slug[:25]}_") and item.slug == safe_slug(
                    slug, item.key, reserved
                ):
                    tokens.add(f"builtin-name:{_name(slug)}")
        for token in sorted(tokens):
            other = seen.setdefault(token, index)
            first, second = root(index), root(other)
            winner, loser = sorted((first, second), key=priorities.__getitem__)
            parents[loser] = winner

    return tuple(
        item.key
        for index, item in enumerate(all_items)
        if index >= len(builtins) and root(index) != index
    )
