/** Unit tests: pure Models-page helpers (lib/models.ts). */

import { describe, expect, it } from "vitest";
import {
  autofillForModel,
  balanceSourceLabel,
  buildProfileConfig,
  capabilitySummary,
  costTier,
  costTierLabel,
  dollarInputToMicros,
  formatMicrosAsDollars,
  formatModelSize,
  isInsufficientFunds,
  isModelIncompatibleRequest,
  microsToDollarInput,
  OLLAMA_KEEP_ALIVE_OPTIONS,
  ollamaDisplayName,
  ollamaNamePath,
  profilePrefillForOllamaModel,
  summarizeBudget,
  webSearchSupport,
} from "@/lib/models";
import type { ModelProfile, OllamaModel, ProviderModelEntry } from "@/lib/types";

const ENTRIES: ProviderModelEntry[] = [
  {
    id: "gpt-4o",
    input_cost_micros_per_million: 2_500_000,
    output_cost_micros_per_million: 10_000_000,
    context_window: 128_000,
    source: "catalog",
  },
  {
    id: "openai/gpt-4o-mini",
    input_cost_micros_per_million: 150_000,
    output_cost_micros_per_million: 600_000,
    context_window: null,
    source: "provider",
  },
  {
    id: "mystery",
    input_cost_micros_per_million: null,
    output_cost_micros_per_million: null,
    context_window: 32_000,
    source: null,
  },
];

