"use client";

/** Month-to-date tracked spend across every provider, with the budget bar
 * when a monthly budget is set (Settings → Budget). Shared by Models and
 * Home so both read the same numbers the same way. */

import { useState } from "react";
import { formatMicrosAsDollars, summarizeBudget, untrackedSpendNote } from "@/lib/models";
import type { WorkspaceSpend } from "@/lib/types";

/** Chips shown before the per-provider breakdown folds behind "+N more".
 * Dev workspaces accumulate hundreds of providers; the tile stays a tile. */
const CHIP_LIMIT = 8;

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
  // Biggest spenders first, so the capped row leads with what matters.
  const breakdown = [
    ...[...spend.providers]
      .sort((a, b) => b.spent_month_micros - a.spent_month_micros)
      .map((p) => `${p.display_name} ${formatMicrosAsDollars(p.spent_month_micros)}`),
    ...(deletedMonth > 0 ? [`Deleted models ${formatMicrosAsDollars(deletedMonth)}`] : []),
  ];
  const [showAllChips, setShowAllChips] = useState(false);
  const visibleChips = showAllChips ? breakdown : breakdown.slice(0, CHIP_LIMIT);
  const foldedChips = breakdown.length - visibleChips.length;
  return (
    <section
      data-testid="spend-tile"
      aria-label="Spend"
      className={`flex flex-col gap-2 ${
        bare
          ? // Home renders this inside a narrow rail card: stay stacked, or the
            // budget note ends up wrapping one word per line beside the total.
            ""
          : "rounded-2xl border border-line bg-surface px-5 py-4 shadow-card md:flex-row md:items-start md:gap-6"
      }`}
    >
      <div className={bare ? "" : "md:w-64 md:shrink-0"}>
        <p className="whitespace-nowrap text-xs font-medium uppercase tracking-wider text-faint">Spend this month</p>
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
      <div className="min-w-0 flex-1">
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
          <ul data-testid="spend-breakdown" className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-faint">
            {visibleChips.map((line) => (
              // A chip wraps internally rather than ever pushing past the card
              // edge — provider display names are user-entered and unbounded.
              <li
                key={line}
                className="min-w-0 max-w-full break-words rounded-full border border-line bg-raised px-2.5 py-0.5 [overflow-wrap:anywhere]"
              >
                {line}
              </li>
            ))}
            {foldedChips > 0 || showAllChips ? (
              <li>
                <button
                  type="button"
                  aria-expanded={showAllChips}
                  onClick={() => setShowAllChips((open) => !open)}
                  className="rounded-full border border-line px-2.5 py-0.5 text-accent-strong hover:underline"
                >
                  {showAllChips ? "Show fewer" : `+${foldedChips} more`}
                </button>
              </li>
            ) : null}
          </ul>
        ) : null}
      </div>
    </section>
  );
}
