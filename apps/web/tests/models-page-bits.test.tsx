/** Render tests for the Models page's smaller pieces: the Spend tile, the
 * disclosure and budget banner that fold it, and the pricing section that
 * owns the catalog-refresh and measure actions. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PricingSection } from "@/components/models/pricing-section";
import { BudgetBanner, SpendDisclosure } from "@/components/models/spend-disclosure";
import { SpendTile } from "@/components/spend-tile";
import { api, ApiError } from "@/lib/api";
import type { PricingStatus, WorkspaceSpend } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function spend(overrides: Partial<WorkspaceSpend> = {}): WorkspaceSpend {
  return {
    spent_month_micros: 12_500_000,
    spent_total_micros: 40_000_000,
    period_start: "2026-08-01T00:00:00Z",
    providers: [],
    monthly_budget_micros: null,
    warning_threshold: 0.8,
    fetched_at: "2026-08-22T00:00:00Z",
    untracked: [],
    untracked_runs: 0,
    ...overrides,
  };
}

describe("SpendTile", () => {
  it("shows the month total and a hint when no budget is set", () => {
    render(<SpendTile spend={spend()} />);
    expect(screen.getByText("$12.50")).toBeTruthy();
    expect(screen.getByText(/\$40\.00 all time/)).toBeTruthy();
    expect(screen.getByText(/No monthly budget set/)).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("names spend from deleted models so the breakdown adds up", () => {
    render(
      <SpendTile
        spend={spend({
          providers: [
            {
              provider_id: "prov-1",
              display_name: "OpenAI",
              type: "openai",
              spent_month_micros: 10_000_000,
              spent_total_micros: 10_000_000,
            },
          ],
          deleted_model_month_micros: 2_500_000,
          deleted_model_total_micros: 2_500_000,
        })}
      />,
    );
    const breakdown = screen.getByTestId("spend-breakdown").textContent ?? "";
    expect(breakdown).toContain("OpenAI $10.00");
    expect(breakdown).toContain("Deleted models $2.50");
  });

  it("keeps a long provider name inside its own wrapping chip — never clipped", () => {
    const longName =
      "Extremely Long Self-Hosted OpenAI-Compatible Endpoint For The Berlin Research Cluster (staging)";
    const { container } = render(
      <SpendTile
        spend={spend({
          providers: [
            {
              provider_id: "prov-long",
              display_name: longName,
              type: "openai_compatible",
              spent_month_micros: 3_510,
              spent_total_micros: 3_510,
            },
            {
              provider_id: "prov-2",
              display_name: "OpenAI",
              type: "openai",
              spent_month_micros: 10_000_000,
              spent_total_micros: 10_000_000,
            },
          ],
        })}
      />,
    );
    const chips = Array.from(container.querySelectorAll('[data-testid="spend-breakdown"] li'));
    expect(chips).toHaveLength(2);
    const longChip = chips.find((chip) => chip.textContent?.includes(longName));
    expect(longChip).toBeTruthy();
    // The chip wraps within the card instead of forcing a one-line overflow:
    // no nowrap, capped at the container, and breakable anywhere.
    expect(longChip?.className).not.toContain("whitespace-nowrap");
    expect(longChip?.className).toContain("max-w-full");
    expect(longChip?.className).toContain("break-words");
  });

  it("caps the breakdown at eight chips, biggest spenders first, behind +N more", () => {
    const providers = Array.from({ length: 12 }, (_, index) => ({
      provider_id: `prov-${index}`,
      display_name: `Provider ${index}`,
      type: "openai" as const,
      spent_month_micros: (index + 1) * 1_000_000,
      spent_total_micros: (index + 1) * 1_000_000,
    }));
    const { container, getByRole } = render(<SpendTile spend={spend({ providers })} />);
    const chipsOf = () =>
      Array.from(container.querySelectorAll('[data-testid="spend-breakdown"] li')).map(
        (chip) => chip.textContent ?? "",
      );

    let chips = chipsOf();
    // 8 provider chips plus the toggle; the biggest spender leads.
    expect(chips).toHaveLength(9);
    expect(chips[0]).toBe("Provider 11 $12.00");
    expect(chips[8]).toBe("+4 more");

    fireEvent.click(getByRole("button", { name: "+4 more" }));
    chips = chipsOf();
    expect(chips).toHaveLength(13);
    expect(chips[12]).toBe("Show fewer");

    fireEvent.click(getByRole("button", { name: "Show fewer" }));
    expect(chipsOf()).toHaveLength(9);
  });

  it("renders the budget bar with the spent share", () => {
    render(<SpendTile spend={spend({ monthly_budget_micros: 50_000_000 })} />);
    const bar = screen.getByRole("progressbar", { name: "Budget used" });
    expect(bar.getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByText("$12.50 of $50.00 (25%)")).toBeTruthy();
  });
});

describe("SpendDisclosure", () => {
  it("folds the tile behind a label carrying the month total", () => {
    render(<SpendDisclosure spend={spend()} />);
    expect(screen.queryByTestId("spend-tile")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Spend this month — $12.50" }));
    expect(screen.getByTestId("spend-tile")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hide spend" })).toBeTruthy();
  });

  it("opens itself and turns danger when the budget is in warning", () => {
    render(
      <SpendDisclosure
        spend={spend({ monthly_budget_micros: 50_000_000, spent_month_micros: 42_000_000 })}
      />,
    );
    // Open from the start: the number is the interrupt, not a click away.
    expect(screen.getByTestId("spend-tile")).toBeTruthy();
    const button = screen.getByRole("button", { name: "Hide spend" });
    expect(button.className).toContain("text-danger");
    // Folded again, the label itself carries the warning.
    fireEvent.click(button);
    expect(screen.queryByTestId("spend-tile")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Spend this month — $42.00 of $50.00 (84%)" }),
    ).toBeTruthy();
  });
});

describe("BudgetBanner", () => {
  it("interrupts near the budget and past it, each in its own tone", () => {
    render(
      <BudgetBanner
        spend={spend({ monthly_budget_micros: 50_000_000, spent_month_micros: 42_000_000 })}
      />,
    );
    const warn = screen.getByTestId("budget-banner");
    expect(warn.textContent).toBe(
      "$42.00 of $50.00 (84%) of the monthly budget — runs stop once it's reached.",
    );
    expect(warn.className).toContain("text-warn");

    cleanup();
    render(
      <BudgetBanner
        spend={spend({ monthly_budget_micros: 50_000_000, spent_month_micros: 60_000_000 })}
      />,
    );
    const over = screen.getByTestId("budget-banner");
    expect(over.textContent).toBe(
      "Over budget: $60.00 of $50.00 — new runs are refused until the budget is raised under Settings.",
    );
    expect(over.className).toContain("text-danger");
  });

  it("stays silent while the budget is fine or there is none", () => {
    render(<BudgetBanner spend={spend({ monthly_budget_micros: 50_000_000 })} />);
    expect(screen.queryByTestId("budget-banner")).toBeNull();
    cleanup();
    render(<BudgetBanner spend={spend()} />);
    expect(screen.queryByTestId("budget-banner")).toBeNull();
  });
});

const PRICING: PricingStatus = {
  catalog_updated: "2026-01",
  catalog_stale: false,
  refreshed_source: null,
  refreshed_fetched_at: null,
  refreshed_entry_count: 0,
  refreshed_attribution: null,
  refreshed_project_url: "https://github.com/BerriAI/litellm",
  profiles: [],
  untracked: [],
  untracked_runs: 0,
  reconcile_available: true,
  reconcile_detail: "",
  pricing_pages: {},
};

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onChanged = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <PricingSection
        workspaceId="w1"
        isAdmin
        status={PRICING}
        isPending={false}
        onChanged={onChanged}
      />
    </QueryClientProvider>,
  );
  return { onChanged };
}

describe("PricingSection", () => {
  it("posts the catalog refresh and shows its sentence", async () => {
    vi.mocked(api).mockResolvedValue({
      ok: true,
      detail: "Fetched 1,204 prices from LiteLLM.",
    });
    const { onChanged } = renderSection();
    // Closed by default: the panel is not on the page until asked for.
    expect(screen.queryByTestId("pricing-panel")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Advanced — where prices come from" }));
    fireEvent.click(screen.getByRole("button", { name: /Refresh price catalog/ }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        "/api/v1/workspaces/w1/model-profiles/refresh-catalog",
        { method: "POST" },
      ),
    );
    const result = await screen.findByTestId("catalog-refresh-result");
    expect(result.textContent).toContain("Fetched 1,204 prices from LiteLLM.");
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("shows a failed measure in the panel's error note", async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(500, "boom"));
    const { onChanged } = renderSection();
    fireEvent.click(screen.getByRole("button", { name: "Advanced — where prices come from" }));
    fireEvent.click(screen.getByRole("button", { name: /Measure real rates/ }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        "/api/v1/workspaces/w1/model-profiles/reconcile-pricing",
        { method: "POST" },
      ),
    );
    const note = await screen.findByRole("alert");
    expect(note.textContent).toBe("boom");
    expect(screen.getByTestId("pricing-panel").contains(note)).toBe(true);
    expect(onChanged).not.toHaveBeenCalled();
  });
});
