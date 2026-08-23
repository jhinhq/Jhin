"use client";

/** Connectors page (plan 17.9): gallery of available connectors, workspace
 * connections with health, and the create/detail flows. Credentials are
 * write-only; the webhook signing secret is shown exactly once at creation. */

import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  Copy,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Webhook,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { ConnectorsGallery } from "@/components/connectors-gallery";
import { ConnectionAccessSummary } from "@/components/connection-access-summary";
import { CreateConnectionDialog } from "@/components/connection-create-dialog";
import { ConnectionTools } from "@/components/connection-tools";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Spinner,
  Tabs,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { findAuthScheme, webhookPayloadUrl } from "@/lib/connectors";
import { formatDateTime } from "@/lib/format";
import {
  useConnections,
  useConnectionAccessSummary,
  useConnectionToolCalls,
  useConnectionTools,
  useConnectors,
  useInvalidateConnections,
  useMarkConnectionWebhookConfigured,
} from "@/lib/hooks";
import type {
  ConnectionInfo,
  ConnectorInfo,
  VerifyResult,
  WebhookSetup,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

function statusTone(status: string): "ok" | "danger" | "neutral" {
  return status === "active" ? "ok" : status === "error" ? "danger" : "neutral";
}

export default function ConnectorsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  const connectors = useConnectors();
  const connections = useConnections(workspaceId, isAdmin);
  const invalidate = useInvalidateConnections(workspaceId);
  const markWebhookConfigured = useMarkConnectionWebhookConfigured(workspaceId);

  const [createFor, setCreateFor] = useState<ConnectorInfo | null>(null);
  const [webhookOnce, setWebhookOnce] = useState<{
    connection: ConnectionInfo;
    webhook: WebhookSetup;
  } | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  if (connectors.isPending) {
    return (
      <>
        <PageHeader title="Connectors" />
        <PageBody>
          <Spinner label="Loading connectors…" />
        </PageBody>
      </>
    );
  }

  const connectorList = connectors.data ?? [];
  const connectionList = connections.data ?? [];
  const detail = detailId
    ? connectionList.find((connection) => connection.id === detailId) ?? null
    : null;
  const detailConnector = detail
    ? connectorList.find((c) => c.connector_type === detail.connector_type)
    : undefined;

  return (
    <>
      <PageHeader
        title="Connectors"
        description="Connect outside services, choose what each connection can reach, and set up webhooks."
      />
      <PageBody className="space-y-8">
        <section>
          <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">Available connectors</h2>
          <ConnectorsGallery
            connectors={connectorList}
            canManage={isAdmin}
            onConnect={setCreateFor}
          />
        </section>

        {isAdmin ? (
          <section>
            <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">Connections</h2>
            {connections.isPending ? (
              <Spinner label="Loading connections…" />
            ) : connectionList.length === 0 ? (
              <EmptyState
                title="No connections yet"
                description="Connect GitHub above to let agents read repositories, open pull requests, and receive webhooks — or any MCP server to bring its tools in. Access is scoped per agent by grants."
              />
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {connectionList.map((connection) => (
                  <button
                    key={connection.id}
                    type="button"
                    data-testid={`connection-${connection.name}`}
                    onClick={() => setDetailId(connection.id)}
                    className={`flex flex-col gap-2 rounded-2xl border border-line bg-surface px-5 py-4 text-left shadow-card transition-colors hover:border-accent ${focusRing}`}
                  >
                    <header className="flex items-center justify-between gap-2">
                      <h3 className="min-w-0 truncate font-display text-sm font-semibold text-ink">
                        {connection.name}
                      </h3>
                      <Badge tone={statusTone(connection.status)}>{connection.status}</Badge>
                    </header>
                    <p className="text-xs text-dim">
                      {connection.connector_type} · {connection.auth_type}
                    </p>
                    <p className="text-xs text-faint">
                      Last verified:{" "}
                      {connection.last_verified_at
                        ? formatDateTime(connection.last_verified_at)
                        : "never"}
                    </p>
                    {connection.last_error ? (
                      <p className="truncate rounded-xl bg-danger-soft px-2.5 py-1 text-xs text-danger">
                        {connection.last_error}
                      </p>
                    ) : null}
                  </button>
                ))}
              </div>
            )}
          </section>
        ) : (
          <p className="text-sm text-dim">Connections are managed by workspace admins.</p>
        )}
      </PageBody>

      {createFor ? (
        <CreateConnectionDialog
          workspaceId={workspaceId}
          connector={createFor}
          onClose={() => setCreateFor(null)}
          onCreated={(created) => {
            invalidate();
            setCreateFor(null);
            if (created.webhook) {
              setWebhookOnce({
                connection: created.connection,
                webhook: created.webhook,
              });
            }
          }}
        />
      ) : null}

      {webhookOnce ? (
        <WebhookSecretDialog
          connectionName={webhookOnce.connection.name}
          connectionId={webhookOnce.connection.id}
          workspaceId={workspaceId}
          webhook={webhookOnce.webhook}
          onClose={() => setWebhookOnce(null)}
          onStored={() => {
            markWebhookConfigured(webhookOnce.connection);
            invalidate();
          }}
        />
      ) : null}

      {detail ? (
        <ConnectionDetailDialog
          workspaceId={workspaceId}
          connection={detail}
          connector={detailConnector}
          onClose={() => setDetailId(null)}
          onChanged={() => {
            invalidate();
            setDetailId(null);
          }}
        />
      ) : null}
    </>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wider text-dim">{label}</p>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-xl border border-line bg-raised px-3 py-2 font-mono text-xs text-ink">
          {value}
        </code>
        <Button
          size="sm"
          aria-label={`Copy ${label}`}
          onClick={() => {
            void navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          <Copy size={12} /> {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

function WebhookSecretDialog({
  workspaceId,
  connectionId,
  connectionName,
  webhook,
  onClose,
  onStored,
}: {
  workspaceId: string;
  connectionId: string;
  connectionName: string;
  webhook: WebhookSetup;
  onClose: () => void;
  onStored: () => Promise<void> | void;
}) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const [providerSecret, setProviderSecret] = useState("");
  const storeSecret = useMutation({
    mutationFn: () => api(
      `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/webhook-secret`,
      { method: "PUT", body: { secret: providerSecret } },
    ),
    onSuccess: async () => {
      setProviderSecret("");
      await onStored();
      onClose();
    },
  });
  const providerSupplied = webhook.secret_mode === "provider_supplied";
  return (
    <Dialog title={providerSupplied ? "Webhook setup" : "Webhook secret — shown once"} open onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-dim">
          Configure the provider webhook for <strong className="text-ink">{connectionName}</strong>.
        </p>
        <CopyRow
          label={providerSupplied ? "Callback URL" : "Payload URL"}
          value={webhookPayloadUrl(webhook.url_path, origin)}
        />
        {providerSupplied ? (
          <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); storeSecret.mutate(); }}>
            <p className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-xs text-warn">
              Create the webhook in Vercel. Jhin verifies the x-vercel-signature header with HMAC SHA1.
            </p>
            <Field label="Provider-generated signing secret" hint="Write-only: the stored value is never displayed.">
              <Input
                type="password"
                autoComplete="off"
                minLength={16}
                required
                value={providerSecret}
                onChange={(event) => setProviderSecret(event.target.value)}
              />
            </Field>
            <Button type="submit" variant="primary" disabled={storeSecret.isPending || providerSecret.length < 16}>
              {storeSecret.isPending ? "Storing…" : "Store provider secret"}
            </Button>
            <ErrorNote message={errText(storeSecret.error, "Storing the webhook secret failed.")} />
          </form>
        ) : webhook.secret ? (
          <>
            <CopyRow label="Signing secret" value={webhook.secret} />
            <p className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-xs text-warn">
              This generated secret is shown once and cannot be retrieved later.
            </p>
          </>
        ) : null}
        {webhook.help ? <p className="text-xs text-dim">{webhook.help}</p> : null}
        <div className="flex justify-end">
          <Button variant={providerSupplied ? "ghost" : "primary"} onClick={onClose}>
            {providerSupplied ? "Close" : "I stored the secret"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

ConnectorsPage.WebhookSecretDialog = WebhookSecretDialog;

function ConnectionDetailDialog({
  workspaceId,
  connection,
  connector,
  onClose,
  onChanged,
}: {
  workspaceId: string;
  connection: ConnectionInfo;
  connector: ConnectorInfo | undefined;
  onClose: () => void;
  onChanged: () => void;
}) {
  const base = `/api/v1/workspaces/${workspaceId}/connections/${connection.id}`;
  const toolCalls = useConnectionToolCalls(workspaceId, connection.id);
  const accessSummary = useConnectionAccessSummary(workspaceId, connection.id);
  const tools = useConnectionTools(workspaceId, connection.id);
  const invalidateAll = useInvalidateConnections(workspaceId);
  const [tab, setTab] = useState<"overview" | "tools">("overview");
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [rotating, setRotating] = useState(false);
  const [rotateCredentials, setRotateCredentials] = useState<Record<string, string>>({});
  const [webhookSecret, setWebhookSecret] = useState("");
  const [error, setError] = useState<string | null>(null);

  const scheme = findAuthScheme(connector, connection.auth_type);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  const verify = useMutation({
    mutationFn: () => api<VerifyResult>(`${base}/verify`, { method: "POST" }),
    onSuccess: (result) => {
      setError(null);
      setVerifyResult(result);
    },
    onError: (err) => setError(errText(err, "Verification failed.")),
  });

  const rotate = useMutation({
    mutationFn: () =>
      api(`${base}/rotate`, {
        method: "POST",
        body: {
          credentials: Object.fromEntries(
            Object.entries(rotateCredentials).filter(([, value]) => value.trim() !== ""),
          ),
        },
      }),
    onSuccess: () => {
      setRotating(false);
      setRotateCredentials({});
      onChanged();
    },
    onError: (err) => setError(errText(err, "Rotating the credential failed.")),
  });

  const toggle = useMutation({
    mutationFn: () =>
      api(`${base}/${connection.status === "disabled" ? "enable" : "disable"}`, {
        method: "POST",
      }),
    onSuccess: onChanged,
    onError: (err) => setError(errText(err, "Updating the connection failed.")),
  });

  const remove = useMutation({
    mutationFn: () => api<void>(base, { method: "DELETE" }),
    onSuccess: onChanged,
    onError: (err) => setError(errText(err, "Deleting the connection failed.")),
  });

  const storeWebhookSecret = useMutation({
    mutationFn: () => api(`${base}/webhook-secret`, {
      method: "PUT",
      body: { secret: webhookSecret },
    }),
    onSuccess: () => {
      setWebhookSecret("");
      onChanged();
    },
    onError: (err) => setError(errText(err, "Storing the webhook secret failed.")),
  });

  const calls = toolCalls.data ?? [];

  return (
    <Dialog title={connection.name} open onClose={onClose} wide>
      <div className="space-y-5">
        <ErrorNote message={error} />

        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={statusTone(connection.status)}>{connection.status}</Badge>
          <Badge tone="neutral">{connection.connector_type}</Badge>
          <Badge tone="neutral">
            <KeyRound size={11} /> {scheme?.label ?? connection.auth_type}
          </Badge>
          <span className="ml-auto text-xs text-faint">
            Last verified:{" "}
            {connection.last_verified_at ? formatDateTime(connection.last_verified_at) : "never"}
          </span>
        </div>

        {Object.keys(connection.config_json).length > 0 ? (
          <dl className="space-y-1 rounded-xl border border-line bg-raised px-3.5 py-2.5 text-xs">
            {Object.entries(connection.config_json).map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="shrink-0 text-faint">{key}:</dt>
                <dd className="min-w-0 truncate font-mono">{String(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}

        {verifyResult ? (
          <p
            className={`flex items-start gap-1.5 rounded-xl px-3 py-2 text-xs ${
              verifyResult.ok ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
            }`}
          >
            {verifyResult.ok ? (
              <CheckCircle2 size={13} className="mt-0.5 shrink-0" />
            ) : (
              <XCircle size={13} className="mt-0.5 shrink-0" />
            )}
            <span className="min-w-0 break-words">{verifyResult.message}</span>
          </p>
        ) : connection.last_error ? (
          <p className="rounded-xl bg-danger-soft px-3 py-2 text-xs text-danger">
            {connection.last_error}
          </p>
        ) : null}

        {connector?.supports_webhooks ? (
          <div className="space-y-2 rounded-xl border border-line bg-raised px-3.5 py-3">
            <p className="flex items-center gap-1.5 text-sm font-medium text-ink">
              <Webhook size={13} /> Webhook
            </p>
            <CopyRow
              label="Payload URL"
              value={webhookPayloadUrl(
                `/api/v1/webhooks/${connection.connector_type}/${connection.public_id}`,
                origin,
              )}
            />
            <p className="text-xs text-faint">
              {connection.webhook_secret_configured
                ? "Webhook secret configured"
                : connector.webhook_secret_mode === "provider_supplied"
                  ? "Webhook secret not configured. Store the provider-generated secret from setup."
                  : "Webhook secret is not configured."}
            </p>
            {connector.webhook_secret_mode === "provider_supplied" ? (
              <form
                className="space-y-2 border-t border-line pt-2"
                onSubmit={(event) => { event.preventDefault(); storeWebhookSecret.mutate(); }}
              >
                <p className="text-xs text-dim">
                  Vercel signs the callback in the x-vercel-signature header with HMAC SHA1.
                </p>
                <Field
                  label="Provider-generated signing secret"
                  hint="Paste the Vercel secret here. It is write-only and cannot be read back."
                >
                  <Input
                    type="password"
                    autoComplete="off"
                    minLength={16}
                    required
                    value={webhookSecret}
                    onChange={(event) => setWebhookSecret(event.target.value)}
                  />
                </Field>
                <Button
                  type="submit"
                  size="sm"
                  disabled={storeWebhookSecret.isPending || webhookSecret.length < 16}
                >
                  {storeWebhookSecret.isPending ? "Storing…" : "Store provider secret"}
                </Button>
              </form>
            ) : null}
          </div>
        ) : null}

        {rotating ? (
          <form
            className="space-y-3 rounded-xl border border-accent/40 bg-accent-soft px-3.5 py-3"
            onSubmit={(event) => {
              event.preventDefault();
              rotate.mutate();
            }}
          >
            <p className="text-sm font-medium text-ink">Re-enter credentials ({scheme?.label})</p>
            {(scheme?.secret_fields ?? []).map((field) => (
              <Field key={field.name} label={field.label}>
                {field.multiline ? (
                  <Textarea
                    rows={4}
                    value={rotateCredentials[field.name] ?? ""}
                    onChange={(e) =>
                      setRotateCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                    }
                    placeholder={field.placeholder}
                    required={field.required}
                    className="font-mono text-xs"
                  />
                ) : (
                  <Input
                    type="password"
                    autoComplete="off"
                    value={rotateCredentials[field.name] ?? ""}
                    onChange={(e) =>
                      setRotateCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                    }
                    placeholder={field.placeholder}
                    required={field.required}
                  />
                )}
              </Field>
            ))}
            <div className="flex justify-end gap-2">
              <Button type="button" size="sm" variant="ghost" onClick={() => setRotating(false)}>
                Cancel
              </Button>
              <Button type="submit" size="sm" variant="primary" disabled={rotate.isPending}>
                {rotate.isPending ? "Rotating…" : "Rotate credential"}
              </Button>
            </div>
          </form>
        ) : null}

        <Tabs
          label="Connection sections"
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "tools", label: `Tools${tools.data ? ` (${tools.data.tools.length})` : ""}` },
          ]}
          value={tab}
          onChange={(id) => setTab(id as "overview" | "tools")}
        />

        {tab === "tools" ? (
          <section data-testid="connection-tools-tab">
            <ConnectionTools
              workspaceId={workspaceId}
              connectionId={connection.id}
              data={tools.data}
              isPending={tools.isPending}
              error={tools.error}
              canManage
              onChanged={() => invalidateAll()}
            />
          </section>
        ) : null}

        <section hidden={tab !== "overview"}>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-dim">
            Agent access
          </h3>
          {accessSummary.isPending ? <Spinner /> : accessSummary.data ? (
            <ConnectionAccessSummary summary={accessSummary.data} />
          ) : (
            <p className="text-xs text-danger">Access summary could not be loaded.</p>
          )}
        </section>

        <section hidden={tab !== "overview"}>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-dim">
            Recent tool usage
          </h3>
          {toolCalls.isPending ? (
            <Spinner />
          ) : calls.length === 0 ? (
            <p className="text-xs text-faint">No tool calls through this connection yet.</p>
          ) : (
            <ul className="max-h-48 space-y-1 overflow-y-auto">
              {calls.map((call) => (
                <li
                  key={call.id}
                  className="flex items-center gap-2 rounded-xl border border-line bg-raised px-3 py-2 text-xs"
                >
                  <code className="min-w-0 flex-1 truncate font-mono">{call.tool_name}</code>
                  <Badge tone={call.status === "completed" ? "ok" : "danger"}>
                    {call.status}
                  </Badge>
                  <span className="shrink-0 text-faint">{formatDateTime(call.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <footer className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <Button size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
            <ShieldCheck size={13} /> {verify.isPending ? "Verifying…" : "Verify"}
          </Button>
          <Button size="sm" onClick={() => setRotating(true)} disabled={rotating}>
            <RefreshCw size={13} /> Rotate credential
          </Button>
          <Button size="sm" onClick={() => toggle.mutate()} disabled={toggle.isPending}>
            {connection.status === "disabled" ? "Enable" : "Disable"}
          </Button>
          <Button
            size="sm"
            variant="danger"
            className="ml-auto"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Delete connection “${connection.name}”? Grants that reference it stop working.`)) {
                remove.mutate();
              }
            }}
          >
            Delete
          </Button>
        </footer>
      </div>
    </Dialog>
  );
}
