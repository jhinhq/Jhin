"use client";

/** Spend on the Models page, folded: the month's total lives in a closed
 * disclosure's label, so the number is one glance away without a 2xl figure
 * leading a page that is about choosing models (Home already leads with it).
 * The moment the budget is actually in trouble the page interrupts — a
 * banner at the top and the disclosure open, in danger — and not before. */

import { Disclosure } from "@/components/company/bits";
import { SpendTile } from "@/components/spend-tile";
import { budgetBannerText, formatMicrosAsDollars, summarizeBudget } from "@/lib/models";
import type { WorkspaceSpend } from "@/lib/types";

function budgetOf(spend: WorkspaceSpend) {
  return summarizeBudget(
    spend.spent_month_micros,
    spend.monthly_budget_micros,
    spend.warning_threshold,
  );
}

/** The interrupt: rendered only while the month is at or over the warning
 * threshold, warn-toned until the budget is crossed, danger after. */
export function BudgetBanner({ spend }: { spend: WorkspaceSpend }) {
  const budget = budgetOf(spend);
  const text = budgetBannerText(budget);
  if (!budget || !text) return null;
  const tone =
    budget.tone === "over"
      ? "border-danger/30 bg-danger-soft text-danger"
      : "border-warn/30 bg-warn-soft text-warn";
  return (
    <p
      role="status"
      data-testid="budget-banner"
      className={`rounded-xl border px-3.5 py-2.5 text-sm ${tone}`}
    >
      {text}
    </p>
  );
}

export function SpendDisclosure({ spend }: { spend: WorkspaceSpend }) {
  const budget = budgetOf(spend);
  const alarmed = budget !== null && budget.tone !== "ok";
  const label = alarmed
    ? `Spend this month — ${budget.label}`
    : `Spend this month — ${formatMicrosAsDollars(spend.spent_month_micros)}`;
  return (
    <Disclosure
      label={label}
      openLabel="Hide spend"
      defaultOpen={alarmed}
      tone={alarmed ? "danger" : "accent"}
    >
      <SpendTile spend={spend} />
    </Disclosure>
  );
}
