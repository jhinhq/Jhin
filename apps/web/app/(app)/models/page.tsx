"use client";

/** Models page (plan 15, 17.2 nav): the decisions first — the workspace
 * default model, then the model cards, then simplified provider cards — with
 * the machinery (verify, balances, price provenance, the pricing panel) in
 * dialogs and one Advanced disclosure. API keys are stored once in the
 * encrypted secret store and only referenced here. */

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, ChevronRight, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { useId, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Disclosure, LoadError } from "@/components/company/bits";
import { ChangeDefaultDialog } from "@/components/models/change-default-dialog";
import { DefaultModelCard } from "@/components/models/default-model-card";
import { ProfileCard } from "@/components/models/profile-card";
import { ProviderCard } from "@/components/models/provider-card";
import { ProviderManageDialog } from "@/components/models/provider-manage-dialog";
import { PricingPanel } from "@/components/pricing-panel";
import { SpendTile } from "@/components/spend-tile";
import { UnpricedModelNote } from "@/components/unpriced-model-note";
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  useInvalidateModels,
  useModelProfiles,
  useModelProviders,
  useProviderModels,
  usePricingStatus,
  useSecrets,
  useWorkspaceDetail,
  useWorkspaceSpend,
} from "@/lib/hooks";
import {
  autofillForModel,
  buildProfileConfig,
  catalogStalenessNote,
  derivationLabel,
  dollarInputToMicros,
  formatPricePair,
  microsToDollarInput,
  observedRateSummary,
  priceSourceBadge,
  priceSourceLabel,
  webSearchSupport,
  type ProfilePrefill,
} from "@/lib/models";
import type {
  CatalogRefreshResult,
  ModelProfile,
  ModelProvider,
  ModelProviderType,
  ProfilePricing,
  ProfilePricingRefresh,
  ReconcilePricingResult,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const PROVIDER_TYPES: { value: ModelProviderType; label: string; needsKey: boolean }[] = [
  { value: "openai", label: "OpenAI", needsKey: true },
  { value: "anthropic", label: "Anthropic", needsKey: true },
  { value: "openrouter", label: "OpenRouter", needsKey: true },
  { value: "ollama", label: "Ollama (local)", needsKey: false },
  { value: "openai_compatible", label: "OpenAI-compatible endpoint", needsKey: false },
];

function providerTypeLabel(type: ModelProviderType): string {
  return PROVIDER_TYPES.find((t) => t.value === type)?.label ?? type;
}

function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

export default function ModelsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  // The providers listing is admin-only server-side; asking anyway would turn
  // every member's visit into a 403 reported as a load failure.
  const providers = useModelProviders(workspaceId, isAdmin);
  const profiles = useModelProfiles(workspaceId);
  const detail = useWorkspaceDetail(workspaceId);
  const spend = useWorkspaceSpend(workspaceId);
  const invalidate = useInvalidateModels(workspaceId);

  const [providerDialog, setProviderDialog] = useState(false);
  const [editingProviderId, setEditingProviderId] = useState<string | null>(null);
  const [managingProviderId, setManagingProviderId] = useState<string | null>(null);
  const [adminKeyProviderId, setAdminKeyProviderId] = useState<string | null>(null);
  const [profileDialog, setProfileDialog] = useState(false);
  // A local model handed over from the Ollama panel: the profile dialog
  // opens already filled in, so nothing is retyped from the host's listing.
  const [profilePrefill, setProfilePrefill] = useState<ProfilePrefill | null>(null);
  const [editingProfile, setEditingProfile] = useState<ModelProfile | null>(null);
  const [changeDefaultOpen, setChangeDefaultOpen] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [pricingError, setPricingError] = useState<string | null>(null);
  const [reconcileResult, setReconcileResult] = useState<ReconcilePricingResult | null>(null);
  const [catalogResult, setCatalogResult] = useState<CatalogRefreshResult | null>(null);

  const pricing = usePricingStatus(workspaceId);
  const pricingByProfile = new Map<string, ProfilePricing>(
    (pricing.data?.profiles ?? []).map((row) => [row.profile_id, row]),
  );

  const reconcile = useMutation({
    mutationFn: () =>
      api<ReconcilePricingResult>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/reconcile-pricing`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setPricingError(null);
      setReconcileResult(result);
      invalidate();
    },
    onError: (error) => setPricingError(errText(error, "Measuring real rates failed.")),
  });

  const refreshCatalog = useMutation({
    mutationFn: () =>
      api<CatalogRefreshResult>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/refresh-catalog`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setPricingError(null);
      setCatalogResult(result);
      invalidate();
    },
    onError: (error) => setPricingError(errText(error, "Refreshing the price catalog failed.")),
  });

  // A disabled query stays pending forever, so the non-admin path must not
  // wait on providers.
  if ((isAdmin && providers.isPending) || profiles.isPending || detail.isPending) {
    return (
      <>
        <PageHeader title="Models" />
        <PageBody>
          <Spinner label="Loading model configuration…" />
        </PageBody>
      </>
    );
  }

  if ((isAdmin && providers.isError) || profiles.isError || detail.isError) {
    return (
      <>
        <PageHeader title="Models" />
        <PageBody>
          <LoadError
            what="your model setup"
            onRetry={() => {
              if (isAdmin) void providers.refetch();
              void profiles.refetch();
              void detail.refetch();
            }}
          />
        </PageBody>
      </>
    );
  }

  const providerList = providers.data ?? [];
  const profileList = profiles.data ?? [];
  const defaultProfileId = detail.data?.default_model_profile_id ?? null;
  const defaultProfile = profileList.find((profile) => profile.id === defaultProfileId) ?? null;
  const defaultProvider = defaultProfile
    ? (providerList.find((provider) => provider.id === defaultProfile.provider_id) ?? null)
    : null;
  // Dialogs hold ids, not rows, so a verify or edit re-renders them with the
  // freshly invalidated data instead of a snapshot from when they opened.
  const managingProvider = providerList.find((p) => p.id === managingProviderId) ?? null;
  const editingProvider = providerList.find((p) => p.id === editingProviderId) ?? null;
  const adminKeyProvider = providerList.find((p) => p.id === adminKeyProviderId) ?? null;

  return (
    <>
      <PageHeader
        title="Models"
        description="Choose the AI models your agents think with."
        actions={
          isAdmin ? (
            <>
              <Button onClick={() => setProviderDialog(true)}>
                <Plus size={14} /> Add provider
              </Button>
              <Button
                variant="primary"
                disabled={providerList.length === 0}
                onClick={() => setProfileDialog(true)}
              >
                <Plus size={14} /> New profile
              </Button>
            </>
          ) : null
        }
      />
      <PageBody className="space-y-8">
        <ErrorNote message={pageError} />

        {spend.data ? <SpendTile spend={spend.data} /> : null}

        <section>
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">
            Default model
          </h2>
          <DefaultModelCard
            profile={defaultProfile}
            provider={defaultProvider}
            isAdmin={isAdmin}
            onChange={() => setChangeDefaultOpen(true)}
          />
        </section>

        <section>
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">
            Models
          </h2>
          {profileList.length === 0 ? (
            <EmptyState
              title="No model profiles yet"
              description="A profile is a named model on a provider with pricing — agents reference profiles, never raw providers."
            />
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {profileList.map((profile) => (
                <ProfileCard
                  key={profile.id}
                  profile={profile}
                  provider={providerList.find((p) => p.id === profile.provider_id)}
                  isDefault={profile.id === defaultProfileId}
                  isAdmin={isAdmin}
                  workspaceId={workspaceId}
                  pricing={pricingByProfile.get(profile.id)}
                  pricingPages={pricing.data?.pricing_pages}
                  onChanged={invalidate}
                  onError={setPageError}
                  onEdit={setEditingProfile}
                />
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">
            Providers
          </h2>
          {!isAdmin ? (
            <p className="text-sm text-dim">
              Provider accounts and API keys are managed by workspace admins.
            </p>
          ) : providerList.length === 0 ? (
            <EmptyState
              title="No model providers yet"
              description="Connect OpenAI, Anthropic, OpenRouter, Ollama, or any OpenAI-compatible endpoint. API keys are envelope-encrypted at rest and never shown again."
              action={
                isAdmin ? (
                  <Button variant="primary" onClick={() => setProviderDialog(true)}>
                    <Plus size={14} /> Add first provider
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {providerList.map((provider) => (
                <ProviderCard
                  key={provider.id}
                  provider={provider}
                  typeLabel={providerTypeLabel(provider.type)}
                  profileCount={profileList.filter((p) => p.provider_id === provider.id).length}
                  onManage={() => setManagingProviderId(provider.id)}
                />
              ))}
            </div>
          )}
        </section>

        <Disclosure
          label="Advanced — where prices come from"
          openLabel="Hide where prices come from"
        >
          <PricingPanel
            status={pricing.data}
            isPending={pricing.isPending}
            isAdmin={isAdmin}
            onReconcile={() => reconcile.mutate()}
            onRefreshCatalog={() => refreshCatalog.mutate()}
            reconciling={reconcile.isPending}
            refreshing={refreshCatalog.isPending}
            reconcileResult={reconcileResult}
            catalogResult={catalogResult}
            error={pricingError}
          />
        </Disclosure>
      </PageBody>

      {providerDialog ? (
        <ProviderDialog
          workspaceId={workspaceId}
          onClose={() => setProviderDialog(false)}
          onCreated={invalidate}
        />
      ) : null}
      {editingProvider ? (
        <ProviderDialog
          workspaceId={workspaceId}
          existing={editingProvider}
          onClose={() => setEditingProviderId(null)}
          onCreated={invalidate}
        />
      ) : null}
      {managingProvider ? (
        <ProviderManageDialog
          workspaceId={workspaceId}
          provider={managingProvider}
          typeLabel={providerTypeLabel(managingProvider.type)}
          profileCount={profileList.filter((p) => p.provider_id === managingProvider.id).length}
          isDefaultProvider={profileList.some(
            (p) => p.provider_id === managingProvider.id && p.id === defaultProfileId,
          )}
          isAdmin={isAdmin}
          onClose={() => setManagingProviderId(null)}
          onChanged={invalidate}
          onEdit={() => setEditingProviderId(managingProvider.id)}
          onAddAdminKey={() => setAdminKeyProviderId(managingProvider.id)}
          onUseAsModel={(prefill) => {
            setManagingProviderId(null);
            setProfilePrefill(prefill);
          }}
        />
      ) : null}
      {adminKeyProvider ? (
        <AdminKeyDialog
          workspaceId={workspaceId}
          provider={adminKeyProvider}
          onClose={() => setAdminKeyProviderId(null)}
          onSaved={() => {
            setAdminKeyProviderId(null);
            invalidate();
          }}
        />
      ) : null}
      {profileDialog ? (
        <ProfileDialog
          workspaceId={workspaceId}
          providers={providerList}
          onClose={() => setProfileDialog(false)}
          onCreated={invalidate}
        />
      ) : null}
      {profilePrefill ? (
        <ProfileDialog
          workspaceId={workspaceId}
          providers={providerList}
          prefill={profilePrefill}
          onClose={() => setProfilePrefill(null)}
          onCreated={invalidate}
        />
      ) : null}
      {editingProfile ? (
        <ProfileDialog
          workspaceId={workspaceId}
          providers={providerList}
          existing={editingProfile}
          isDefault={editingProfile.id === defaultProfileId}
          pricing={pricingByProfile.get(editingProfile.id)}
          onClose={() => setEditingProfile(null)}
          onCreated={invalidate}
        />
      ) : null}
      {changeDefaultOpen ? (
        <ChangeDefaultDialog
          workspaceId={workspaceId}
          profiles={profileList}
          providers={providerList}
          currentDefaultId={defaultProfileId}
          onClose={() => setChangeDefaultOpen(false)}
          onChanged={invalidate}
        />
      ) : null}
    </>
  );
}

/** Save an API key under `baseName`, picking a numbered variant when a
 * secret with that name already exists (e.g. a provider deleted and re-added
 * with the same display name). Users never chose the name, so a conflict
 * should not surface as an error. */
async function storeApiKey(workspaceId: string, baseName: string, value: string): Promise<string> {
  for (let attempt = 1; attempt <= 20; attempt += 1) {
    const name = attempt === 1 ? baseName : `${baseName} (${attempt})`;
    try {
      const secret = await api<{ id: string }>(`/api/v1/workspaces/${workspaceId}/secrets`, {
        method: "POST",
        body: { name, value, type: "api_key" },
      });
      return secret.id;
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error;
    }
  }
  throw new ApiError(409, "Too many secrets share this name. Rename the provider and try again.");
}

function ProviderDialog({
  workspaceId,
  existing,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  /** When set, the dialog edits this provider (PATCH) instead of creating one. */
  existing?: ModelProvider;
  onClose: () => void;
  onCreated: () => void;
}) {
  const secrets = useSecrets(workspaceId);
  const editing = existing !== undefined;
  const [type, setType] = useState<ModelProviderType>(existing?.type ?? "openai");
  const [displayName, setDisplayName] = useState(existing?.display_name ?? "");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "");
  const [keyMode, setKeyMode] = useState<"keep" | "new" | "existing" | "none">(
    existing ? (existing.secret_id ? "keep" : "none") : "new",
  );
  const [apiKey, setApiKey] = useState("");
  const [secretId, setSecretId] = useState("");
  const [adminKey, setAdminKey] = useState("");

  const typeMeta = PROVIDER_TYPES.find((t) => t.value === type)!;

  // The provider can only be saved after a live check of exactly these inputs.
  const draftKey = JSON.stringify({ type, baseUrl: baseUrl.trim(), keyMode, apiKey, secretId });
  const [verified, setVerified] = useState<{ key: string; detail: string } | null>(null);
  const verifiedForCurrent = verified?.key === draftKey;
  const draftSecretId =
    keyMode === "existing" && secretId
      ? secretId
      : keyMode === "keep"
        ? (existing?.secret_id ?? null)
        : null;
  const test = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; detail: string }>(
        `/api/v1/workspaces/${workspaceId}/model-providers/verify-draft`,
        {
          method: "POST",
          body: {
            type,
            base_url: baseUrl.trim() || null,
            api_key: keyMode === "new" && apiKey.trim() ? apiKey.trim() : null,
            secret_id: draftSecretId,
          },
        },
      ),
    onSuccess: (result) => {
      setVerified(result.ok ? { key: draftKey, detail: result.detail } : null);
      setTestFailure(result.ok ? null : result.detail);
    },
    onError: (error) => setTestFailure(errText(error, "The connection test failed.")),
  });
  const [testFailure, setTestFailure] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      if (!verifiedForCurrent) {
        throw new ApiError(400, "Test the connection before saving.");
      }
      let resolvedSecretId: string | null = null;
      if (keyMode === "new" && apiKey.trim()) {
        // Store the key in the encrypted secret store first, then reference it.
        resolvedSecretId = await storeApiKey(
          workspaceId,
          `${displayName.trim() || type} API key`,
          apiKey.trim(),
        );
      } else if (keyMode === "existing" && secretId) {
        resolvedSecretId = secretId;
      }
      if (existing) {
        const body: Record<string, unknown> = {
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
        };
        // "Keep the current key" sends nothing, so the stored secret survives.
        if (keyMode !== "keep") body.secret_id = resolvedSecretId;
        await api<ModelProvider>(
          `/api/v1/workspaces/${workspaceId}/model-providers/${existing.id}`,
          { method: "PATCH", body },
        );
        // Stamp last_verified_at on the saved row (same check that just passed).
        await api(`/api/v1/workspaces/${workspaceId}/model-providers/${existing.id}/verify`, {
          method: "POST",
        }).catch(() => undefined);
        return existing;
      }
      let adminSecretId: string | null = null;
      if (type === "openai" && adminKey.trim()) {
        adminSecretId = await storeApiKey(
          workspaceId,
          `${displayName.trim() || type} admin key`,
          adminKey.trim(),
        );
      }
      const provider = await api<ModelProvider>(
        `/api/v1/workspaces/${workspaceId}/model-providers`,
        {
          method: "POST",
          body: {
            type,
            display_name: displayName.trim(),
            base_url: baseUrl.trim() || null,
            secret_id: resolvedSecretId,
            admin_secret_id: adminSecretId,
          },
        },
      );
      // Stamp last_verified_at on the saved row (same check that just passed).
      await api(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}/verify`, {
        method: "POST",
      }).catch(() => undefined);
      return provider;
    },
    onSuccess: () => {
      onCreated();
      onClose();
    },
  });

  return (
    <Dialog title={editing ? "Edit model provider" : "Add model provider"} open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Provider type">
          <Select
            value={type}
            disabled={editing}
            onChange={(e) => setType(e.target.value as ModelProviderType)}
          >
            {PROVIDER_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Display name">
          <Input
            required
            maxLength={200}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="OpenAI (production)"
          />
        </Field>
        <Field
          label="Base URL"
          hint={
            type === "ollama"
              ? "Defaults to http://localhost:11434/v1 when empty."
              : type === "openai_compatible"
                ? "Required: the /v1 root of the endpoint."
                : "Leave empty for the official endpoint."
          }
        >
          <Input
            maxLength={500}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://…/v1"
            required={type === "openai_compatible"}
          />
        </Field>
        <Field
          label="API key"
          hint="Stored encrypted (AES-256-GCM envelope); it is never displayed again."
        >
          <div className="space-y-2">
            <Select
              value={keyMode}
              onChange={(e) => setKeyMode(e.target.value as typeof keyMode)}
            >
              {editing && existing?.secret_id ? (
                <option value="keep">Keep the current key</option>
              ) : null}
              <option value="new">Enter a new key</option>
              <option value="existing">Use an existing secret</option>
              <option value="none">No key{typeMeta.needsKey ? "" : " (local endpoint)"}</option>
            </Select>
            {keyMode === "new" ? (
              <Input
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-…"
                required={typeMeta.needsKey}
              />
            ) : null}
            {keyMode === "existing" ? (
              <Select value={secretId} onChange={(e) => setSecretId(e.target.value)} required>
                <option value="">Choose a secret…</option>
                {(secrets.data ?? []).map((secret) => (
                  <option key={secret.id} value={secret.id}>
                    {secret.name} ({secret.masked_hint})
                  </option>
                ))}
              </Select>
            ) : null}
          </div>
        </Field>
        {!editing && type === "openai" ? (
          <Field
            label="Admin key (for spend reporting)"
            hint={ADMIN_KEY_HINT}
          >
            <Input
              type="password"
              autoComplete="off"
              value={adminKey}
              onChange={(e) => setAdminKey(e.target.value)}
              placeholder="sk-admin-… (optional)"
            />
          </Field>
        ) : null}
        {verifiedForCurrent ? (
          <p
            role="status"
            className="flex items-start gap-2 rounded-xl border border-ok/30 bg-ok-soft px-3 py-2 text-sm text-ok"
          >
            <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden />
            <span>Connection verified — {verified?.detail}</span>
          </p>
        ) : (
          <ErrorNote message={testFailure} />
        )}
        <ErrorNote
          message={errText(
            save.error,
            editing ? "Saving the provider failed." : "Creating the provider failed.",
          )}
        />
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => {
              setTestFailure(null);
              test.mutate();
            }}
            disabled={test.isPending || (keyMode === "new" && typeMeta.needsKey && !apiKey.trim())}
          >
            <ShieldCheck size={13} /> {test.isPending ? "Testing…" : "Test connection"}
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={save.isPending || !verifiedForCurrent}
            title={verifiedForCurrent ? undefined : "Test the connection first"}
          >
            {save.isPending ? (editing ? "Saving…" : "Adding…") : editing ? "Save changes" : "Add provider"}
          </Button>
        </div>
        {!verifiedForCurrent ? (
          <p className="text-xs text-faint">
            Run “Test connection” with these settings to enable saving. Changing any field
            requires a new test.
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

function ProfileDialog({
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
  const [providerId, setProviderId] = useState(
    existing?.provider_id ?? prefill?.providerId ?? providers[0]?.id ?? "",
  );
  const [displayName, setDisplayName] = useState(
    existing?.display_name ?? prefill?.displayName ?? "",
  );
  const [modelName, setModelName] = useState(existing?.model_name ?? prefill?.modelName ?? "");
  const [inputCost, setInputCost] = useState(
    microsToDollarInput(existing?.input_cost_micros_per_million ?? prefill?.inputCostMicros ?? null),
  );
  const [outputCost, setOutputCost] = useState(
    microsToDollarInput(existing?.output_cost_micros_per_million ?? prefill?.outputCostMicros ?? null),
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
  // re-renders. A prefill counts as already filled: the provider's model
  // list arriving later must not overwrite what the panel handed over.
  const [autofilledFor, setAutofilledFor] = useState<string | null>(
    prefill ? `${prefill.providerId}:${prefill.modelName.trim().toLowerCase()}` : null,
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
  // List prices age. Saying how old they are is the difference between a
  // number the admin can trust and one they should go check. A local Ollama
  // model has no list price to have aged, so the nudge would only confuse.
  const catalogStaleness = providerType === "ollama" ? null : catalogStalenessNote(catalogUpdated);
  const summary = pricesKnown
    ? `$${inputCost || "0"} in · $${outputCost || "0"} out per 1M tokens`
    : "No prices yet — runs will show $0.00 until you add them.";
  // Provenance describes the stored price, not the unsaved form inputs.
  const storedPriced = Boolean(
    existing &&
      existing.input_cost_micros_per_million !== null &&
      existing.output_cost_micros_per_million !== null,
  );
  const storedBadge = existing ? priceSourceBadge(existing.price_source, storedPriced) : null;

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
              setProviderId(e.target.value);
              setAutofilledFor(null);
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
              {pricingNote ? <p className="text-xs text-dim">{pricingNote}</p> : null}
              {!pricesKnown && modelName.trim() ? (
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
                    placeholder="0.15"
                  />
                </Field>
                <Field label="Output $ / 1M tokens">
                  <Input
                    type="number"
                    min="0"
                    step="0.000001"
                    value={outputCost}
                    onChange={(e) => setOutputCost(e.target.value)}
                    placeholder="0.60"
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
                    {pricing?.price_source_label ??
                      priceSourceLabel(existing.price_source, storedPriced)}
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
          ) : pricingNote ? (
            <p className="border-t border-line px-3 py-2 text-xs text-dim">{pricingNote}</p>
          ) : null}
        </div>

        {refreshNote ? <p className="text-xs text-faint">{refreshNote}</p> : null}
        <ErrorNote
          message={
            errText(refreshPrices.error, "Refreshing prices failed.") ??
            errText(remove.error, "Deleting the profile failed.") ??
            errText(
              create.error,
              existing ? "Saving the profile failed." : "Creating the profile failed.",
            )
          }
        />
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

const ADMIN_KEY_HINT =
  "Optional. OpenAI has no balance API; an admin key lets Jhin read month-to-date spend. Create one in the OpenAI dashboard → Settings → Organization → Admin keys. Stored encrypted, never displayed.";

function AdminKeyDialog({
  workspaceId,
  provider,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  provider: ModelProvider;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [adminKey, setAdminKey] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      const secretId = await storeApiKey(workspaceId, `${provider.display_name} admin key`, adminKey.trim());
      await api<ModelProvider>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "PATCH",
        body: { admin_secret_id: secretId },
      });
    },
    onSuccess: onSaved,
  });
  return (
    <Dialog title="Add OpenAI admin key" open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Admin key" hint={ADMIN_KEY_HINT}>
          <Input
            type="password"
            autoComplete="off"
            required
            value={adminKey}
            onChange={(e) => setAdminKey(e.target.value)}
            placeholder="sk-admin-…"
          />
        </Field>
        <ErrorNote message={errText(save.error, "Saving the admin key failed.")} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending || !adminKey.trim()}>
            {save.isPending ? "Saving…" : "Save admin key"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
