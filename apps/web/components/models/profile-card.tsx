"use client";

/** One model, one row: the name people picked, the raw identifier when it
 * differs, the provider, what the model is good at, what it costs, and —
 * for a model on an Ollama host — whether it is in memory right now.
 * Everything operational (refresh, delete, provenance) lives in the edit
 * dialog; the row face carries only what a non-expert decides with, and an
 * action that fails says so in the row that acted. */

import { useMutation } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { LogoTile } from "@/components/catalog/logo-tile";
import { OllamaLoadState } from "@/components/models/ollama-load-state";
import { PriceLine, priceState } from "@/components/models/price-line";
import { UnpricedModelNote } from "@/components/unpriced-model-note";
import { Badge, Button, Dialog, ErrorNote } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { capabilitySummary, providerTypeLabel } from "@/lib/models";
import type { OllamaHost } from "@/lib/ollama-host";
import type {
  ModelProfile,
  ModelProvider,
  ModelProviderType,
  ProfilePricing,
} from "@/lib/types";

export function ProfileCard({
  profile,
  provider,
  isDefault,
  isAdmin,
  workspaceId,
  pricing,
  pricingPages,
  host,
  onChanged,
  onEdit,
}: {
  profile: ModelProfile;
  /** Undefined for a member, who cannot list providers; the row then names
   * the provider type from the pricing status instead. */
  provider: ModelProvider | undefined;
  isDefault: boolean;
  isAdmin: boolean;
  workspaceId: string;
  pricing: ProfilePricing | undefined;
  pricingPages: Record<string, string> | undefined;
  /** The page's subscription to the profile's Ollama host, when its provider
   * is one; the row then says whether the model is loaded. */
  host?: OllamaHost;
  onChanged: () => void;
  onEdit: (profile: ModelProfile) => void;
}) {
  const [addPriceOpen, setAddPriceOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const makeDefault = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { default_model_profile_id: profile.id },
      }),
    onSuccess: () => {
      setActionError(null);
      onChanged();
    },
    onError: (error) => setActionError(errorText(error, "Setting the default failed.")),
  });

  // Saving a price from the row: the API stamps anything posted here as
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
      setActionError(null);
      setAddPriceOpen(false);
      onChanged();
    },
    onError: (error) => setActionError(errorText(error, "Saving prices failed.")),
  });

  const providerType: ModelProviderType =
    provider?.type ?? pricing?.provider_type ?? "openai_compatible";
  // The API reports an unpriced profile on a self-hosted provider as assumed
  // free: the row says so calmly instead of warning, and leaves the price
  // fields to the edit dialog for the endpoint that does bill.
  const state = priceState(profile);
  const providerLabel =
    provider?.display_name ?? (pricing ? providerTypeLabel(pricing.provider_type) : null);
  const capability = capabilitySummary(profile);
  // The facts line: the raw identifier only when the name does not already
  // say it, then the provider, then what the model is good at.
  const facts: { key: string; text: string; mono?: boolean }[] = [];
  if (profile.model_name !== profile.display_name) {
    facts.push({ key: "model", text: profile.model_name, mono: true });
  }
  if (providerLabel) facts.push({ key: "provider", text: providerLabel });
  if (capability) facts.push({ key: "capability", text: capability });

  return (
    <li
      data-testid={`profile-card-${profile.id}`}
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-3 md:px-5"
    >
      <LogoTile name={provider?.display_name ?? profile.display_name} size={36} />
      <div className="min-w-0 flex-1 basis-56">
        <h3
          className="flex flex-wrap items-center gap-2 font-display text-sm font-semibold text-ink"
          title={profile.model_name}
        >
          <span className="min-w-0 truncate">{profile.display_name}</span>
          {isDefault ? <Badge tone="info">Default</Badge> : null}
        </h3>
        {facts.length > 0 ? (
          <p className="text-xs text-faint">
            {facts.map((fact, index) => (
              <Fragment key={fact.key}>
                {index > 0 ? " · " : null}
                <span className={fact.mono ? "font-mono" : undefined}>{fact.text}</span>
              </Fragment>
            ))}
          </p>
        ) : null}
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs">
        <PriceLine profile={profile} variant="row" />
        {isAdmin && state === "unpriced" ? (
          <Button size="sm" variant="ghost" onClick={() => setAddPriceOpen(true)}>
            Add price
          </Button>
        ) : null}
      </div>

      {host ? (
        <OllamaLoadState host={host} modelName={profile.model_name} isAdmin={isAdmin} />
      ) : null}

      {isAdmin ? (
        <div className="ml-auto flex shrink-0 gap-1">
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
          <Button size="sm" variant="ghost" onClick={() => onEdit(profile)}>
            Edit
          </Button>
        </div>
      ) : null}

      {actionError ? (
        <div className="basis-full">
          <ErrorNote message={actionError} />
        </div>
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
    </li>
  );
}
