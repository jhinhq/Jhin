"use client";

/** The hero card on the Models page: which model agents think with unless
 * they were given their own. The one decision most workspaces ever make here,
 * so it comes first and in plain language — the machinery lives elsewhere.
 * It is the page's only shadowed object; every list beneath it is a plain
 * bordered box, so the shape alone says which thing matters most. */

import { LogoTile } from "@/components/catalog/logo-tile";
import { OllamaLoadState } from "@/components/models/ollama-load-state";
import { PriceLine } from "@/components/models/price-line";
import { Button, EmptyState } from "@/components/ui";
import { capabilitySummary } from "@/lib/models";
import type { OllamaHost } from "@/lib/ollama-host";
import type { ModelProfile, ModelProvider } from "@/lib/types";

/** "$$ Moderate" with the exact price pair spelled out beside it (a hover
 * tooltip alone is invisible on touch); a warning line when no price is set,
 * because "unknown" must never render as "cheap". Kept as a name for the
 * hero's price line; the markup lives in PriceLine. */
export function CostTierLine(props: { profile: ModelProfile }) {
  return <PriceLine variant="hero" {...props} />;
}

export function DefaultModelCard({
  profile,
  provider,
  isAdmin,
  host,
  onChange,
}: {
  /** The workspace default, or null when none is set. */
  profile: ModelProfile | null;
  /** The provider that default runs on (null when the profile is null). */
  provider: ModelProvider | null;
  isAdmin: boolean;
  /** The page's subscription to the default's Ollama host, when its provider
   * is one; the hero then says whether the model is loaded. */
  host?: OllamaHost;
  /** Opens the change-default dialog. */
  onChange: () => void;
}) {
  if (!profile) {
    return (
      <EmptyState
        title="No default model yet"
        description="Agents use the workspace default unless given their own model. Pick one below."
        action={
          isAdmin ? (
            <Button variant="primary" size="sm" onClick={onChange}>
              Choose a default
            </Button>
          ) : undefined
        }
      />
    );
  }

  return (
    <article
      data-testid="default-model-card"
      className="flex flex-wrap items-center gap-4 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <LogoTile name={provider?.display_name ?? profile.display_name} size={48} />
      <div className="min-w-0 flex-1 basis-56 space-y-0.5">
        <h3 className="truncate font-display text-lg font-bold text-ink" title={profile.model_name}>
          {profile.display_name}
        </h3>
        {capabilitySummary(profile) ? (
          <p className="text-sm text-dim">{capabilitySummary(profile)}</p>
        ) : null}
        <PriceLine profile={profile} variant="hero" />
        {host ? (
          <OllamaLoadState host={host} modelName={profile.model_name} isAdmin={isAdmin} />
        ) : null}
      </div>
      {isAdmin ? (
        // Full width under the text on a phone, a button at the right edge
        // from md up.
        <Button className="basis-full md:basis-auto" onClick={onChange}>
          Change
        </Button>
      ) : null}
    </article>
  );
}
