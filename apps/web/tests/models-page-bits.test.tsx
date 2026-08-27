/** Render tests: the Spend tile and the out-of-credit chat card. */

import { render, screen } from "@testing-library/react";
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

  it("renders the budget bar with the spent share", () => {
    render(<SpendTile spend={spend({ monthly_budget_micros: 50_000_000 })} />);
    const bar = screen.getByRole("progressbar", { name: "Budget used" });
    expect(bar.getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByText("$12.50 of $50.00 (25%)")).toBeTruthy();
  });
});
