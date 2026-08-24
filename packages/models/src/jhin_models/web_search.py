"""Optional model-native web search (docs/architecture/web.md, path 2).

Some providers can run a web search *inside* the model call: OpenAI's
``web_search_options`` on chat completions (search-preview models),
OpenRouter's ``web`` plugin, and Anthropic's server-side ``web_search`` tool
on the Messages API. This never touches Jhin's tool gateway — no
``tool_call`` rows, no grants — so it is a per-profile opt-in stored as
``config_json.web_search`` and validated against the provider before it is
saved (like :mod:`jhin_models.embeddings` and :mod:`jhin_models.images`).

Adapters that support the flag translate it to their wire format; adapters
that do not raise :class:`~jhin_models.base.ModelProviderError` so a stale
profile fails loudly instead of silently dropping the feature.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

WEB_SEARCH_CONFIG_KEY = "web_search"
MAX_WEB_SEARCH_USES = 10
# Providers whose chat adapter can run a model-native web search.
WEB_SEARCH_PROVIDERS = ("openai", "openrouter", "anthropic")


class WebSearchConfig(BaseModel):
    """Per-profile ``config_json.web_search`` block."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    enabled: bool = False
    # Upper bound on provider-side searches per request where the provider
    # supports one (Anthropic ``max_uses``, OpenRouter ``max_results``).
    max_uses: int | None = Field(default=None, ge=1, le=MAX_WEB_SEARCH_USES)

    @classmethod
    def from_profile_config(cls, config_json: dict[str, Any] | None) -> WebSearchConfig:
        raw = (config_json or {}).get(WEB_SEARCH_CONFIG_KEY)
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValueError:
            return cls()


def web_search_unsupported_reason(provider_type: str, model_name: str) -> str | None:
    """Why ``config_json.web_search`` cannot be enabled on this profile.

    Returns ``None`` when the provider/model pair supports model-native web
    search, otherwise a human-readable rejection for profile validation.
    """
    provider = provider_type.strip().lower()
    model = model_name.strip().lower()
    if provider == "anthropic":
        return None
    if provider == "openrouter":
        return None
    if provider == "openai":
        # Chat completions accept ``web_search_options`` only on the dedicated
        # search models: the (deprecated but still listed) ``*-search-preview``
        # family and its successor ``gpt-5-search-api``.
        if "search-preview" in model or "search-api" in model:
            return None
        return (
            "OpenAI chat completions only support built-in web search on the "
            "dedicated search models (e.g. gpt-5-search-api or "
            "gpt-4o-mini-search-preview); pick one of those models or disable web search"
        )
    return (
        f"the {provider_type} provider has no model-native web search; "
        "grant the agent the web.search connector tool instead"
    )


class WebCitation(BaseModel):
    """One provider-reported source behind a web-searched answer."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""


def render_citations(citations: list[WebCitation]) -> str:
    """A visible, labeled sources block appended to the reply text."""
    if not citations:
        return ""
    seen: set[str] = set()
    lines: list[str] = []
    for citation in citations:
        if not citation.url or citation.url in seen:
            continue
        seen.add(citation.url)
        title = citation.title.strip()
        lines.append(f"- {title} — {citation.url}" if title else f"- {citation.url}")
    if not lines:
        return ""
    return "\n\nSources (provider web search):\n" + "\n".join(lines)


__all__ = [
    "MAX_WEB_SEARCH_USES",
    "WEB_SEARCH_CONFIG_KEY",
    "WEB_SEARCH_PROVIDERS",
    "WebCitation",
    "WebSearchConfig",
    "render_citations",
    "web_search_unsupported_reason",
]
