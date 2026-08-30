/** Pure helpers for the Models page: price auto-fill when a model is picked
 * and balance/spend formatting. Kept free of React so they are unit-testable. */

import type {
  BalanceSource,
  ModelProfile,
  ModelProviderType,
  ObservedRate,
  PriceSourceName,
  PriceSource,
  ProviderModelEntry,
  UntrackedModel,
} from "@/lib/types";

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

export const PROVIDER_LABELS: Record<ModelProviderType, string> = {
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

export interface WebSearchSupport {
  supported: boolean;
  reason: string | null;
}

/** Whether a provider/model pair can run the model's built-in web search
 * (mirrors the API's validation in jhin_models.web_search). */
export function webSearchSupport(
  providerType: ModelProviderType,
  modelName: string,
): WebSearchSupport {
  if (providerType === "anthropic" || providerType === "openrouter") {
    return { supported: true, reason: null };
  }
  if (providerType === "openai") {
    const model = modelName.trim().toLowerCase();
    if (model.includes("search-preview") || model.includes("search-api")) {
      return { supported: true, reason: null };
    }
    return {
      supported: false,
      reason:
        "OpenAI only supports built-in web search on its dedicated search models (e.g. gpt-5-search-api or gpt-4o-mini-search-preview).",
    };
  }
  return {
    supported: false,
    reason: "This provider has no built-in web search — grant the agent the web.search tool instead.",
  };
}

/** The profile config_json to save, preserving unrelated keys. */
export function buildProfileConfig(
  existing: Record<string, unknown> | undefined,
  webSearchEnabled: boolean,
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...(existing ?? {}) };
  if (webSearchEnabled) next.web_search = { enabled: true };
  else delete next.web_search;
  return next;
}

export const INSUFFICIENT_FUNDS_CODE = "insufficient_funds";

/** Whether a system/error message payload is the out-of-credit failure. */
export function isInsufficientFunds(content: Record<string, unknown> | null | undefined): boolean {
  return content?.error_code === INSUFFICIENT_FUNDS_CODE;
}

export const MODEL_INCOMPATIBLE_REQUEST_CODE = "model_incompatible_request";

/**
 * Whether the failure is "this model cannot serve this request as configured"
 * (today: a reasoning effort that the provider will not combine with tools).
 * The message names the fix, so it needs a readable card rather than the
 * truncating one-line chip.
 */
export function isModelIncompatibleRequest(
  content: Record<string, unknown> | null | undefined,
): boolean {
  return content?.error_code === MODEL_INCOMPATIBLE_REQUEST_CODE;
}

/** Real pricing pages, for the "we don't know this model's price" prompt.
 *  Mirrors `jhin_models.pricing.PRICING_PAGES`; the API sends the same map on
 *  the pricing-status response and that copy wins when present. */
export const PRICING_PAGE_URLS: Partial<Record<ModelProviderType, string>> = {
  openai: "https://platform.openai.com/docs/pricing",
  anthropic: "https://www.anthropic.com/pricing#api",
  openrouter: "https://openrouter.ai/models",
};

export function pricingPageUrl(
  providerType: ModelProviderType,
  fromApi?: Record<string, string> | null,
): string | null {
  return fromApi?.[providerType] ?? PRICING_PAGE_URLS[providerType] ?? null;
}

/** How far back the built-in list prices may be before we nudge, in months.
 *  Matches `CATALOG_STALE_AFTER_DAYS` on the server (≈6 months). */
export const CATALOG_STALE_AFTER_MONTHS = 6;

/**
 * Whether a `YYYY-MM` catalog stamp is old enough to warn about.
 *
 * List prices move. Showing a two-year-old number without saying so is how a
 * spend total quietly becomes fiction, so an old catalog earns a nudge rather
 * than silent trust. An unparseable stamp counts as stale: not knowing how old
 * the prices are is itself a reason to check.
 */
export function isCatalogStale(catalogUpdated: string | null | undefined, now = new Date()): boolean {
  if (!catalogUpdated) return true;
  const match = /^(\d{4})-(\d{2})$/.exec(catalogUpdated.trim());
  if (!match) return true;
  const year = Number(match[1]);
  const month = Number(match[2]);
  if (month < 1 || month > 12) return true;
  const months =
    (now.getUTCFullYear() - year) * 12 + (now.getUTCMonth() + 1 - month);
  return months > CATALOG_STALE_AFTER_MONTHS;
}

export function catalogStalenessNote(catalogUpdated: string | null | undefined): string | null {
  if (!isCatalogStale(catalogUpdated)) return null;
  return catalogUpdated
    ? `These are list prices from ${catalogUpdated} — check they're current.`
    : "We can't tell how old these list prices are — check they're current.";
}

const PRICE_SOURCE_LABELS: Record<PriceSourceName, string> = {
  user: "Entered by an admin in this workspace",
  observed: "Measured from your actual provider spend",
  provider: "Live from the provider's own model list",
  refreshed_catalog: "From the LiteLLM community price catalog",
  catalog: "Public list price",
};

/**
 * Short label naming where a price came from.
 *
 * `priced` separates the two ways a source can be unknown: no price at all,
 * versus a price whose provenance was never recorded. The second is treated
 * as user-entered, and saying so stops a perfectly good number reading as
 * "no price set".
 */
