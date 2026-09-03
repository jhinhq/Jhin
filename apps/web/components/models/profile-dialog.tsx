"use client";

/** Create or edit a model profile: a provider, a name, the model identifier
 * (with the provider's listing as a datalist), auto-filled prices with their
 * provenance, the built-in web-search switch, and — when editing — price
 * provenance, "Refresh prices", and Delete through the shared ConfirmDialog. */

import { useMutation } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useId, useState } from "react";
import { UnpricedModelNote } from "@/components/unpriced-model-note";
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  ErrorNote,
  Field,
  Input,
  Select,
} from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useProviderModels } from "@/lib/hooks";
import {
  autofillForModel,
  buildProfileConfig,
  catalogStalenessNote,
  derivationLabel,
  dollarInputToMicros,
  effectivePriceSource,
  formatPricePair,
  isSelfHostedProvider,
  microsToDollarInput,
  observedRateSummary,
  priceSourceBadge,
  priceSourceLabel,
  selfHostedPriceNote,
  webSearchSupport,
  type ProfilePrefill,
} from "@/lib/models";
import type {
  ModelProfile,
  ModelProvider,
  ModelProviderType,
  ProfilePricing,
  ProfilePricingRefresh,
} from "@/lib/types";

export function ProfileDialog({
  workspaceId,
  providers,
  existing,
  prefill,
  isDefault = false,
  pricing,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  providers: ModelProvider[];
  /** When set, the dialog edits this profile (PATCH) instead of creating one. */
  existing?: ModelProfile;
  /** Starting values for a new profile (a local model picked on the Ollama
   * panel); ignored when editing. */
  prefill?: ProfilePrefill;
  /** Whether the profile being edited is the workspace default. */
  isDefault?: boolean;
  /** The price provenance for the profile being edited, when known. */
  pricing?: ProfilePricing;
  onClose: () => void;
  onCreated: () => void;
}) {
  const initialProviderId = existing?.provider_id ?? prefill?.providerId ?? providers[0]?.id ?? "";
  const [providerId, setProviderId] = useState(initialProviderId);
  const [displayName, setDisplayName] = useState(
    existing?.display_name ?? prefill?.displayName ?? "",
  );
  const [modelName, setModelName] = useState(existing?.model_name ?? prefill?.modelName ?? "");
  // A new profile on a self-hosted provider starts at $0 — its true price —
  // the moment the provider is chosen, not once a model is picked. Otherwise
  // the fields sit empty behind cloud placeholders ("0.15", "0.60") under a
  // summary that calls the model free. A stored row keeps whatever it holds
  // (an assumed-free profile keeps its empty fields), and a prefill already
  // carries $0.
  const initialType = providers.find((p) => p.id === initialProviderId)?.type;
  const startsFree =
    !existing && !prefill && initialType !== undefined && isSelfHostedProvider(initialType);
  const [inputCost, setInputCost] = useState(
    startsFree
      ? "0"
      : microsToDollarInput(
          existing?.input_cost_micros_per_million ?? prefill?.inputCostMicros ?? null,
        ),
  );
  const [outputCost, setOutputCost] = useState(
    startsFree
      ? "0"
      : microsToDollarInput(
          existing?.output_cost_micros_per_million ?? prefill?.outputCostMicros ?? null,
        ),
  );
  const [contextWindow, setContextWindow] = useState(() => {
    const tokens = existing?.context_window ?? prefill?.contextWindow;
    return tokens ? String(tokens) : "";
  });
  const [webSearchEnabled, setWebSearchEnabled] = useState(
    Boolean(
      (existing?.config_json as { web_search?: { enabled?: boolean } } | undefined)?.web_search
        ?.enabled,
    ),
  );
  const [pricingOpen, setPricingOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  const providerModels = useProviderModels(workspaceId, providerId || null);
  const provider = providers.find((p) => p.id === providerId);
  const providerType: ModelProviderType = provider?.type ?? "openai_compatible";
  // A prefill carries a real price ($0 for a local model), and the note under
  // the pricing fields must say so rather than stay silent about it.
  const prefillNote = prefill
    ? autofillForModel(prefill.modelName, undefined, providerType, null)
    : null;
  const [pricingNote, setPricingNote] = useState<string | null>(
    prefillNote?.known ? prefillNote.note : null,
  );
  // The model the prices were last auto-filled for, so edits survive
  // re-renders. A prefill or a stored row counts as already filled: the
  // provider's model list arriving later must not overwrite what the panel
  // handed over or what the row holds (an assumed-free profile keeps its
  // empty fields; "Refresh prices" is the deliberate way to look them up
  // again). Picking a different model still fills.
  const [autofilledFor, setAutofilledFor] = useState<string | null>(
    existing
      ? `${existing.provider_id}:${existing.model_name.trim().toLowerCase()}`
      : prefill
        ? `${prefill.providerId}:${prefill.modelName.trim().toLowerCase()}`
        : null,
  );
  const webSupport = webSearchSupport(providerType, modelName);
  const entries = providerModels.data?.models;
  const catalogUpdated = providerModels.data?.catalog_updated ?? null;
  const pricingPanelId = useId();

  // Auto-fill prices (and context window) whenever a listed model is picked:
  // on typing/picking (handler) and when the model list arrives after the
  // name was already typed (derived state adjusted during render).
  const applyAutofill = (name: string, list: typeof entries) => {
    const key = `${providerId}:${name.trim().toLowerCase()}`;
    if (!name.trim() || !list || autofilledFor === key) return;
    const listed = list.some((e) => e.id.toLowerCase() === name.trim().toLowerCase());
    if (!listed) return;
    const fill = autofillForModel(name, list, providerType, catalogUpdated);
    setAutofilledFor(key);
    setPricingNote(fill.note);
    if (fill.known) {
      setInputCost(fill.inputCost);
      setOutputCost(fill.outputCost);
    } else {
      // Unknown price: open the disclosure so the user sees what to enter.
      setPricingOpen(true);
    }
    if (fill.contextWindow) setContextWindow(fill.contextWindow);
  };
  const [seenEntries, setSeenEntries] = useState(entries);
  if (seenEntries !== entries) {
    setSeenEntries(entries);
    applyAutofill(modelName, entries);
  }

  const create = useMutation({
    mutationFn: () =>
      api(
        existing
          ? `/api/v1/workspaces/${workspaceId}/model-profiles/${existing.id}`
          : `/api/v1/workspaces/${workspaceId}/model-profiles`,
        {
          method: existing ? "PATCH" : "POST",
          body: {
            provider_id: providerId,
            display_name: displayName.trim(),
            model_name: modelName.trim(),
            context_window: contextWindow.trim() ? Number(contextWindow) : null,
            // UI takes $ per 1M tokens; API stores micro-dollars per 1M.
            input_cost_micros_per_million: dollarInputToMicros(inputCost),
            output_cost_micros_per_million: dollarInputToMicros(outputCost),
            config_json: buildProfileConfig(
              existing?.config_json,
              webSearchEnabled && webSupport.supported,
            ),
          },
        },
      ),
    onSuccess: () => {
      onCreated();
      onClose();
    },
  });

  const refreshPrices = useMutation({
    mutationFn: () =>
      api<ProfilePricingRefresh>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/${existing?.id}/refresh-pricing`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setRefreshNote(result.detail);
      if (result.updated) {
        setInputCost(microsToDollarInput(result.profile.input_cost_micros_per_million));
        setOutputCost(microsToDollarInput(result.profile.output_cost_micros_per_million));
        onCreated();
      }
    },
  });

  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/api/v1/workspaces/${workspaceId}/model-profiles/${existing?.id}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      onCreated();
      onClose();
    },
    onError: () => setConfirmDelete(false),
  });

  const pricesKnown = Boolean(inputCost || outputCost);
  const selfHosted = isSelfHostedProvider(providerType);
  // Empty price fields on a self-hosted provider are not a gap: the profile
  // resolves to $0. The note under the fields says so, and they stay
  // editable for the endpoint that does bill.
  const priceNote = pricingNote ?? selfHostedPriceNote(providerType);
  // List prices age. Saying how old they are is the difference between a
  // number the admin can trust and one they should go check. A self-hosted
  // model has no list price to have aged, so the nudge would only confuse.
  const catalogStaleness = selfHosted ? null : catalogStalenessNote(catalogUpdated);
  const summary = pricesKnown
    ? `$${inputCost || "0"} in · $${outputCost || "0"} out per 1M tokens`
    : selfHosted
      ? "Free (self-hosted) — no per-token price."
      : "No prices yet — runs will show $0.00 until you add them.";
  // Provenance describes the stored price, not the unsaved form inputs.
  const storedPriced = Boolean(
    existing &&
      existing.input_cost_micros_per_million !== null &&
      existing.output_cost_micros_per_million !== null,
  );
  const storedSource = existing ? effectivePriceSource(existing) : null;
  const storedBadge = existing ? priceSourceBadge(storedSource, storedPriced) : null;

  const dialogError = refreshPrices.error
    ? errorText(refreshPrices.error, "Refreshing prices failed.")
    : remove.error
      ? errorText(remove.error, "Deleting the profile failed.")
      : create.error
        ? errorText(
            create.error,
            existing ? "Saving the profile failed." : "Creating the profile failed.",
          )
        : null;

  return (
    <Dialog title={existing ? "Edit model profile" : "New model profile"} open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Provider">
          <Select
            value={providerId}
            onChange={(e) => {
              const nextType = providers.find((p) => p.id === e.target.value)?.type;
              setProviderId(e.target.value);
              setAutofilledFor(null);
              // The note described the previous provider's prices.
              setPricingNote(null);
              // Self-hosted means $0 as soon as it is chosen (see startsFree);
              // leaving it clears that $0 so a cloud model is never saved as
              // free by accident. Only a new profile: an edit keeps its row.
              if (!existing && nextType !== undefined) {
                if (isSelfHostedProvider(nextType)) {
                  setInputCost("0");
                  setOutputCost("0");
                } else if (selfHosted) {
                  setInputCost("");
                  setOutputCost("");
                }
              }
            }}
            required
          >
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Profile name" hint="Shown wherever a model is selected.">
          <Input
            required
            maxLength={200}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="GPT-5 mini (cheap default)"
          />
        </Field>
        <Field
          label="Model"
          hint={
            providerModels.isPending
              ? "Loading the provider's model list…"
              : entries && entries.length > 0
                ? `${entries.length} models available — pick one or type an identifier.`
                : providerModels.data?.detail
                  ? `Couldn't list models (${providerModels.data.detail}). Type the exact identifier.`
                  : "The exact model identifier the provider expects."
          }
        >
          <Input
            required
            maxLength={200}
            list="profile-model-options"
            value={modelName}
            onChange={(e) => {
              setModelName(e.target.value);
              applyAutofill(e.target.value, entries);
            }}
            placeholder="gpt-5-mini"
          />
          <datalist id="profile-model-options">
            {(entries ?? []).map((model) => (
              <option key={model.id} value={model.id} />
            ))}
          </datalist>
        </Field>

        <div className="rounded-xl border border-line px-3 py-2.5">
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              aria-label="Model's built-in web search"
              checked={webSearchEnabled && webSupport.supported}
              disabled={!webSupport.supported}
              onChange={(e) => setWebSearchEnabled(e.target.checked)}
            />
            <span>
              Model&apos;s built-in web search
              <span className="block text-xs text-dim">
                {webSupport.supported
                  ? "The provider searches the web inside the model call and cites sources in the reply. No tool grant is involved."
                  : webSupport.reason}
              </span>
            </span>
          </label>
        </div>

        <div className="rounded-xl border border-line">
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-ink"
            aria-expanded={pricingOpen}
            aria-controls={pricingPanelId}
            onClick={() => setPricingOpen((open) => !open)}
          >
            {pricingOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <span className="font-medium">Pricing (auto-filled)</span>
            <span className="ml-auto truncate text-xs text-dim">{summary}</span>
          </button>
          {pricingOpen ? (
            <div id={pricingPanelId} className="space-y-3 border-t border-line px-3 py-3">
              {priceNote ? (
                <p className="text-xs text-dim" data-testid="dialog-price-note">
                  {priceNote}
                </p>
              ) : null}
              {!pricesKnown && modelName.trim() && !selfHosted ? (
                <UnpricedModelNote modelName={modelName.trim()} providerType={providerType} />
              ) : null}
              {catalogStaleness ? (
                <p data-testid="dialog-catalog-staleness" className="text-xs text-warn">
                  {catalogStaleness}
                </p>
              ) : null}
              <div className="grid grid-cols-2 gap-3">
                <Field label="Input $ / 1M tokens">
                  <Input
                    type="number"
                    min="0"
                    step="0.000001"
                    value={inputCost}
                    onChange={(e) => setInputCost(e.target.value)}
                    // A cloud example price is a wrong hint on a host that
                    // has none, even once the field has been cleared.
                    placeholder={selfHosted ? "0" : "0.15"}
                  />
                </Field>
                <Field label="Output $ / 1M tokens">
                  <Input
                    type="number"
                    min="0"
                    step="0.000001"
                    value={outputCost}
                    onChange={(e) => setOutputCost(e.target.value)}
                    placeholder={selfHosted ? "0" : "0.60"}
                  />
                </Field>
              </div>
              <Field label="Context window (tokens)" hint="Optional.">
                <Input
                  type="number"
                  min="1"
                  step="1"
                  value={contextWindow}
                  onChange={(e) => setContextWindow(e.target.value)}
                  placeholder="128000"
                />
              </Field>
              {existing && storedBadge ? (
                <div className="space-y-1 text-[11px] text-faint" data-testid="price-provenance">
                  <p>
                    <Badge tone={storedBadge.tone}>{storedBadge.text}</Badge>{" "}
                    {pricing?.price_source_label ?? priceSourceLabel(storedSource, storedPriced)}
                  </p>
                  {pricing?.observed ? (
                    <p>
                      Measured: {observedRateSummary(pricing.observed)} —{" "}
                      {derivationLabel(pricing.observed.derivation)}
                    </p>
                  ) : null}
                  {pricing?.suggestion ? (
                    <p>
                      {pricing.suggestion_label} suggests{" "}
                      {formatPricePair(
                        pricing.suggestion.input_cost_micros_per_million,
                        pricing.suggestion.output_cost_micros_per_million,
                      )}
                      .
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : priceNote ? (
            <p className="border-t border-line px-3 py-2 text-xs text-dim">{priceNote}</p>
          ) : null}
        </div>

        {refreshNote ? <p className="text-xs text-faint">{refreshNote}</p> : null}
        <ErrorNote message={dialogError} />
        <div className="flex flex-wrap items-center gap-2">
          {existing ? (
            <>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                title="Look the price up again from the provider or the public price list"
                disabled={refreshPrices.isPending}
                onClick={() => refreshPrices.mutate()}
              >
                <RefreshCw size={12} className={refreshPrices.isPending ? "animate-spin" : ""} />{" "}
                Refresh prices
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="text-danger"
                disabled={remove.isPending}
                onClick={() => setConfirmDelete(true)}
              >
                Delete
              </Button>
            </>
          ) : null}
          <div className="ml-auto flex gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {create.isPending ? "Saving…" : existing ? "Save changes" : "Create profile"}
            </Button>
          </div>
        </div>
      </form>

      {existing ? (
        <ConfirmDialog
          open={confirmDelete}
          title={`Delete profile “${existing.display_name}”?`}
          body={
            isDefault
              ? "It is the workspace default; agents will have no default model until you pick another."
              : "This cannot be undone."
          }
          confirmLabel="Delete profile"
          busy={remove.isPending}
          onConfirm={() => remove.mutate()}
          onClose={() => setConfirmDelete(false)}
        />
      ) : null}
    </Dialog>
  );
}
