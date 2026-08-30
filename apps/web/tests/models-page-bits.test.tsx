/** Render tests: the Spend tile and the out-of-credit chat card. */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SpendTile } from "@/components/spend-tile";
import type { WorkspaceSpend } from "@/lib/types";

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
