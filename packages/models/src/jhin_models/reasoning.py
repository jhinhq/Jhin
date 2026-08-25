"""Reasoning-effort control for OpenAI-family chat completions.

OpenAI's reasoning models (the ``o`` series and ``gpt-5`` and later) apply a
non-``none`` ``reasoning_effort`` by default, and ``POST /v1/chat/completions``
**refuses** to combine that default with function tools::

    Function tools with reasoning_effort are not supported for gpt-5.6-terra
    in /v1/chat/completions. To use function tools, use /v1/responses or set
    reasoning_effort to 'none'.

Jhin advertises the agent's granted tools on essentially every step, so a
reasoning model is unusable unless the request pins ``reasoning_effort`` to
``"none"``. The adapters do that automatically; :class:`ReasoningConfig` is
the per-profile override (``config_json.reasoning``) for the cases where a
higher effort is wanted deliberately — a tools-free agent, say.

The rule, in one place:

* the effort is only ever sent to OpenAI-family adapters that speak the
  chat-completions wire format (``openai``, ``openai_compatible``); OpenRouter
  normalizes reasoning parameters itself and gets its own native translation;
* an explicit ``config_json.reasoning.effort`` always wins;
* otherwise ``"none"`` is sent only when the request carries tools *and* the
  model is reasoning-class — never on a model where the parameter is
  meaningless, and never on a tools-free request (there is no conflict to
  solve there, and OpenAI's own default is the better answer).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

REASONING_CONFIG_KEY = "reasoning"

# Effort the profile may pin. ``"minimal"`` is deliberately absent: OpenAI
# accepted it on the first gpt-5 generation and current reasoning models
# reject it ("Supported values are: 'none', 'low', 'medium', 'high'"), so
# allowing it would only let profiles save a value that 400s at run time.
ReasoningEffort = Literal["none", "low", "medium", "high"]
REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high")
NO_REASONING = "none"

# Providers whose adapter understands a reasoning effort at all. Anthropic
# (extended thinking) and Ollama (local models) do not take this parameter.
REASONING_PROVIDERS = ("openai", "openai_compatible", "openrouter")

# Reasoning-class model names, matched by shape rather than by an exact list
# so dated and suffixed variants keep working:
#   o1, o1-mini, o1-preview, o3, o3-mini, o4-mini, o5-…
#   gpt-5, gpt-5-mini, gpt-5-2025-08-07, gpt-5.1-codex, gpt-5.6-terra, gpt-6-…
# ``gpt-4o``/``gpt-4o-mini``/``gpt-4.1`` are *not* reasoning models, and the
# ``gpt-5-chat*`` line is the non-reasoning chat variant of the gpt-5 family.
_REASONING_PATTERNS = (
    re.compile(r"^o[1-9]\d*(?:[-.].*)?$"),
    re.compile(r"^gpt-(?:[5-9]|\d{2,})(?:\.\d+)*(?:[-.].*)?$"),
)
_NON_REASONING_MARKERS = ("-chat",)


def normalize_model_name(model_name: str) -> str:
    """Bare model id: no vendor namespace, no variant suffix.

    OpenRouter names models ``openai/gpt-5-mini`` and can append a variant
    (``:online``, ``:free``); the matcher works on ``gpt-5-mini``.
    """
    name = model_name.strip().lower().rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def is_reasoning_model(model_name: str) -> bool:
    """Whether OpenAI applies a reasoning effort to this model by default."""
    name = normalize_model_name(model_name)
    if not name or any(marker in name for marker in _NON_REASONING_MARKERS):
        return False
    return any(pattern.match(name) for pattern in _REASONING_PATTERNS)


class ReasoningConfig(BaseModel):
    """Per-profile ``config_json.reasoning`` block.

    ``effort`` pins the value sent to the provider and overrides the automatic
    tool-compatibility behaviour. ``supports_reasoning`` is an escape hatch
    that forces reasoning-class treatment for a model name the matcher does
    not recognize; the profile's own ``supports_reasoning`` column is folded
    into it when the snapshot is built.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    effort: ReasoningEffort | None = None
    supports_reasoning: bool = False

    @property
    def is_set(self) -> bool:
        """Whether the profile said anything about reasoning at all."""
        return self.effort is not None or self.supports_reasoning

    @classmethod
    def from_profile_config(cls, config_json: dict[str, Any] | None) -> ReasoningConfig:
        raw = (config_json or {}).get(REASONING_CONFIG_KEY)
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValueError:
            return cls()


def request_is_reasoning_class(model_name: str, config: ReasoningConfig | None) -> bool:
    """Reasoning-class per the name matcher, or per the profile's override."""
    if config is not None and config.supports_reasoning:
        return True
    return is_reasoning_model(model_name)


def automatic_reasoning_effort(
    model_name: str, *, has_tools: bool, config: ReasoningConfig | None = None
) -> str | None:
    """``"none"`` when tools would otherwise collide with the model's default
    reasoning effort, else ``None`` (send no ``reasoning_effort`` at all)."""
    if has_tools and request_is_reasoning_class(model_name, config):
        return NO_REASONING
    return None


def reasoning_unsupported_reason(
    provider_type: str, model_name: str, *, supports_reasoning: bool = False
) -> str | None:
    """Why ``config_json.reasoning`` cannot be saved on this profile.

    ``None`` when the provider/model pair accepts a reasoning effort,
    otherwise a human-readable rejection for profile validation.
    """
    provider = provider_type.strip().lower()
    if provider not in REASONING_PROVIDERS:
        return (
            f"the {provider_type} provider does not accept a reasoning effort; "
            "remove config_json.reasoning"
        )
    if supports_reasoning or is_reasoning_model(model_name):
        return None
    return (
        f"'{model_name}' is not a reasoning model, so the provider rejects a "
        "reasoning effort; remove config_json.reasoning, or set the profile's "
        '"supports reasoning" flag if this model really is one'
    )


__all__ = [
    "NO_REASONING",
    "REASONING_CONFIG_KEY",
    "REASONING_EFFORTS",
    "REASONING_PROVIDERS",
    "ReasoningConfig",
    "ReasoningEffort",
    "automatic_reasoning_effort",
    "is_reasoning_model",
    "normalize_model_name",
    "reasoning_unsupported_reason",
    "request_is_reasoning_class",
]
