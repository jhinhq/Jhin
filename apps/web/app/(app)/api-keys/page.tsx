"use client";

/** API keys: mint scoped bearer keys, see what they have been doing, revoke
 * them (docs/architecture/api-keys.md). */

import { useMutation } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import {
  ExpiryPicker,
  OneTimeSecret,
  ROLE_COPY,
  RoleBadge,
  ScopeTree,
} from "@/components/access";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useApiKeys, useApiKeyUsage, useScopeCatalog } from "@/lib/hooks";
import type { ApiKeyCreated, ApiKeyInfo, ExpiryUnit } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const USAGE_HELP: Record<string, string> = {
  owner: "As the owner you see every API call made in this workspace.",
  admin: "You see your own key's calls and those of members and viewers.",
  member: "You see calls made by your own keys.",
  viewer: "You see calls made by your own keys.",
};

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

export default function ApiKeysPage() {
  const { workspace, user, role, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const keys = useApiKeys(workspaceId);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/api-keys/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setError(null);
      void keys.refetch();
    },
    onError: (cause) => setError(errorText(cause, "That key could not be revoked.")),
  });

  const rows = keys.data ?? [];

  return (
    <>
      <PageHeader
        eyebrow="Advanced"
        title="API keys"
        description="Let a script, a CI job, or another system act in this workspace without a browser."
        actions={
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            <Plus size={14} /> New key
          </Button>
        }
      />
      <PageBody className="max-w-5xl space-y-8">
        <Card as="section">
          <h2 className="mb-1 font-display text-base font-semibold">Keys</h2>
          <p className="mb-4 text-sm text-dim">
            A key can never do more than the person who created it. Yours is capped at{" "}
            <strong>{ROLE_COPY[role].label.toLowerCase()}</strong>.
          </p>
          <ErrorNote message={error} />
          {keys.isPending ? (
            <Spinner />
          ) : rows.length === 0 ? (
            <EmptyState
              icon={<KeyRound size={20} aria-hidden />}
              title="No API keys yet"
              description="Create one to call the Jhin API from a script or another system."
              action={
                <Button variant="primary" onClick={() => setCreateOpen(true)}>
                  <Plus size={14} /> New key
                </Button>
              }
            />
          ) : (
            <ul className="divide-y divide-line" data-testid="api-key-list">
              {rows.map((key) => (
                <KeyRow
                  key={key.id}
                  apiKey={key}
                  canRevoke={key.created_by_user_id === user.id || can("admin")}
                  onRevoke={() => {
                    if (
                      window.confirm(
                        `Revoke “${key.name}”? Anything still using it stops working immediately.`,
                      )
                    ) {
                      revoke.mutate(key.id);
                    }
                  }}
                />
              ))}
            </ul>
          )}
        </Card>

        <UsageLog workspaceId={workspaceId} keys={rows} help={USAGE_HELP[role]} />
      </PageBody>

      <CreateKeyDialog
        open={createOpen}
        workspaceId={workspaceId}
        onClose={() => setCreateOpen(false)}
        onCreated={() => void keys.refetch()}
      />
    </>
  );
}

function KeyRow({
  apiKey,
  canRevoke,
  onRevoke,
}: {
  apiKey: ApiKeyInfo;
  canRevoke: boolean;
  onRevoke: () => void;
}) {
  const tone = apiKey.status === "active" ? "ok" : apiKey.status === "expired" ? "warn" : "danger";
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">
          {apiKey.name}{" "}
          <code className="ml-1 font-mono text-xs text-faint">jhin_{apiKey.prefix}_…</code>
        </p>
        <p className="mt-0.5 text-xs text-dim">
          {apiKey.created_by_name ? `Created by ${apiKey.created_by_name}` : "Creator removed"} ·{" "}
          {apiKey.expires_at ? `expires ${formatRelative(apiKey.expires_at)}` : "never expires"} ·{" "}
          {apiKey.last_used_at ? `last used ${formatRelative(apiKey.last_used_at)}` : "never used"}
        </p>
        <p className="mt-1 flex flex-wrap gap-1">
          {apiKey.scopes.map((scope) => (
            <code
              key={scope}
              className="rounded-md bg-hover px-1.5 py-0.5 font-mono text-[11px] text-dim"
            >
              {scope}
            </code>
          ))}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <RoleBadge role={apiKey.role_ceiling} />
        <Badge tone={tone}>{apiKey.status}</Badge>
        {canRevoke && apiKey.status !== "revoked" ? (
          <Button size="sm" variant="ghost" aria-label={`Revoke ${apiKey.name}`} onClick={onRevoke}>
            <Trash2 size={13} />
          </Button>
        ) : null}
      </div>
    </li>
  );
}

