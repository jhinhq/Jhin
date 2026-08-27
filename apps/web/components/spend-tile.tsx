"use client";

/** Month-to-date tracked spend across every provider, with the budget bar
 * when a monthly budget is set (Settings → Budget). Shared by Models and
 * Home so both read the same numbers the same way. */

import { formatMicrosAsDollars, summarizeBudget, untrackedSpendNote } from "@/lib/models";
import type { WorkspaceSpend } from "@/lib/types";

export function SpendTile({
  spend,
  bare = false,
}: {
  spend: WorkspaceSpend;
  /** Drop the card chrome when the caller already renders one (Home). */
  bare?: boolean;
}) {
  const budget = summarizeBudget(
    spend.spent_month_micros,
    spend.monthly_budget_micros,
    spend.warning_threshold,
  );
  const barTone =
    budget?.tone === "over" ? "bg-danger" : budget?.tone === "warn" ? "bg-warn" : "bg-accent";
  // Runs on unpriced models contributed $0 to the total above. Saying so is
  // the difference between "you spent this much" and "at least this much".
  const untracked = untrackedSpendNote(spend.untracked, spend.untracked_runs);
  // Deleting a model does not refund its runs. That spend stays in the total,
  // so it gets its own line here — otherwise the per-provider figures quietly
  // stop adding up to the number above them.
  const deletedMonth = spend.deleted_model_month_micros ?? 0;
  const breakdown = [
    ...spend.providers.map(
      (p) => `${p.display_name} ${formatMicrosAsDollars(p.spent_month_micros)}`,
    ),
    ...(deletedMonth > 0 ? [`Deleted models ${formatMicrosAsDollars(deletedMonth)}`] : []),
  ];
  return (
    <section
      data-testid="spend-tile"
      aria-label="Spend"
      className={`flex flex-col gap-2 md:flex-row md:items-center md:gap-6 ${
        bare ? "" : "rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
      }`}
    >
      <div>
        <p className="text-xs font-medium uppercase tracking-wider text-faint">Spend this month</p>
        <p className="font-display text-2xl font-semibold tabular-nums text-ink">
          {formatMicrosAsDollars(spend.spent_month_micros)}
        </p>
        <p className="text-xs text-dim">
          {formatMicrosAsDollars(spend.spent_total_micros)} all time · tracked by Jhin from run costs
        </p>
        {untracked ? (
          <p data-testid="spend-untracked" className="mt-1 text-xs text-warn">
            {untracked}
          </p>
        ) : null}
      </div>
      <div className="flex-1">
        {budget ? (
          <div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-dim">Monthly budget</span>
              <span className={budget.tone === "ok" ? "text-dim" : "text-danger"}>{budget.label}</span>
            </div>
            <div
              role="progressbar"
              aria-label="Budget used"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.min(budget.percent, 100)}
              className="mt-1 h-2 overflow-hidden rounded-full bg-raised"
            >
              <div className={`h-full rounded-full ${barTone}`} style={{ width: `${budget.ratio * 100}%` }} />
            </div>
          </div>
        ) : (
          <p className="text-xs text-faint">No monthly budget set — add one under Settings to get a warning bar here.</p>
        )}
        {breakdown.length > 1 ? (
          <p data-testid="spend-breakdown" className="mt-1 truncate text-xs text-faint">
            {breakdown.join(" · ")}
          </p>
        ) : null}
      </div>
    </section>
  );
}