export function priceSourceLabel(
  source: PriceSourceName | null | undefined,
  priced = false,
): string {
  if (!source) return priced ? "Set before Jhin tracked price sources" : "No price set";
  return PRICE_SOURCE_LABELS[source];
}

export function priceSourceBadge(
  source: PriceSourceName | null | undefined,
  priced = false,
): { text: string; tone: "neutral" | "ok" | "warn" | "accent" | "info" } {
  if (!source && priced) return { text: "Yours", tone: "accent" };
  switch (source) {
    case "user":
      return { text: "Yours", tone: "accent" };
    case "observed":
      return { text: "Measured", tone: "ok" };
    case "provider":
      return { text: "Live", tone: "info" };
    case "refreshed_catalog":
      return { text: "Catalog", tone: "neutral" };
    case "catalog":
      return { text: "List price", tone: "neutral" };
    default:
      return { text: "No price", tone: "warn" };
  }
}

/** "$2.50 in · $10.00 out" — or an em dash when either half is unknown. */
export function formatPricePair(
  inputMicros: number | null | undefined,
  outputMicros: number | null | undefined,
): string {
  if (inputMicros === null || inputMicros === undefined) return "—";
  if (outputMicros === null || outputMicros === undefined) return "—";
  return `${formatMicrosAsDollars(inputMicros)} in · ${formatMicrosAsDollars(outputMicros)} out`;
}

/**
 * The sentence shown under a measured rate.
 *
 * A blended rate has no input/output split — saying so is the whole point,
 * because presenting it as a pair would be a number we made up.
 */
export function observedRateSummary(rate: ObservedRate): string {
  if (rate.blended_cost_micros_per_million !== null) {
    return `${formatMicrosAsDollars(rate.blended_cost_micros_per_million)} per 1M tokens (blended — we can't split input from output)`;
  }
  return formatPricePair(
    rate.input_cost_micros_per_million,
    rate.output_cost_micros_per_million,
  );
}

const DERIVATION_LABELS: Record<ObservedRate["derivation"], string> = {
  provider_quantity: "measured exactly from your provider's itemised invoice",
  split: "measured from itemised spend divided by Jhin's token counts",
  catalog_ratio: "measured total, input/output split assumed from list prices",
  blended: "one blended rate across all tokens",
};

export function derivationLabel(derivation: ObservedRate["derivation"]): string {
  return DERIVATION_LABELS[derivation];
}

/** How expensive a model is, as one of three buckets a non-expert can read. */
export type CostTier = 1 | 2 | 3;

/**
 * Bucket a price pair into a simple tier for the model cards.
 *
 * The buckets are on combined dollars per 1M tokens (input plus output): a
 * missing half counts as $0 so a partly priced model still lands somewhere,
 * but a model with no price at all returns null — "we don't know" must never
 * render as "cheap".
 */
export function costTier(
  inputMicros: number | null,
  outputMicros: number | null,
): CostTier | null {
  if (inputMicros === null && outputMicros === null) return null;
  const combined = ((inputMicros ?? 0) + (outputMicros ?? 0)) / MICROS_PER_DOLLAR;
  if (combined <= 3) return 1;
  if (combined <= 15) return 2;
  return 3;
}

const COST_TIER_LABELS: Record<CostTier, string> = {
  1: "Inexpensive",
  2: "Moderate",
  3: "Premium",
};

export function costTierLabel(tier: CostTier): string {
  return COST_TIER_LABELS[tier];
}

/**
 * One plain-language line saying what a model is good at.
 *
 * Built from the two facts a profile reliably carries — context window and
 * whether built-in web search is on — because a claim the data cannot back
 * ("great at code") would be marketing, not a capability.
 */
export function capabilitySummary(profile: ModelProfile): string {
  const parts: string[] = [];
  const window = profile.context_window ?? 0;
  if (window >= 400_000) parts.push("Reads very long documents");
  else if (window >= 100_000) parts.push("Handles long documents");
  // An unknown window earns no claim at all: a line every card repeats
  // verbatim conveys nothing, so the card simply says less.
  else if (window > 0) parts.push("Good for everyday tasks");
  const webSearch = (profile.config_json as { web_search?: { enabled?: boolean } }).web_search;
  if (webSearch?.enabled) parts.push("Can search the web");
  return parts.join(" · ");
}

/**
 * The honest footnote for a spend total that excludes unpriced models.
 *
 * Without it, a run on an unpriced model is indistinguishable from a free one
 * and the total reads as complete when it is not.
 */
export function untrackedSpendNote(
  untracked: UntrackedModel[] | undefined,
  untrackedRuns: number | undefined,
): string | null {
  if (!untracked || untracked.length === 0 || !untrackedRuns) return null;
  const single = untrackedRuns === 1;
  const named = untracked
    .slice(0, 2)
    .map((row) => row.model_name)
    .join(", ");
  const more = untracked.length > 2 ? ` and ${untracked.length - 2} more` : "";
  return `${untrackedRuns} ${single ? "run" : "runs"} on ${named}${more} ${
    single ? "isn't" : "aren't"
  } included — no price set.`;
}
