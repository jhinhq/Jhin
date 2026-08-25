/** Unit tests: pure Models-page helpers (lib/models.ts). */

import { describe, expect, it } from "vitest";
import {
  autofillForModel,
  balanceSourceLabel,
  buildProfileConfig,
  dollarInputToMicros,
  formatMicrosAsDollars,
  isInsufficientFunds,
  isModelIncompatibleRequest,
  microsToDollarInput,
  summarizeBudget,
  webSearchSupport,
} from "@/lib/models";
import type { ProviderModelEntry } from "@/lib/types";

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
