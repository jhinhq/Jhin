/** Pure helpers for the Models page: price auto-fill when a model is picked
 * and balance/spend formatting. Kept free of React so they are unit-testable. */

import type { BalanceSource, ModelProviderType, PriceSource, ProviderModelEntry } from "@/lib/types";

export const MICROS_PER_DOLLAR = 1_000_000;

/** Micro-dollars to a form-friendly dollar string ("" when unknown). */
export function microsToDollarInput(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "";
  return String(micros / MICROS_PER_DOLLAR);
}

/** Dollar form input to micro-dollars (null when empty or not a number). */
export function dollarInputToMicros(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Math.round(parsed * MICROS_PER_DOLLAR);
}

export interface PriceAutofill {
  /** Whether the picked model carried any pricing. */
  known: boolean;
  inputCost: string;
  outputCost: string;
  contextWindow: string;
  source: PriceSource;
  /** One-line explanation shown under the pricing fields. */
  note: string;
}

const PROVIDER_LABELS: Record<ModelProviderType, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  ollama: "Ollama",
  openai_compatible: "the provider",
};

/** What to auto-fill when `modelId` is picked from `entries`. Matching is
 * case-insensitive on the exact identifier; unknown models return empty
 * fields with a hint to enter prices manually. */
export function autofillForModel(
  modelId: string,
  entries: ProviderModelEntry[] | undefined,
  providerType: ModelProviderType,
  catalogUpdated: string | null | undefined,
): PriceAutofill {
  const wanted = modelId.trim().toLowerCase();
  const entry = entries?.find((candidate) => candidate.id.toLowerCase() === wanted);
  const label = PROVIDER_LABELS[providerType] ?? providerType;
  if (!entry || entry.source === null) {
    return {
      known: false,
      inputCost: "",
      outputCost: "",
      contextWindow: entry?.context_window ? String(entry.context_window) : "",
      source: null,
      note: wanted
        ? `No public price is known for this model — enter the prices from ${label}'s pricing page.`
        : "Pick a model to auto-fill its prices.",
    };
  }
  const note =
    entry.source === "provider"
      ? `Live from ${label} — edit if your contract differs.`
      : `Prices from ${label}'s public list (catalog updated ${catalogUpdated ?? "recently"}) — edit if your contract differs.`;
  return {
    known: true,
    inputCost: microsToDollarInput(entry.input_cost_micros_per_million),
    outputCost: microsToDollarInput(entry.output_cost_micros_per_million),
    contextWindow: entry.context_window ? String(entry.context_window) : "",
    source: entry.source,
    note,
  };
}

/** Dollars with two decimals, or five when the amount is tiny but non-zero. */
export function formatMicrosAsDollars(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  const dollars = micros / MICROS_PER_DOLLAR;
  if (dollars === 0) return "$0.00";
  const abs = Math.abs(dollars);
  const text = abs < 0.01 ? abs.toFixed(5) : abs.toFixed(2);
  return dollars < 0 ? `-$${text}` : `$${text}`;
}

export function balanceSourceLabel(source: BalanceSource): string {
  switch (source) {
    case "openrouter":
      return "Live from OpenRouter";
    case "openai_admin":
      return "From OpenAI's admin API (month to date)";
    default:
      return "Tracked by Jhin";
  }
}

export interface BudgetSummary {
  /** 0..1 share of the budget spent (capped at 1 for the bar). */
  ratio: number;
  percent: number;
  tone: "ok" | "warn" | "over";
  label: string;
}

/** Progress against a monthly budget; null when no budget is set. */
export function summarizeBudget(
  spentMicros: number,
  budgetMicros: number | null,
  warningThreshold = 0.8,
): BudgetSummary | null {
  if (budgetMicros === null || budgetMicros <= 0) return null;
  const raw = spentMicros / budgetMicros;
  const ratio = Math.min(Math.max(raw, 0), 1);
  const percent = Math.round(raw * 100);
  const tone = raw >= 1 ? "over" : raw >= warningThreshold ? "warn" : "ok";
  const label =
    tone === "over"
      ? `Over budget: ${formatMicrosAsDollars(spentMicros)} of ${formatMicrosAsDollars(budgetMicros)}`
      : `${formatMicrosAsDollars(spentMicros)} of ${formatMicrosAsDollars(budgetMicros)} (${percent}%)`;
  return { ratio, percent, tone, label };
}

export const INSUFFICIENT_FUNDS_CODE = "insufficient_funds";

/** Whether a system/error message payload is the out-of-credit failure. */
export function isInsufficientFunds(content: Record<string, unknown> | null | undefined): boolean {
  return content?.error_code === INSUFFICIENT_FUNDS_CODE;
}
