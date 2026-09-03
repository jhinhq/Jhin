"use client";

/** One model, one card: the name people picked, what the model is good at,
 * and what it roughly costs — with the exact price pair kept one glance
 * deeper. Everything operational (refresh, delete, provenance) lives in the
 * edit dialog; the card face carries only what a non-expert decides with. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { UnpricedModelNote } from "@/components/unpriced-model-note";
import { Badge, Button, Dialog } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { capabilitySummary, costTier, formatPricePair } from "@/lib/models";
import type {
  ModelProfile,
  ModelProvider,
  ModelProviderType,
  ProfilePricing,
} from "@/lib/types";

function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

export function ProfileCard({
  profile,
  provider,
  isDefault,
  isAdmin,
  workspaceId,
  pricing,
  pricingPages,
  onChanged,
  onError,
  onEdit,
}: {
  profile: ModelProfile;
  provider: ModelProvider | undefined;
  isDefault: boolean;
  isAdmin: boolean;
  workspaceId: string;
  pricing: ProfilePricing | undefined;
  pricingPages: Record<string, string> | undefined;
  onChanged: () => void;
  onError: (message: string | null) => void;
  onEdit: (profile: ModelProfile) => void;
}) {
  const [addPriceOpen, setAddPriceOpen] = useState(false);

  const makeDefault = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { default_model_profile_id: profile.id },
      }),
    onSuccess: () => {
      onError(null);
      onChanged();
    },
    onError: (error) => onError(errText(error, "Setting the default failed.")),
  });

  // Saving a price from the card: the API stamps anything posted here as
  // user-entered, so no later automatic refresh will move it.
  const savePrices = useMutation({
    mutationFn: (costs: { input: number | null; output: number | null }) =>
      api<ModelProfile>(`/api/v1/workspaces/${workspaceId}/model-profiles/${profile.id}`, {
        method: "PATCH",
        body: {
          input_cost_micros_per_million: costs.input,
          output_cost_micros_per_million: costs.output,
        },
      }),
    onSuccess: () => {
      onError(null);
      setAddPriceOpen(false);
      onChanged();
    },
    onError: (error) => onError(errText(error, "Saving prices failed.")),
  });

  const priced =
    profile.input_cost_micros_per_million !== null &&
    profile.output_cost_micros_per_million !== null;
  const tier = costTier(
    profile.input_cost_micros_per_million,
    profile.output_cost_micros_per_million,
  );
  const providerType: ModelProviderType = provider?.type ?? "openai_compatible";
  // The API reports an unpriced profile on a self-hosted provider as assumed
  // free: the card says so calmly instead of warning, and leaves the price
  // fields to the edit dialog for the endpoint that does bill.
  const assumedFree = Boolean(profile.assumed_free);

  return (
    <article
      data-testid={`profile-card-${profile.id}`}
      className="flex flex-col gap-2 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3
            className="truncate font-display text-sm font-semibold text-ink"
            title={profile.model_name}
          >
            {profile.display_name}
          </h3>
          <p className="mt-0.5 text-xs text-faint">{provider?.display_name ?? "—"}</p>
        </div>
        {isDefault ? <Badge tone="info">Default</Badge> : null}
      </header>

      {capabilitySummary(profile) ? (
        <p className="text-sm text-dim">{capabilitySummary(profile)}</p>
      ) : null}

      {priced && tier !== null ? (
        <p className="text-xs text-faint" data-testid="profile-cost-line">
          <span className="font-mono text-accent-strong" aria-hidden>
            {"$".repeat(tier)}
          </span>{" "}
          {formatPricePair(
            profile.input_cost_micros_per_million,
            profile.output_cost_micros_per_million,
          )}
        </p>
      ) : assumedFree ? (
        <div className="flex flex-wrap items-center gap-2" data-testid="profile-assumed-free">
          <Badge tone="ok">Free (self-hosted)</Badge>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="warn">No price yet</Badge>
          {isAdmin ? (
            <Button size="sm" variant="ghost" onClick={() => setAddPriceOpen(true)}>
              Add price
            </Button>
          ) : null}
        </div>
      )}

      {isAdmin ? (
        <footer className="mt-auto flex items-center gap-2 border-t border-line pt-3">
          {!isDefault ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={makeDefault.isPending}
              onClick={() => makeDefault.mutate()}
            >
              Make default
            </Button>
          ) : null}
          <Button size="sm" variant="ghost" className="ml-auto" onClick={() => onEdit(profile)}>
            Edit
          </Button>
        </footer>
      ) : null}

      {addPriceOpen ? (
        <Dialog title="Add a price" open onClose={() => setAddPriceOpen(false)}>
          <UnpricedModelNote
            modelName={profile.model_name}
            providerType={providerType}
            pricingPages={pricingPages}
            runs={pricing?.runs_this_month ?? 0}
            saving={savePrices.isPending}
            onSave={(input: number | null, output: number | null) =>
              savePrices.mutate({ input, output })
            }
          />
        </Dialog>
      ) : null}
    </article>
  );
}
