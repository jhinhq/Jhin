"use client";

/** Models page (plan 15, 17.2 nav): provider cards with verify, model
 * profiles with pricing, and the workspace default profile. API keys are
 * stored once in the encrypted secret store and only referenced here. */

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Cpu, KeyRound, Plus, ShieldCheck, XCircle } from "lucide-react";
import { useState } from "react";
import { PageHeader } from "@/components/app-shell";
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
  useSecrets,
  useWorkspaceDetail,
} from "@/lib/hooks";
import type { ModelProfile, ModelProvider, ModelProviderType } from "@/lib/types";
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
  const invalidate = useInvalidateModels(workspaceId);

  const [providerDialog, setProviderDialog] = useState(false);
  const [profileDialog, setProfileDialog] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  if (providers.isPending || profiles.isPending) {
    return (
      <>
        <PageHeader title="Models" />
        <div className="px-8 py-6">
          <Spinner label="Loading model configuration…" />
        </div>
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
        description="Providers, model profiles, and the workspace default"
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
      <div className="space-y-8 px-8 py-6">
        <ErrorNote message={pageError} />

        <section>
          <h2 className="mb-3 text-sm font-semibold text-dim">Providers</h2>
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
                  onChanged={invalidate}
                  onError={setPageError}
                />
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-3 text-sm font-semibold text-dim">Model profiles</h2>
          {profileList.length === 0 ? (
            <EmptyState
              title="No model profiles yet"
              description="A profile is a named model on a provider with pricing — agents reference profiles, never raw providers."
            />
          ) : (
            <div className="overflow-hidden rounded-xl border border-line">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line bg-raised text-left text-xs uppercase tracking-wider text-dim">
                    <th className="px-4 py-2.5 font-medium">Profile</th>
                    <th className="px-4 py-2.5 font-medium">Model</th>
                    <th className="px-4 py-2.5 font-medium">Provider</th>
                    <th className="px-4 py-2.5 font-medium">Cost / 1M in · out</th>
                    <th className="px-4 py-2.5 font-medium">Default</th>
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
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

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
    </>
  );
}

function ProviderCard({
  provider,
  isAdmin,
  workspaceId,
  profileCount,
  onChanged,
  onError,
}: {
  provider: ModelProvider;
  isAdmin: boolean;
  workspaceId: string;
  profileCount: number;
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
    onError: (error) => onError(errText(error, "Delete failed.")),
  });

  const typeLabel =
    PROVIDER_TYPES.find((t) => t.value === provider.type)?.label ?? provider.type;

  return (
    <article className="flex flex-col gap-3 rounded-xl border border-line bg-surface px-5 py-4">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 truncate text-sm font-semibold">
            <Cpu size={14} className="shrink-0 text-accent" /> {provider.display_name}
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
          className={`flex items-start gap-1.5 rounded-md px-2.5 py-1.5 text-xs ${
            verifyResult.ok
              ? "bg-ok/10 text-ok"
              : "bg-danger/10 text-danger"
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
        <p className="rounded-md bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
          {provider.last_error}
        </p>
      ) : null}

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
              if (window.confirm(`Delete provider “${provider.display_name}”?`)) {
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
}: {
  profile: ModelProfile;
  provider: ModelProvider | undefined;
  isDefault: boolean;
  isAdmin: boolean;
  workspaceId: string;
  onChanged: () => void;
  onError: (message: string | null) => void;
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

  const cost = (micros: number | null) =>
    micros === null ? "—" : `$${(micros / 1_000_000).toFixed(2)}`;

  return (
    <tr className="border-b border-line last:border-0 hover:bg-hover/50">
      <td className="px-4 py-2.5 font-medium">{profile.display_name}</td>
      <td className="px-4 py-2.5">
        <code className="text-xs">{profile.model_name}</code>
      </td>
      <td className="px-4 py-2.5 text-dim">{provider?.display_name ?? "—"}</td>
      <td className="px-4 py-2.5 tabular-nums text-dim">
        {cost(profile.input_cost_micros_per_million)} ·{" "}
        {cost(profile.output_cost_micros_per_million)}
      </td>
      <td className="px-4 py-2.5">
        {isDefault ? (
          <Badge tone="accent">workspace default</Badge>
        ) : isAdmin ? (
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={() => makeDefault.mutate()}>
              Make default
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-danger"
              onClick={() => {
                if (window.confirm(`Delete profile “${profile.display_name}”?`)) {
                  remove.mutate();
                }
              }}
            >
              Delete
            </Button>
          </div>
        ) : (
          <span className="text-faint">—</span>
        )}
      </td>
    </tr>
  );
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

  const typeMeta = PROVIDER_TYPES.find((t) => t.value === type)!;

  const create = useMutation({
    mutationFn: async () => {
      let resolvedSecretId: string | null = null;
      if (keyMode === "new" && apiKey.trim()) {
        // Store the key in the encrypted secret store first, then reference it.
        const secret = await api<{ id: string }>(
          `/api/v1/workspaces/${workspaceId}/secrets`,
          {
            method: "POST",
            body: {
              name: `${displayName.trim() || type} API key`,
              value: apiKey.trim(),
              type: "api_key",
            },
          },
        );
        resolvedSecretId = secret.id;
      } else if (keyMode === "existing" && secretId) {
        resolvedSecretId = secretId;
      }
      return api(`/api/v1/workspaces/${workspaceId}/model-providers`, {
        method: "POST",
        body: {
          type,
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
          secret_id: resolvedSecretId,
        },
      });
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
        <ErrorNote message={errText(create.error, "Creating the provider failed.")} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Adding…" : "Add provider"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ProfileDialog({
  workspaceId,
  providers,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  providers: ModelProvider[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [providerId, setProviderId] = useState(providers[0]?.id ?? "");
  const [displayName, setDisplayName] = useState("");
  const [modelName, setModelName] = useState("");
  const [inputCost, setInputCost] = useState("");
  const [outputCost, setOutputCost] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}/model-profiles`, {
        method: "POST",
        body: {
          provider_id: providerId,
          display_name: displayName.trim(),
          model_name: modelName.trim(),
          // UI takes $ per 1M tokens; API stores micro-dollars per 1M.
          input_cost_micros_per_million: inputCost
            ? Math.round(Number(inputCost) * 1_000_000)
            : null,
          output_cost_micros_per_million: outputCost
            ? Math.round(Number(outputCost) * 1_000_000)
            : null,
        },
      }),
    onSuccess: () => {
      onCreated();
      onClose();
    },
  });

  return (
    <Dialog title="New model profile" open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Provider">
          <Select value={providerId} onChange={(e) => setProviderId(e.target.value)} required>
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.display_name}
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
        <Field label="Model name" hint="The exact model identifier the provider expects.">
          <Input
            required
            maxLength={200}
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="gpt-5-mini"
          />
        </Field>
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
        <ErrorNote message={errText(create.error, "Creating the profile failed.")} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create profile"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