function CreateKeyDialog({
  open,
  workspaceId,
  onClose,
  onCreated,
}: {
  open: boolean;
  workspaceId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const catalog = useScopeCatalog(workspaceId);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<string>>(new Set());
  const [amount, setAmount] = useState("90");
  const [unit, setUnit] = useState<ExpiryUnit>("days");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api<ApiKeyCreated>(`/api/v1/workspaces/${workspaceId}/api-keys`, {
        method: "POST",
        body: {
          name,
          scopes: [...scopes],
          expires_unit: unit,
          expires_in: unit === "never" ? null : Number(amount),
        },
      }),
    onSuccess: (result) => {
      setCreated(result);
      setError(null);
      onCreated();
    },
    onError: (cause) => setError(errorText(cause, "That key could not be created.")),
  });

  const close = () => {
    setCreated(null);
    setError(null);
    setName("");
    setScopes(new Set());
    onClose();
  };

  return (
    <Dialog
      title={created ? "Copy your key now" : "New API key"}
      open={open}
      onClose={close}
      wide
      description={
        created ? undefined : "Pick a name, choose what it may do, and decide how long it lasts."
      }
    >
      {created ? (
        <div className="space-y-4">
          <OneTimeSecret
            testId="api-key-reveal"
            label={created.api_key.name}
            value={created.key}
            warning="This is the only time the key is shown. Once you close this dialog it cannot be recovered — you would have to create a new one."
          />
          <p className="text-sm text-dim">
            Send it as{" "}
            <code className="font-mono text-[12px]">Authorization: Bearer &lt;key&gt;</code>.
          </p>
          <div className="flex justify-end">
            <Button variant="primary" onClick={close}>
              I have copied it
            </Button>
          </div>
        </div>
      ) : (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Field label="What is this key for?" hint="A name you will recognise in six months.">
            <Input
              required
              maxLength={120}
              value={name}
              placeholder="Nightly report script"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>

          <div>
            <p className="mb-2 text-sm font-medium">What may it do?</p>
            {catalog.isPending ? (
              <Spinner />
            ) : catalog.data ? (
              <ScopeTree catalog={catalog.data} selected={scopes} onChange={setScopes} />
            ) : (
              <ErrorNote message="The permission list could not be loaded." />
            )}
          </div>

          <ExpiryPicker
            amount={amount}
            unit={unit}
            onAmountChange={setAmount}
            onUnitChange={setUnit}
          />

          <ErrorNote message={error} />
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-faint" data-testid="scope-count">
              {scopes.size === 0
                ? "Choose at least one permission."
                : `${scopes.size} permission${scopes.size === 1 ? "" : "s"} selected`}
            </p>
            <div className="flex gap-2">
              <Button variant="ghost" type="button" onClick={close}>
                Cancel
              </Button>
              <Button
                variant="primary"
                type="submit"
                disabled={create.isPending || scopes.size === 0 || name.trim() === ""}
              >
                {create.isPending ? "Creating…" : "Create key"}
              </Button>
            </div>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function UsageLog({
  workspaceId,
  keys,
  help,
}: {
  workspaceId: string;
  keys: ApiKeyInfo[];
  help: string;
}) {
  const [filter, setFilter] = useState("");
  const usage = useApiKeyUsage(workspaceId, filter || undefined);
  const items = useMemo(() => usage.data?.items ?? [], [usage.data]);

  return (
    <Card as="section">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-display text-base font-semibold">Recent calls</h2>
          <p className="mt-1 text-sm text-dim">{help}</p>
        </div>
        {keys.length > 0 ? (
          <div className="w-56">
            <Field label="Filter by key">
              <Select value={filter} onChange={(event) => setFilter(event.target.value)}>
                <option value="">All keys I can see</option>
                {keys.map((key) => (
                  <option key={key.id} value={key.id}>
                    {key.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        ) : null}
      </div>

      {usage.isPending ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing yet"
          description="Calls made with an API key show up here, including ones that were refused."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-[13px]" data-testid="usage-log">
            <thead className="text-xs uppercase tracking-wider text-faint">
              <tr>
                <th className="py-2 pr-3 font-medium">When</th>
                <th className="py-2 pr-3 font-medium">Key</th>
                <th className="py-2 pr-3 font-medium">Acting as</th>
                <th className="py-2 pr-3 font-medium">Call</th>
                <th className="py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.map((entry) => (
                <tr key={entry.id}>
                  <td className="whitespace-nowrap py-2 pr-3 text-dim">
                    {formatDateTime(entry.created_at)}
                  </td>
                  <td className="py-2 pr-3">
                    {entry.api_key_name ?? "—"}
                    {entry.api_key_prefix ? (
                      <code className="ml-1 font-mono text-[11px] text-faint">
                        {entry.api_key_prefix}
                      </code>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3 text-dim">{entry.acting_user_name ?? "—"}</td>
                  <td className="py-2 pr-3">
                    <code className="font-mono text-[11px]">
                      {entry.method} {entry.path}
                    </code>
                  </td>
                  <td className="py-2">
                    <Badge tone={entry.status_code < 400 ? "ok" : "danger"}>
                      {entry.status_code}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
