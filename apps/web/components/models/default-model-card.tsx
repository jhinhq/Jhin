"use client";

/** The hero card on the Models page: which model agents think with unless
 * they were given their own. The one decision most workspaces ever make here,
 * so it comes first and in plain language — the machinery lives elsewhere. */

import { EmptyState } from "@/components/ui";
import { Button } from "@/components/ui";
import { LogoTile } from "@/components/catalog/logo-tile";
import { capabilitySummary, costTier, costTierLabel, formatPricePair } from "@/lib/models";
import type { ModelProfile, ModelProvider } from "@/lib/types";

/** "$$ Moderate" with the exact price pair spelled out beside it (a hover
 * tooltip alone is invisible on touch); a warning line when no price is set,
 * because "unknown" must never render as "cheap". */
export function CostTierLine({ profile }: { profile: ModelProfile }) {
  const tier = costTier(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  );
  if (tier === null) {
    return <p className="text-sm text-warn">No price set yet</p>;
  }
  const pair = formatPricePair(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  );
  return (
    <p className="text-sm text-dim" title={`${pair} per 1M tokens`}>
      <span className="font-mono text-accent-strong" aria-hidden>
        {"$".repeat(tier)}
      </span>{" "}
      {costTierLabel(tier)} <span className="text-faint">{pair} per 1M tokens</span>
    </p>
  );
}

export function DefaultModelCard({
  profile,
  provider,
  isAdmin,
  onChange,
}: {
  /** The workspace default, or null when none is set. */
  profile: ModelProfile | null;
  /** The provider that default runs on (null when the profile is null). */
  provider: ModelProvider | null;
  isAdmin: boolean;
  /** Opens the change-default dialog. */
  onChange: () => void;
}) {
  if (!profile) {
    return (
      <EmptyState
        title="No default model yet"
        description="Agents use the workspace default unless given their own model. Pick one below."
      />
    );
  }

  return (
    <article
      data-testid="default-model-card"
      className="flex items-center gap-4 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <LogoTile name={provider?.display_name ?? profile.display_name} size={48} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <h3 className="truncate font-display text-lg font-bold text-ink" title={profile.model_name}>
          {profile.display_name}
        </h3>
        {capabilitySummary(profile) ? (
          <p className="text-sm text-dim">{capabilitySummary(profile)}</p>
        ) : null}
        <CostTierLine profile={profile} />
      </div>
      {isAdmin ? (
        <Button className="shrink-0" onClick={onChange}>
          Change
        </Button>
      ) : null}
    </article>
  );
}
