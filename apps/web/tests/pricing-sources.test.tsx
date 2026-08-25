/**
 * Unknown-price UI, catalog staleness, and honest spend totals.
 *
 * A model with no price does not fail loudly — it records every run as $0 and
 * quietly makes the spend total wrong. These tests hold the line that the UI
 * says so, offers the fix, and never presents a guessed number as a measured
 * one.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PricingPanel } from "@/components/pricing-panel";
import { SpendTile } from "@/components/spend-tile";
import { UnpricedModelNote } from "@/components/unpriced-model-note";
import {
  catalogStalenessNote,
  derivationLabel,
  formatPricePair,
  isCatalogStale,
  observedRateSummary,
  priceSourceBadge,
  priceSourceLabel,
  pricingPageUrl,
  untrackedSpendNote,
} from "@/lib/models";
import type {
  ObservedRate,
  PricingStatus,
  ProfilePricing,
  WorkspaceSpend,
} from "@/lib/types";

afterEach(cleanup);

function profile(overrides: Partial<ProfilePricing> = {}): ProfilePricing {
  return {
    profile_id: "p1",
    display_name: "Terra",
    model_name: "gpt-5.6-terra",
    provider_id: "prov-1",
    provider_type: "openai",
    input_cost_micros_per_million: null,
    output_cost_micros_per_million: null,
    price_source: null,
    price_source_label: "No price is known for this model",
    priced: false,
    pricing_page_url: "https://platform.openai.com/docs/pricing",
    runs_this_month: 3,
    suggestion: null,
    suggestion_label: null,
    observed: null,
    ...overrides,
  };
}

function status(overrides: Partial<PricingStatus> = {}): PricingStatus {
  return {
    catalog_updated: "2026-01",
    catalog_stale: false,
    refreshed_source: null,
    refreshed_fetched_at: null,
    refreshed_entry_count: 0,
    refreshed_attribution: null,
    refreshed_project_url: "https://github.com/BerriAI/litellm",
    profiles: [profile()],
    untracked: [
      { model_name: "gpt-5.6-terra", runs: 3, input_tokens: 100, output_tokens: 10 },
    ],
    untracked_runs: 3,
    reconcile_available: true,
    reconcile_detail: "1 provider(s) can report itemised spend",
    pricing_pages: { openai: "https://platform.openai.com/docs/pricing" },
    ...overrides,
  };
}

function spend(overrides: Partial<WorkspaceSpend> = {}): WorkspaceSpend {
  return {
    spent_month_micros: 12_500_000,
    spent_total_micros: 40_000_000,
    period_start: "2026-08-01T00:00:00Z",
    providers: [],
    monthly_budget_micros: null,
    warning_threshold: 0.8,
    fetched_at: "2026-08-24T00:00:00Z",
    untracked: [],
    untracked_runs: 0,
    ...overrides,
  };
}

const PANEL_PROPS = {
  isPending: false,
  isAdmin: true,
  onReconcile: () => {},
  onRefreshCatalog: () => {},
  reconciling: false,
  refreshing: false,
  reconcileResult: null,
  catalogResult: null,
  error: null,
};

// --- The unknown-price warning ---

describe("UnpricedModelNote", () => {
  it("says plainly that spend will not be tracked, and names the model", () => {
    render(<UnpricedModelNote modelName="gpt-5.6-terra" providerType="openai" />);
    expect(screen.getByText(/won't be tracked/)).toBeTruthy();
    expect(screen.getByText("gpt-5.6-terra")).toBeTruthy();
  });

  it("links the provider's real pricing page", () => {
    render(<UnpricedModelNote modelName="gpt-5.6-terra" providerType="openai" />);
    const link = screen.getByRole("link", { name: /OpenAI's pricing page/ });
    expect(link.getAttribute("href")).toBe("https://platform.openai.com/docs/pricing");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("counts the runs already recorded as $0, so the cost of ignoring it is visible", () => {
    render(<UnpricedModelNote modelName="gpt-5.6-terra" providerType="openai" runs={3} />);
    expect(screen.getByText(/3 runs have already been recorded as \$0\.00/)).toBeTruthy();
  });

  it("saves inline prices as micro-dollars per million tokens", () => {
    const onSave = vi.fn();
    render(
      <UnpricedModelNote modelName="gpt-5.6-terra" providerType="openai" onSave={onSave} />,
    );
    fireEvent.change(
      screen.getByLabelText("Input dollars per million tokens for gpt-5.6-terra"),
      { target: { value: "2.00" } },
    );
    fireEvent.change(
      screen.getByLabelText("Output dollars per million tokens for gpt-5.6-terra"),
      { target: { value: "12.00" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save prices" }));
    expect(onSave).toHaveBeenCalledWith(2_000_000, 12_000_000);
  });

  it("offers no fields to someone who cannot save them", () => {
    render(<UnpricedModelNote modelName="gpt-5.6-terra" providerType="openai" />);
    expect(screen.queryByRole("button", { name: "Save prices" })).toBeNull();
  });

  it("does not offer to save nothing", () => {
    render(<UnpricedModelNote modelName="x" providerType="openai" onSave={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Save prices" }).hasAttribute("disabled")).toBe(
      true,
    );
  });
});

// --- The pricing panel ---

describe("PricingPanel", () => {
  it("states the precedence order so a number's authority is legible", () => {
    render(<PricingPanel {...PANEL_PROPS} status={status()} />);
    expect(screen.getByText(/a price you entered/)).toBeTruthy();
    expect(screen.getByText(/can be stale or wrong, which is exactly why they rank last/)).toBeTruthy();
  });

  it("names every unpriced model rather than only counting them", () => {
    render(<PricingPanel {...PANEL_PROPS} status={status()} />);
    const summary = screen.getByTestId("pricing-unpriced-summary");
    expect(summary.textContent).toContain("gpt-5.6-terra");
    expect(summary.textContent).toContain("isn't tracked");
  });

  it("nudges when the built-in list prices are old", () => {
    render(<PricingPanel {...PANEL_PROPS} status={status({ catalog_stale: true })} />);
    expect(screen.getByTestId("catalog-staleness").textContent).toContain(
      "These are list prices from 2026-01",
    );
  });

  it("stays quiet about staleness when the catalog is current", () => {
    render(<PricingPanel {...PANEL_PROPS} status={status()} />);
    expect(screen.queryByTestId("catalog-staleness")).toBeNull();
  });

  it("credits LiteLLM with its MIT notice once a catalog is cached", () => {
    render(
      <PricingPanel
        {...PANEL_PROPS}
        status={status({
          refreshed_source: "litellm",
          refreshed_entry_count: 197,
          refreshed_fetched_at: "2026-08-24T09:00:00Z",
          refreshed_attribution:
            "LiteLLM model price map, MIT License, Copyright (c) 2023 Berri AI",
        })}
      />,
    );
    expect(screen.getByTestId("catalog-attribution").textContent).toBe(
      "LiteLLM model price map, MIT License, Copyright (c) 2023 Berri AI",
    );
    const link = screen.getAllByRole("link", { name: "LiteLLM" })[0];
    expect(link.getAttribute("href")).toBe("https://github.com/BerriAI/litellm");
    expect(screen.getByText(/197 prices, refreshed/)).toBeTruthy();
  });

  it("credits LiteLLM before any refresh has happened too", () => {
    render(<PricingPanel {...PANEL_PROPS} status={status()} />);
    expect(screen.getAllByRole("link", { name: "LiteLLM" })[0].getAttribute("href")).toBe(
      "https://github.com/BerriAI/litellm",
    );
  });

  it("explains why measuring is unavailable instead of offering a dead button", () => {
    render(
      <PricingPanel
        {...PANEL_PROPS}
        status={status({
          reconcile_available: false,
          reconcile_detail: "Measuring real rates needs an OpenAI provider with an admin key",
        })}
      />,
    );
    const button = screen.getByRole("button", { name: /Measure real rates/ });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/needs an OpenAI provider with an admin key/)).toBeTruthy();
  });

  it("hides both admin actions from a viewer", () => {
    render(<PricingPanel {...PANEL_PROPS} isAdmin={false} status={status()} />);
    expect(screen.queryByRole("button", { name: /Measure real rates/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Refresh price catalog/ })).toBeNull();
  });

  it("shows a blended measured rate as one number, never as a split", () => {
    render(
      <PricingPanel
        {...PANEL_PROPS}
        status={status()}
        reconcileResult={{
          providers: [
            {
              provider_id: "prov-1",
              display_name: "OpenAI",
              provider_type: "openai",
              derived: [
                {
                  model_key: "gpt-5.6-terra",
                  derivation: "blended",
                  confidence: "medium",
                  note: "no public list price exists to split input from output",
                  input_micros_per_million: null,
                  output_micros_per_million: null,
                  blended_micros_per_million: 2_000_000,
                  input_tokens: 1_000_000,
                  output_tokens: 100_000,
                  runs: 25,
                  cost_micros: 2_200_000,
                },
              ],
              skipped: [{ model_key: "o3", reason: "only 2 run(s) in the period" }],
              applied: [],
              period_start: "2026-07-25T00:00:00Z",
              period_end: "2026-08-24T00:00:00Z",
              billed_micros: 2_200_000,
              unattributed_micros: 0,
              unattributed_labels: [],
              detail: "Measured 1 rate(s)",
            },
          ],
          skipped_providers: [],
          computed_at: "2026-08-24T10:00:00Z",
          detail: "Measured 1 model rate(s) from real spend",
        }}
      />,
    );
    const result = screen.getByTestId("reconcile-result");
    expect(result.textContent).toContain("$2.00 per 1M tokens (blended)");
    expect(result.textContent).not.toContain(" in · ");
    expect(result.textContent).toContain("only 2 run(s) in the period");
  });

  it("reports a failed catalog refresh without hiding it", () => {
    render(
      <PricingPanel
        {...PANEL_PROPS}
        status={status()}
        catalogResult={{
          updated: false,
          entry_count: 0,
          fetched_at: null,
          source: "litellm",
          source_url: "https://example.invalid",
          attribution: "LiteLLM model price map, MIT License, Copyright (c) 2023 Berri AI",
          detail: "Could not refresh the price catalog (network error) — still using the built-in price list",
          repriced: [],
        }}
      />,
    );
    expect(screen.getByTestId("catalog-refresh-result").textContent).toContain(
      "still using the built-in price list",
    );
  });
});

// --- Honest spend ---

describe("SpendTile untracked note", () => {
  it("says which runs are missing from the total", () => {
    render(
      <SpendTile
        spend={spend({
          untracked: [
            { model_name: "gpt-5.6-terra", runs: 3, input_tokens: 0, output_tokens: 0 },
          ],
          untracked_runs: 3,
        })}
      />,
    );
    expect(screen.getByTestId("spend-untracked").textContent).toBe(
      "3 runs on gpt-5.6-terra aren't included — no price set.",
    );
  });

  it("stays silent when everything is priced", () => {
    render(<SpendTile spend={spend()} />);
    expect(screen.queryByTestId("spend-untracked")).toBeNull();
  });
});

// --- Pure helpers ---

describe("pricing helpers", () => {
  it("treats a catalog older than six months as stale", () => {
    const now = new Date("2026-08-24T00:00:00Z");
    expect(isCatalogStale("2026-02", now)).toBe(false);
    expect(isCatalogStale("2026-01", now)).toBe(true);
    expect(isCatalogStale("2026-08", now)).toBe(false);
  });

  it("treats an unknown or malformed stamp as stale", () => {
    expect(isCatalogStale(null)).toBe(true);
    expect(isCatalogStale("")).toBe(true);
    expect(isCatalogStale("whenever")).toBe(true);
    expect(isCatalogStale("2026-13")).toBe(true);
  });

  it("returns no staleness note for a fresh catalog", () => {
    const fresh = new Date().toISOString().slice(0, 7);
    expect(catalogStalenessNote(fresh)).toBeNull();
  });

  it("labels each price source distinctly", () => {
    expect(priceSourceLabel("user")).toBe("Entered by an admin in this workspace");
    expect(priceSourceLabel("observed")).toContain("actual provider spend");
    expect(priceSourceLabel(null)).toBe("No price set");
    expect(priceSourceBadge("observed")).toEqual({ text: "Measured", tone: "ok" });
    expect(priceSourceBadge(null)).toEqual({ text: "No price", tone: "warn" });
  });

  it("distinguishes an unpriced row from one whose source was never recorded", () => {
    expect(priceSourceLabel(null, true)).toBe("Set before Jhin tracked price sources");
    expect(priceSourceBadge(null, true)).toEqual({ text: "Yours", tone: "accent" });
    expect(priceSourceBadge(null, false)).toEqual({ text: "No price", tone: "warn" });
  });

  it("shows an em dash rather than half a price", () => {
    expect(formatPricePair(2_500_000, 10_000_000)).toBe("$2.50 in · $10.00 out");
    expect(formatPricePair(2_500_000, null)).toBe("—");
    expect(formatPricePair(null, 10_000_000)).toBe("—");
  });

  it("never renders a blended rate as an input/output pair", () => {
    const blended: ObservedRate = {
      model_key: "gpt-5.6-terra",
      input_cost_micros_per_million: null,
      output_cost_micros_per_million: null,
      blended_cost_micros_per_million: 2_000_000,
      derivation: "blended",
      confidence: "medium",
      note: "",
      sample_runs: 25,
      sample_input_tokens: 1_000_000,
      sample_output_tokens: 100_000,
      computed_at: "2026-08-24T00:00:00Z",
    };
    expect(observedRateSummary(blended)).toBe(
      "$2.00 per 1M tokens (blended — we can't split input from output)",
    );
    expect(observedRateSummary({ ...blended, blended_cost_micros_per_million: null, input_cost_micros_per_million: 100_000, output_cost_micros_per_million: 1_000_000, derivation: "split" })).toBe(
      "$0.10 in · $1.00 out",
    );
  });

  it("names the derivation so its assumptions are visible", () => {
    expect(derivationLabel("provider_quantity")).toContain("itemised invoice");
    expect(derivationLabel("catalog_ratio")).toContain("split assumed from list prices");
  });

  it("prefers the server's pricing page map over the built-in one", () => {
    expect(pricingPageUrl("openai", { openai: "https://example.test/prices" })).toBe(
      "https://example.test/prices",
    );
    expect(pricingPageUrl("openai", null)).toBe("https://platform.openai.com/docs/pricing");
    expect(pricingPageUrl("ollama", null)).toBeNull();
  });

  it("pluralises and truncates the untracked-spend note", () => {
    expect(untrackedSpendNote([], 0)).toBeNull();
    expect(untrackedSpendNote(undefined, undefined)).toBeNull();
    expect(
      untrackedSpendNote([{ model_name: "a", runs: 1, input_tokens: 0, output_tokens: 0 }], 1),
    ).toBe("1 run on a isn't included — no price set.");
    expect(
      untrackedSpendNote(
        [
          { model_name: "a", runs: 1, input_tokens: 0, output_tokens: 0 },
          { model_name: "b", runs: 1, input_tokens: 0, output_tokens: 0 },
          { model_name: "c", runs: 1, input_tokens: 0, output_tokens: 0 },
        ],
        3,
      ),
    ).toBe("3 runs on a, b and 1 more aren't included — no price set.");
  });
});
