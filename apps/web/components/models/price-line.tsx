"use client";

/** One way of saying what a model costs, wherever a model is shown: the
 * cost-tier glyph with the exact pair beside it, "Free (self-hosted)" for a
 * profile the API resolves to $0 by virtue of its provider, or a warning
 * when a hosted vendor's model has no price at all — because "unknown" must
 * never render as "cheap". The hero, the model row and the change-default
 * picker each wear it at their own size and weight; the words are the same. */

import { Badge } from "@/components/ui";
import { costTier, costTierLabel, formatPricePair, type CostTier } from "@/lib/models";
import type { ModelProfile } from "@/lib/types";

export type PriceState = "priced" | "free" | "unpriced";

/** Priced only when both halves are stored; otherwise the API's assumed-free
 * verdict decides between "free" and "no price". */
export function priceState(profile: ModelProfile): PriceState {
  const tier = costTier(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  );
  const priced =
    profile.input_cost_micros_per_million !== null &&
    profile.output_cost_micros_per_million !== null &&
    tier !== null;
  if (priced) return "priced";
  return profile.assumed_free ? "free" : "unpriced";
}

export function PriceLine({
  profile,
  variant,
}: {
  profile: ModelProfile;
  /** hero: the default-model card; option: a change-default radio card;
   * row: a model row, where the pair is spelled out and the tier is quiet. */
  variant: "hero" | "option" | "row";
}) {
  const state = priceState(profile);
  const pair = formatPricePair(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  );
  // priceState only says "priced" when the tier is known.
  const tier = (costTier(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  ) ?? 1) as CostTier;

  if (variant === "row") {
    if (state === "priced") {
      return (
        <p className="text-xs text-faint" data-testid="profile-cost-line">
          <span className="font-mono text-dim" aria-hidden>
            {"$".repeat(tier)}
          </span>{" "}
          {pair}
        </p>
      );
    }
    if (state === "free") {
      return (
        <div className="flex flex-wrap items-center gap-2" data-testid="profile-assumed-free">
          <Badge tone="ok">Free (self-hosted)</Badge>
        </div>
      );
    }
    return <Badge tone="warn">No price yet</Badge>;
  }

  if (state === "free") {
    return variant === "hero" ? (
      <p className="text-sm text-dim" data-testid="cost-tier-free">
        Free (self-hosted) <span className="text-faint">— no per-token price</span>
      </p>
    ) : (
      <span className="text-[13px] text-dim">Free (self-hosted)</span>
    );
  }
  if (state === "unpriced") {
    return variant === "hero" ? (
      <p className="text-sm text-warn">No price set yet</p>
    ) : (
      <span className="text-[13px] text-warn">No price set yet</span>
    );
  }

  const body = (
    <>
      <span className="font-mono text-accent-strong" aria-hidden>
        {"$".repeat(tier)}
      </span>{" "}
      {costTierLabel(tier)} <span className="text-faint">{pair} per 1M tokens</span>
    </>
  );
  return variant === "hero" ? (
    <p className="text-sm text-dim" title={`${pair} per 1M tokens`}>
      {body}
    </p>
  ) : (
    <span className="text-[13px] text-dim" title={`${pair} per 1M tokens`}>
      {body}
    </span>
  );
}
