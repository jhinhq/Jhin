"use client";

/** Connect or edit a provider. Saving is gated on a live "Test connection"
 * of exactly the current draft: every field change requires a new test. The
 * API key (and an optional OpenAI admin key) is stored once in the encrypted
 * secret store and the provider row only references the secret id. */

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { ADMIN_KEY_HINT } from "@/components/models/admin-key-dialog";
import { Button, Dialog, ErrorNote, Field, Input, Select } from "@/components/ui";
import { api, ApiError, errorText } from "@/lib/api";
import { useSecrets } from "@/lib/hooks";
import { storeApiKey } from "@/lib/model-secrets";
import { PROVIDER_TYPES } from "@/lib/models";
import type { ModelProvider, ModelProviderType } from "@/lib/types";

export function ProviderDialog({
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
    onError: (error) => setTestFailure(errorText(error, "The connection test failed.")),
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
          <Field label="Admin key (for spend reporting)" hint={ADMIN_KEY_HINT}>
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
          message={
            save.error
              ? errorText(
                  save.error,
                  editing ? "Saving the provider failed." : "Creating the provider failed.",
                )
              : null
          }
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
            {save.isPending
              ? editing
                ? "Saving…"
                : "Adding…"
              : editing
                ? "Save changes"
                : "Add provider"}
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