describe("autofillForModel", () => {
  it("fills catalog prices and names the catalog date", () => {
    const fill = autofillForModel("GPT-4O", ENTRIES, "openai", "2026-01");
    expect(fill.known).toBe(true);
    expect(fill.inputCost).toBe("2.5");
    expect(fill.outputCost).toBe("10");
    expect(fill.contextWindow).toBe("128000");
    expect(fill.source).toBe("catalog");
    expect(fill.note).toBe(
      "Prices from OpenAI's public list (catalog updated 2026-01) — edit if your contract differs.",
    );
  });

  it("labels live provider prices", () => {
    const fill = autofillForModel("openai/gpt-4o-mini", ENTRIES, "openrouter", null);
    expect(fill.known).toBe(true);
    expect(fill.inputCost).toBe("0.15");
    expect(fill.contextWindow).toBe("");
    expect(fill.note).toBe("Live from OpenRouter — edit if your contract differs.");
  });

  it("asks for manual prices when the model is unknown or unpriced", () => {
    const unpriced = autofillForModel("mystery", ENTRIES, "anthropic", "2026-01");
    expect(unpriced.known).toBe(false);
    expect(unpriced.inputCost).toBe("");
    expect(unpriced.contextWindow).toBe("32000");
    expect(unpriced.note).toMatch(/enter the prices from Anthropic's pricing page/);

    const unknown = autofillForModel("nope", ENTRIES, "openai", "2026-01");
    expect(unknown.known).toBe(false);
    expect(unknown.contextWindow).toBe("");
    expect(autofillForModel("", ENTRIES, "openai", null).note).toBe("Pick a model to auto-fill its prices.");
    expect(autofillForModel("gpt-4o", undefined, "openai", null).known).toBe(false);
  });

  it("prices a local Ollama model at $0 instead of asking for a pricing page", () => {
    const local: ProviderModelEntry[] = [
      {
        id: "qwen3.8:latest",
        input_cost_micros_per_million: null,
        output_cost_micros_per_million: null,
        context_window: 40_960,
        source: null,
      },
    ];
    const fill = autofillForModel("qwen3.8:latest", local, "ollama", null);
    expect(fill.known).toBe(true);
    expect(fill.inputCost).toBe("0");
    expect(fill.outputCost).toBe("0");
    expect(fill.contextWindow).toBe("40960");
    expect(fill.source).toBeNull();
    expect(fill.note).toBe("Runs on your Ollama host — no per-token price.");
    // Even a model the host did not list is free to run there.
    expect(autofillForModel("gemma4:31b", undefined, "ollama", null)).toMatchObject({
      known: true,
      inputCost: "0",
      outputCost: "0",
      contextWindow: "",
    });
  });
});

describe("formatModelSize", () => {
  it("quotes decimal gigabytes to one place and whole megabytes below one", () => {
    expect(formatModelSize(17_700_000_000)).toBe("17.7 GB");
    expect(formatModelSize(16_757_083_340)).toBe("16.8 GB");
    expect(formatModelSize(1_000_000_000)).toBe("1.0 GB");
    expect(formatModelSize(536_870_912)).toBe("537 MB");
    expect(formatModelSize(0)).toBe("0 MB");
  });
});

describe("ollamaDisplayName", () => {
  it("drops only the :latest tag", () => {
    expect(ollamaDisplayName("qwen3.8:latest")).toBe("qwen3.8");
    expect(ollamaDisplayName("gemma4:31b")).toBe("gemma4:31b");
    expect(ollamaDisplayName("tripolskypetr/qwen3.6-uncensored-aggressive:latest")).toBe(
      "tripolskypetr/qwen3.6-uncensored-aggressive",
    );
  });
});

describe("ollamaNamePath", () => {
  it("keeps slashes as path separators and encodes colons", () => {
    expect(ollamaNamePath("qwen3.8:latest")).toBe("qwen3.8%3Alatest");
    expect(ollamaNamePath("tripolskypetr/qwen3.6-uncensored-aggressive:latest")).toBe(
      "tripolskypetr/qwen3.6-uncensored-aggressive%3Alatest",
    );
  });
});

describe("OLLAMA_KEEP_ALIVE_OPTIONS", () => {
  it("offers a short lease, a long one, and forever — never the unload sentinel", () => {
    expect(OLLAMA_KEEP_ALIVE_OPTIONS.map((option) => option.value)).toEqual(["5m", "1h", "-1"]);
    expect(OLLAMA_KEEP_ALIVE_OPTIONS.map((option) => option.label)).toEqual([
      "5 minutes",
      "1 hour",
      "Until unloaded",
    ]);
  });
});

describe("profilePrefillForOllamaModel", () => {
  it("prices the profile at $0 and carries the host's context length", () => {
    const model: OllamaModel = {
      name: "qwen3.8:latest",
      size_bytes: 17_700_000_000,
      family: "qwen3",
      parameter_size: "27.3B",
      quantization: "Q4_K_M",
      modified_at: "2026-08-30T12:00:00Z",
      context_length: 40_960,
      capabilities: ["completion", "tools", "thinking"],
      loaded: false,
      size_vram_bytes: null,
      expires_at: null,
      keeps_loaded: false,
    };
    expect(profilePrefillForOllamaModel("prov-1", model)).toEqual({
      providerId: "prov-1",
      modelName: "qwen3.8:latest",
      displayName: "qwen3.8",
      contextWindow: 40_960,
      inputCostMicros: 0,
      outputCostMicros: 0,
    });
    expect(profilePrefillForOllamaModel("prov-1", { ...model, context_length: null }).contextWindow).toBeNull();
  });
});

describe("dollar/micro conversion", () => {
  it("round-trips", () => {
    expect(dollarInputToMicros("2.5")).toBe(2_500_000);
    expect(dollarInputToMicros(" 0.15 ")).toBe(150_000);
    expect(dollarInputToMicros("")).toBeNull();
    expect(dollarInputToMicros("abc")).toBeNull();
    expect(dollarInputToMicros("-1")).toBeNull();
    expect(microsToDollarInput(150_000)).toBe("0.15");
    expect(microsToDollarInput(null)).toBe("");
  });
});

describe("formatMicrosAsDollars", () => {
  it("formats cents, tiny amounts, negatives and unknowns", () => {
    expect(formatMicrosAsDollars(37_500_000)).toBe("$37.50");
    expect(formatMicrosAsDollars(0)).toBe("$0.00");
    expect(formatMicrosAsDollars(1_234)).toBe("$0.00123");
    expect(formatMicrosAsDollars(-2_000_000)).toBe("-$2.00");
    expect(formatMicrosAsDollars(null)).toBe("—");
  });
});

describe("balanceSourceLabel", () => {
  it("explains each source", () => {
    expect(balanceSourceLabel("openrouter")).toBe("Live from OpenRouter");
    expect(balanceSourceLabel("openai_admin")).toBe("From OpenAI's admin API (month to date)");
    expect(balanceSourceLabel("tracked")).toBe("Tracked by Jhin");
  });
});

describe("summarizeBudget", () => {
  it("is null without a budget", () => {
    expect(summarizeBudget(5, null)).toBeNull();
    expect(summarizeBudget(5, 0)).toBeNull();
  });

  it("tones by threshold and caps the bar", () => {
    expect(summarizeBudget(10_000_000, 100_000_000)).toMatchObject({ tone: "ok", percent: 10, ratio: 0.1 });
    expect(summarizeBudget(85_000_000, 100_000_000)).toMatchObject({ tone: "warn", percent: 85 });
    expect(summarizeBudget(50_000_000, 100_000_000, 0.5)).toMatchObject({ tone: "warn" });
    const over = summarizeBudget(150_000_000, 100_000_000);
    expect(over).toMatchObject({ tone: "over", ratio: 1, percent: 150 });
    expect(over?.label).toBe("Over budget: $150.00 of $100.00");
    expect(summarizeBudget(10_000_000, 100_000_000)?.label).toBe("$10.00 of $100.00 (10%)");
  });
});

describe("isInsufficientFunds", () => {
  it("matches only the out-of-credit code", () => {
    expect(isInsufficientFunds({ error_code: "insufficient_funds" })).toBe(true);
    expect(isInsufficientFunds({ error_code: "step_failed" })).toBe(false);
    expect(isInsufficientFunds(null)).toBe(false);
  });
});

describe("isModelIncompatibleRequest", () => {
  it("matches only the incompatible-request code", () => {
    expect(isModelIncompatibleRequest({ error_code: "model_incompatible_request" })).toBe(true);
    expect(isModelIncompatibleRequest({ error_code: "insufficient_funds" })).toBe(false);
    expect(isModelIncompatibleRequest(null)).toBe(false);
  });
});

describe("webSearchSupport", () => {
  it("supports anthropic and openrouter for any model", () => {
    expect(webSearchSupport("anthropic", "claude-x")).toEqual({ supported: true, reason: null });
    expect(webSearchSupport("openrouter", "meta/llama")).toEqual({ supported: true, reason: null });
  });

  it("gates openai on its dedicated search models", () => {
    expect(webSearchSupport("openai", "gpt-4o-mini-search-preview").supported).toBe(true);
    expect(webSearchSupport("openai", "gpt-5-search-api").supported).toBe(true);
    const denied = webSearchSupport("openai", "gpt-4o-mini");
    expect(denied.supported).toBe(false);
    expect(denied.reason).toContain("search");
  });

  it("rejects providers without built-in search", () => {
    expect(webSearchSupport("ollama", "llama3").supported).toBe(false);
    expect(webSearchSupport("openai_compatible", "fake-mini").supported).toBe(false);
  });
});

function modelProfile(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "p1",
    workspace_id: "w1",
    provider_id: "prov-1",
    model_name: "gpt-5-mini",
    display_name: "GPT-5 mini",
    context_window: null,
    input_cost_micros_per_million: null,
    output_cost_micros_per_million: null,
    price_source: null,
    supports_tools: true,
    supports_reasoning: false,
    config_json: {},
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("costTier", () => {
  it("buckets combined dollars per 1M tokens into three tiers", () => {
    expect(costTier(150_000, 600_000)).toBe(1); // $0.75 combined
    expect(costTier(2_500_000, 500_000)).toBe(1); // exactly $3.00
    expect(costTier(2_500_000, 10_000_000)).toBe(2); // $12.50
    expect(costTier(5_000_000, 10_000_000)).toBe(2); // exactly $15.00
    expect(costTier(15_000_000, 75_000_000)).toBe(3); // $90.00
  });

  it("treats a missing half as $0 but no price at all as unknown", () => {
    expect(costTier(null, null)).toBeNull();
    expect(costTier(2_000_000, null)).toBe(1);
    expect(costTier(null, 20_000_000)).toBe(3);
  });
});

describe("costTierLabel", () => {
  it("names each tier", () => {
    expect(costTierLabel(1)).toBe("Inexpensive");
    expect(costTierLabel(2)).toBe("Moderate");
    expect(costTierLabel(3)).toBe("Premium");
  });
});

describe("capabilitySummary", () => {
  it("grades the context window in plain language", () => {
    expect(capabilitySummary(modelProfile({ context_window: 50_000 }))).toBe(
      "Good for everyday tasks",
    );
    expect(capabilitySummary(modelProfile({ context_window: 100_000 }))).toBe(
      "Handles long documents",
    );
    expect(capabilitySummary(modelProfile({ context_window: 400_000 }))).toBe(
      "Reads very long documents",
    );
    // An unknown window earns no claim: the card says less, not the same
    // sentence on every model.
    expect(capabilitySummary(modelProfile({ context_window: null }))).toBe("");
  });

  it("appends the web-search line only when it is enabled", () => {
    expect(
      capabilitySummary(
        modelProfile({ context_window: 200_000, config_json: { web_search: { enabled: true } } }),
      ),
    ).toBe("Handles long documents · Can search the web");
    expect(
      capabilitySummary(
        modelProfile({ context_window: 200_000, config_json: { web_search: { enabled: false } } }),
      ),
    ).toBe("Handles long documents");
  });
});

describe("buildProfileConfig", () => {
  it("preserves unrelated keys and toggles web_search", () => {
    const existing = { embeddings: { enabled: true }, web_search: { enabled: true } };
    expect(buildProfileConfig(existing, true)).toEqual({
      embeddings: { enabled: true },
      web_search: { enabled: true },
    });
    expect(buildProfileConfig(existing, false)).toEqual({ embeddings: { enabled: true } });
    expect(buildProfileConfig(undefined, true)).toEqual({ web_search: { enabled: true } });
  });
});
