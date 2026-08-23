"use client";

/** Models page (plan 15, 17.2 nav): provider cards with verify, model
 * profiles with pricing, and the workspace default profile. API keys are
 * stored once in the encrypted secret store and only referenced here. */

import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  KeyRound,
  Plus,
  RefreshCw,
  ShieldCheck,
  Wallet,
  XCircle,
} from "lucide-react";
import { useId, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import {
  useInvalidateModels,
  useModelProfiles,
  useModelProviders,
  useProviderBalance,
  useProviderModels,
  useSecrets,
  useWorkspaceDetail,
  useWorkspaceSpend,
} from "@/lib/hooks";
import {
  autofillForModel,
  balanceSourceLabel,
  dollarInputToMicros,
  formatMicrosAsDollars,
  microsToDollarInput,
  summarizeBudget,
} from "@/lib/models";
import type {
  ModelProfile,
  ModelProvider,
  ModelProviderType,
  ProfilePricingRefresh,
  WorkspaceSpend,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const PROVIDER_TYPES: { value: ModelProviderType; label: string; needsKey: boolean }[] = [
  { value: "openai", label: "OpenAI", needsKey: true },
  { value: "anthropic", label: "Anthropic", needsKey: true },
  { value: "openrouter", label: "OpenRouter", needsKey: true },
  { value: "ollama", label: "Ollama (local)", needsKey: false },
  { value: "openai_compatible", label: "OpenAI-compatible endpoint", needsKey: false },
];

function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

export default function ModelsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  const providers = useModelProviders(workspaceId);
  const profiles = useModelProfiles(workspaceId);
  const detail = useWorkspaceDetail(workspaceId);
  const spend = useWorkspaceSpend(workspaceId);
  const invalidate = useInvalidateModels(workspaceId);

  const [providerDialog, setProviderDialog] = useState(false);
  const [profileDialog, setProfileDialog] = useState(false);
  const [editingProfile, setEditingProfile] = useState<ModelProfile | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  if (providers.isPending || profiles.isPending) {
    return (
      <>
        <PageHeader title="Models" />
        <PageBody>
          <Spinner label="Loading model configuration…" />
        </PageBody>
      </>
    );
  }

  const providerList = providers.data ?? [];
  const profileList = profiles.data ?? [];
  const defaultProfileId = detail.data?.default_model_profile_id ?? null;

  return (
    <>
      <PageHeader
        title="Models"
        description="Connect AI providers, name the models your agents can use, and pick a workspace default."
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
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">Providers</h2>
          {providerList.length === 0 ? (
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
                  isAdmin={isAdmin}
                  workspaceId={workspaceId}
                  profileCount={profileList.filter((p) => p.provider_id === provider.id).length}
                  isDefaultProvider={profileList.some(
                    (p) => p.provider_id === provider.id && p.id === defaultProfileId,
                  )}
                  onChanged={invalidate}
                  onError={setPageError}
                />
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">Model profiles</h2>
          {profileList.length === 0 ? (
            <EmptyState
              title="No model profiles yet"
              description="A profile is a named model on a provider with pricing — agents reference profiles, never raw providers."
            />
          ) : (
            <div className="overflow-x-auto rounded-2xl border border-line bg-surface shadow-card">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-xs font-medium uppercase tracking-wider text-faint">
                  <tr>
                    <th className="px-4 py-3">Profile</th>
                    <th className="px-4 py-3">Model</th>
                    <th className="px-4 py-3">Provider</th>
                    <th className="px-4 py-3">Cost / 1M in · out</th>
                    <th className="px-4 py-3">Default</th>
                  </tr>
                </thead>
                <tbody>
                  {profileList.map((profile) => (
                    <ProfileRow
                      key={profile.id}
                      profile={profile}
                      provider={providerList.find((p) => p.id === profile.provider_id)}
                      isDefault={profile.id === defaultProfileId}
                      isAdmin={isAdmin}
                      workspaceId={workspaceId}
                      onChanged={invalidate}
                      onError={setPageError}
                      onEdit={setEditingProfile}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </PageBody>

      {providerDialog ? (
        <ProviderDialog
          workspaceId={workspaceId}
          onClose={() => setProviderDialog(false)}
          onCreated={invalidate}
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
      {editingProfile ? (
        <ProfileDialog
          workspaceId={workspaceId}
          providers={providerList}
          existing={editingProfile}
          onClose={() => setEditingProfile(null)}
          onCreated={invalidate}
        />
      ) : null}
    </>
  );
}

function ProviderCard({
  provider,
  isAdmin,
  workspaceId,
  profileCount,
  isDefaultProvider,
  onChanged,
  onError,
}: {
  provider: ModelProvider;
  isAdmin: boolean;
  workspaceId: string;
  profileCount: number;
  isDefaultProvider: boolean;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  const [verifyResult, setVerifyResult] = useState<{ ok: boolean; detail: string } | null>(null);

  const verify = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; detail: string }>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}/verify`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setVerifyResult(result);
      onChanged();
    },
    onError: (error) => onError(errText(error, "Verification failed.")),
  });

  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      onError(null);
      onChanged();
    },
  });
  const removeError = errText(remove.error, "Deleting the provider failed.");

  const typeLabel =
    PROVIDER_TYPES.find((t) => t.value === provider.type)?.label ?? provider.type;

  return (
    <article className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 truncate font-display text-sm font-semibold text-ink">
            <Cpu size={14} className="shrink-0 text-accent-strong" aria-hidden /> {provider.display_name}
          </h3>
          <p className="mt-0.5 text-xs text-dim">{typeLabel}</p>
        </div>
        <Badge tone={provider.enabled ? "ok" : "neutral"}>
          {provider.enabled ? "enabled" : "disabled"}
        </Badge>
      </header>

      <dl className="space-y-1 text-xs text-dim">
        {provider.base_url ? (
          <div className="truncate">
            <dt className="inline text-faint">Endpoint: </dt>
            <dd className="inline font-mono">{provider.base_url}</dd>
          </div>
        ) : null}
        <div>
          <dt className="inline text-faint">Credential: </dt>
          <dd className="inline">
            {provider.secret_id ? (
              <span className="inline-flex items-center gap-1">
                <KeyRound size={11} /> encrypted secret
              </span>
            ) : (
              "none"
            )}
          </dd>
        </div>
        <div>
          <dt className="inline text-faint">Profiles: </dt>
          <dd className="inline">{profileCount}</dd>
        </div>
        <div>
          <dt className="inline text-faint">Last verified: </dt>
          <dd className="inline">
            {provider.last_verified_at ? formatDateTime(provider.last_verified_at) : "never"}
          </dd>
        </div>
      </dl>

      {verifyResult ? (
        <p
          className={`flex items-start gap-1.5 rounded-xl px-3 py-2 text-xs ${
            verifyResult.ok
              ? "bg-ok-soft text-ok"
              : "bg-danger-soft text-danger"
          }`}
        >
          {verifyResult.ok ? (
            <CheckCircle2 size={13} className="mt-0.5 shrink-0" />
          ) : (
            <XCircle size={13} className="mt-0.5 shrink-0" />
          )}
          <span className="min-w-0 break-words">{verifyResult.detail}</span>
        </p>
      ) : provider.last_error ? (
        <p className="rounded-xl bg-danger-soft px-3 py-2 text-xs text-danger">
          {provider.last_error}
        </p>
      ) : null}

      {removeError ? (
        <p role="alert" className="rounded-xl bg-danger-soft px-3 py-2 text-xs text-danger">
          {removeError}
        </p>
      ) : null}

      <BalanceBlock
        provider={provider}
        isAdmin={isAdmin}
        workspaceId={workspaceId}
        onChanged={onChanged}
        onError={onError}
      />

      {isAdmin ? (
        <footer className="mt-auto flex items-center gap-2 border-t border-line pt-3">
          <Button size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
            <ShieldCheck size={13} /> {verify.isPending ? "Verifying…" : "Verify"}
          </Button>
          <Button
            size="sm"
            variant="danger"
            className="ml-auto"
            disabled={remove.isPending}
            onClick={() => {
              const extra =
                profileCount > 0
                  ? ` This also removes its ${profileCount === 1 ? "model profile" : `${profileCount} model profiles`}${isDefaultProvider ? " and clears the workspace default" : ""}.`
                  : "";
              if (window.confirm(`Delete provider “${provider.display_name}”?${extra}`)) {
                remove.mutate();
              }
            }}
          >
            Delete
          </Button>
        </footer>
      ) : null}
    </article>
  );
}

function ProfileRow({
  profile,
  provider,
  isDefault,
  isAdmin,
  workspaceId,
  onChanged,
  onError,
  onEdit,
}: {
  profile: ModelProfile;
  provider: ModelProvider | undefined;
  isDefault: boolean;
  isAdmin: boolean;
  workspaceId: string;
  onChanged: () => void;
  onError: (message: string | null) => void;
  onEdit: (profile: ModelProfile) => void;
}) {
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

  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/api/v1/workspaces/${workspaceId}/model-profiles/${profile.id}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      onError(null);
      onChanged();
    },
    onError: (error) => onError(errText(error, "Delete failed.")),
  });

  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  const refreshPrices = useMutation({
    mutationFn: () =>
      api<ProfilePricingRefresh>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/${profile.id}/refresh-pricing`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      onError(null);
      setRefreshNote(result.detail);
      if (result.updated) onChanged();
    },
    onError: (error) => onError(errText(error, "Refreshing prices failed.")),
  });

  const cost = (micros: number | null) =>
    micros === null ? "—" : `$${(micros / 1_000_000).toFixed(2)}`;

  return (
    <tr className="border-t border-line hover:bg-hover">
      <td className="px-4 py-3 font-medium text-ink">{profile.display_name}</td>
      <td className="px-4 py-3">
        <code className="font-mono text-xs">{profile.model_name}</code>
      </td>
      <td className="px-4 py-3 text-dim">{provider?.display_name ?? "—"}</td>
      <td className="px-4 py-3 tabular-nums text-dim">
        {cost(profile.input_cost_micros_per_million)} ·{" "}
        {cost(profile.output_cost_micros_per_million)}
        {refreshNote ? <p className="mt-1 text-[11px] text-faint">{refreshNote}</p> : null}
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          {isDefault ? (
            <Badge tone="accent">workspace default</Badge>
          ) : isAdmin ? (
            <Button size="sm" variant="ghost" onClick={() => makeDefault.mutate()}>
              Make default
            </Button>
          ) : null}
          {isAdmin ? (
            <>
              <Button size="sm" variant="ghost" onClick={() => onEdit(profile)}>
                Edit
              </Button>
              <Button
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
                size="sm"
                variant="ghost"
                className="text-danger"
                disabled={remove.isPending}
                onClick={() => {
                  const note = isDefault
                    ? " It is the workspace default; agents will have no default model until you pick another."
                    : "";
                  if (window.confirm(`Delete profile “${profile.display_name}”?${note}`)) {
                    remove.mutate();
                  }
                }}
              >
                Delete
              </Button>
            </>
          ) : null}
          {!isAdmin && !isDefault ? <span className="text-faint">—</span> : null}
        </div>
      </td>
    </tr>
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
  onClose,
  onCreated,
}: {
  workspaceId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const secrets = useSecrets(workspaceId);
  const [type, setType] = useState<ModelProviderType>("openai");
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [keyMode, setKeyMode] = useState<"new" | "existing" | "none">("new");
  const [apiKey, setApiKey] = useState("");
  const [secretId, setSecretId] = useState("");
  const [adminKey, setAdminKey] = useState("");

  const typeMeta = PROVIDER_TYPES.find((t) => t.value === type)!;

  // The provider can only be saved after a live check of exactly these inputs.
  const draftKey = JSON.stringify({ type, baseUrl: baseUrl.trim(), keyMode, apiKey, secretId });
  const [verified, setVerified] = useState<{ key: string; detail: string } | null>(null);
  const verifiedForCurrent = verified?.key === draftKey;
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
            secret_id: keyMode === "existing" && secretId ? secretId : null,
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

  const create = useMutation({
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
    <Dialog title="Add model provider" open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Provider type">
          <Select value={type} onChange={(e) => setType(e.target.value as ModelProviderType)}>
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
        {type === "openai" ? (
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
        <ErrorNote message={errText(create.error, "Creating the provider failed.")} />
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
            disabled={create.isPending || !verifiedForCurrent}
            title={verifiedForCurrent ? undefined : "Test the connection first"}
          >
            {create.isPending ? "Adding…" : "Add provider"}
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
  onClose,
  onCreated,
}: {
  workspaceId: string;
  providers: ModelProvider[];
  /** When set, the dialog edits this profile (PATCH) instead of creating one. */
  existing?: ModelProfile;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [providerId, setProviderId] = useState(existing?.provider_id ?? providers[0]?.id ?? "");
  const [displayName, setDisplayName] = useState(existing?.display_name ?? "");
  const [modelName, setModelName] = useState(existing?.model_name ?? "");
  const [inputCost, setInputCost] = useState(
    microsToDollarInput(existing?.input_cost_micros_per_million ?? null),
  );
  const [outputCost, setOutputCost] = useState(
    microsToDollarInput(existing?.output_cost_micros_per_million ?? null),
  );
  const [contextWindow, setContextWindow] = useState(
    existing?.context_window ? String(existing.context_window) : "",
  );
  const [pricingOpen, setPricingOpen] = useState(false);
  const [pricingNote, setPricingNote] = useState<string | null>(null);
  // The model the prices were last auto-filled for, so edits survive re-renders.
  const [autofilledFor, setAutofilledFor] = useState<string | null>(null);
  const providerModels = useProviderModels(workspaceId, providerId || null);
  const provider = providers.find((p) => p.id === providerId);
  const providerType: ModelProviderType = provider?.type ?? "openai_compatible";
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
          },
        },
      ),
    onSuccess: () => {
      onCreated();
      onClose();
    },
  });

  const pricesKnown = Boolean(inputCost || outputCost);
  const summary = pricesKnown
    ? `$${inputCost || "0"} in · $${outputCost || "0"} out per 1M tokens`
    : "No prices yet — runs will show $0.00 until you add them.";

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
            </div>
          ) : pricingNote ? (
            <p className="border-t border-line px-3 py-2 text-xs text-dim">{pricingNote}</p>
          ) : null}
        </div>

        <ErrorNote
          message={errText(create.error, existing ? "Saving the profile failed." : "Creating the profile failed.")}
        />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Saving…" : existing ? "Save changes" : "Create profile"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

const ADMIN_KEY_HINT =
  "Optional. OpenAI has no balance API; an admin key lets Jhin read month-to-date spend. Create one in the OpenAI dashboard → Settings → Organization → Admin keys. Stored encrypted, never displayed.";

/** Balance and spend for one provider: live remaining when the provider
 * reports it, otherwise Jhin's tracked spend with an optional "loaded
 * credits" figure so an estimated remaining amount can be shown. */
function BalanceBlock({
  provider,
  isAdmin,
  workspaceId,
  onChanged,
  onError,
}: {
  provider: ModelProvider;
  isAdmin: boolean;
  workspaceId: string;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  const balance = useProviderBalance(workspaceId, provider.id);
  const [adminKeyDialog, setAdminKeyDialog] = useState(false);
  const [credits, setCredits] = useState<string | null>(null);

  const saveCredits = useMutation({
    mutationFn: (micros: number | null) =>
      api<ModelProvider>(`/api/v1/workspaces/${workspaceId}/model-providers/${provider.id}`, {
        method: "PATCH",
        body: { credits_loaded_micros: micros },
      }),
    onSuccess: () => {
      onError(null);
      setCredits(null);
      onChanged();
      void balance.refetch();
    },
    onError: (error) => onError(errText(error, "Saving the loaded credits failed.")),
  });

  if (balance.isPending) {
    return (
      <div data-testid="balance-block" className="rounded-xl bg-raised px-3 py-2 text-xs text-faint">
        Loading balance…
      </div>
    );
  }
  const data = balance.data;
  if (!data) {
    return (
      <div data-testid="balance-block" className="rounded-xl bg-raised px-3 py-2 text-xs text-faint">
        Balance unavailable.
      </div>
    );
  }

  const live = data.provider_remaining_micros !== null;
  const creditsValue = credits ?? microsToDollarInput(data.credits_loaded_micros);
  const showAddAdminKey = provider.type === "openai" && !provider.has_admin_key && isAdmin;

  return (
    <div data-testid="balance-block" className="space-y-1.5 rounded-xl bg-raised px-3 py-2 text-xs">
      <p className="flex items-center gap-1.5 font-medium text-ink">
        <Wallet size={12} aria-hidden /> Balance
      </p>
      {live ? (
        <p className="text-sm tabular-nums text-ink">
          {formatMicrosAsDollars(data.provider_remaining_micros)}{" "}
          <span className="text-xs text-dim">remaining</span>
        </p>
      ) : (
        <p className="text-dim">
          {data.source === "openai_admin" && data.provider_spent_month_micros !== null ? (
            <>
              Spent this month: <span className="tabular-nums text-ink">{formatMicrosAsDollars(data.provider_spent_month_micros)}</span>
            </>
          ) : (
            <>
              Spent this month through Jhin:{" "}
              <span className="tabular-nums text-ink">{formatMicrosAsDollars(data.tracked_spent_month_micros)}</span>
            </>
          )}
        </p>
      )}
      {!live ? (
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-faint">
            Loaded credits $
            <input
              type="number"
              min="0"
              step="any"
              aria-label="Loaded credits in dollars"
              className="h-6 w-24 rounded-md border border-line bg-surface px-1.5 text-xs tabular-nums text-ink"
              value={creditsValue}
              disabled={!isAdmin || saveCredits.isPending}
              onChange={(e) => setCredits(e.target.value)}
              onBlur={() => {
                if (credits === null) return;
                const micros = dollarInputToMicros(credits);
                if (micros !== (data.credits_loaded_micros ?? null)) saveCredits.mutate(micros);
                else setCredits(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  (e.target as HTMLInputElement).blur();
                }
              }}
            />
          </label>
          {data.estimated_remaining_micros !== null ? (
            <span className="tabular-nums text-ink">
              ≈ {formatMicrosAsDollars(data.estimated_remaining_micros)} remaining
            </span>
          ) : null}
        </div>
      ) : null}
      <p className="text-faint" title={data.detail ?? undefined}>
        {balanceSourceLabel(data.source)}
        {data.source === "tracked" && data.detail && data.detail !== "Tracked by Jhin"
          ? ` — ${data.detail}`
          : ""}
      </p>
      {showAddAdminKey ? (
        <Button size="sm" variant="ghost" onClick={() => setAdminKeyDialog(true)}>
          <KeyRound size={12} /> Add admin key
        </Button>
      ) : null}
      {adminKeyDialog ? (
        <AdminKeyDialog
          workspaceId={workspaceId}
          provider={provider}
          onClose={() => setAdminKeyDialog(false)}
          onSaved={() => {
            setAdminKeyDialog(false);
            onChanged();
            void balance.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

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

/** Month-to-date tracked spend across every provider, with the budget bar
 * when a monthly budget is set (Settings → Budget). */
export function SpendTile({ spend }: { spend: WorkspaceSpend }) {
  const budget = summarizeBudget(
    spend.spent_month_micros,
    spend.monthly_budget_micros,
    spend.warning_threshold,
  );
  const barTone =
    budget?.tone === "over" ? "bg-danger" : budget?.tone === "warn" ? "bg-warn" : "bg-accent";
  return (
    <section
      data-testid="spend-tile"
      aria-label="Spend"
      className="flex flex-col gap-2 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card md:flex-row md:items-center md:gap-6"
    >
      <div>
        <p className="text-xs font-medium uppercase tracking-wider text-faint">Spend this month</p>
        <p className="font-display text-2xl font-semibold tabular-nums text-ink">
          {formatMicrosAsDollars(spend.spent_month_micros)}
        </p>
        <p className="text-xs text-dim">
          {formatMicrosAsDollars(spend.spent_total_micros)} all time · tracked by Jhin from run costs
        </p>
      </div>
      <div className="flex-1">
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
        {spend.providers.length > 1 ? (
          <p className="mt-1 truncate text-xs text-faint">
            {spend.providers
              .map((p) => `${p.display_name} ${formatMicrosAsDollars(p.spent_month_micros)}`)
              .join(" · ")}
          </p>
        ) : null}
      </div>
    </section>
  );
}
