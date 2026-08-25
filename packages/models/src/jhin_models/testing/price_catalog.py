"""A trimmed sample of LiteLLM's community price map, in its real shape.

Taken from a live fetch of ``model_prices_and_context_window.json`` (verified
2026-08-24: ~1.8 MB, 3,176 top-level keys) and cut down to the entries that
actually break naive parsers — the ``sample_spec`` documentation entry, the
newer ``fallback_generalizations`` routing block, a provider-prefixed key, an
embedding entry whose output price is ``0.0``, a non-token billing mode, and
entries missing the optional fields.

It lives beside :mod:`jhin_models.testing.fake_openai` so both the models
package and the API can build the same fixture without either copying the
external format into its own tests.

Derived from the LiteLLM model price map, MIT License, Copyright (c) 2023
Berri AI (https://github.com/BerriAI/litellm). The live map is fetched at run
time rather than vendored; this is a handful of entries kept only to pin the
document's shape in tests.
"""

from __future__ import annotations

from typing import Any

SAMPLE_LITELLM_PRICE_MAP: dict[str, Any] = {
    "sample_spec": {
        "input_cost_per_token": 0.0,
        "litellm_provider": "one of https://docs.litellm.ai/docs/providers",
        "max_input_tokens": "max input tokens, if the provider specifies it.",
        "max_tokens": "LEGACY parameter.",
        "mode": "one of: chat, embedding, completion, image_generation",
        "output_cost_per_token": 0.0,
    },
    "fallback_generalizations": {
        "rules": [{"name": "bedrock-claude-ids", "pattern": "^(?:[a-z-]+\\.)?anthropic\\.claude-"}]
    },
    "gpt-4o": {
        "cache_read_input_token_cost": 1.25e-06,
        "input_cost_per_token": 2.5e-06,
        "litellm_provider": "openai",
        "max_input_tokens": 128000,
        "max_output_tokens": 16384,
        "max_tokens": 16384,
        "mode": "chat",
        "output_cost_per_token": 1e-05,
        "supports_vision": True,
    },
    "gpt-4o-2024-05-13": {
        "input_cost_per_token": 5e-06,
        "litellm_provider": "openai",
        "max_input_tokens": 128000,
        "mode": "chat",
        "output_cost_per_token": 1.5e-05,
    },
    "gpt-5.6-terra": {
        "input_cost_per_token": 2e-06,
        "litellm_provider": "openai",
        "max_input_tokens": 922000,
        "mode": "chat",
        "output_cost_per_token": 1.2e-05,
    },
    "claude-sonnet-4-20250514": {
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token": 3e-06,
        "litellm_provider": "anthropic",
        "max_input_tokens": 1000000,
        "mode": "chat",
        "output_cost_per_token": 1.5e-05,
    },
    "text-embedding-3-small": {
        "input_cost_per_token": 2e-08,
        "litellm_provider": "openai",
        "max_input_tokens": 8191,
        "max_tokens": 8191,
        "mode": "embedding",
        "output_cost_per_token": 0.0,
    },
    "openrouter/anthropic/claude-3.5-sonnet": {
        "input_cost_per_token": 3e-06,
        "litellm_provider": "openrouter",
        "max_input_tokens": 200000,
        "mode": "chat",
        "output_cost_per_token": 1.5e-05,
    },
    "dall-e-3": {
        "input_cost_per_pixel": 0.0,
        "litellm_provider": "openai",
        "mode": "image_generation",
    },
    "gemini-2.5-pro": {
        "input_cost_per_token": 1.25e-06,
        "litellm_provider": "gemini",
        "mode": "chat",
        "output_cost_per_token": 1e-05,
    },
    "openai/container": {"litellm_provider": "openai", "mode": "chat"},
    "broken-entry": "not an object",
}

__all__ = ["SAMPLE_LITELLM_PRICE_MAP"]
