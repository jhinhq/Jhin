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
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Select,
  Spinner,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  findAuthScheme,
  coerceConnectorConfig,
  configFieldsForAuth,
  validateConnectionForm,
  webhookPayloadUrl,
} from "@/lib/connectors";
import { formatDateTime } from "@/lib/format";
import {
  useConnections,
  useConnectionAccessSummary,
  useConnectionToolCalls,
  useConnectors,
  useInvalidateConnections,
  useMarkConnectionWebhookConfigured,
} from "@/lib/hooks";
import type {
  ConnectionCreated,
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
                description="Connect GitHub above to let agents read repositories, open pull requests, and receive webhooks — scoped per agent by grants."
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

function CreateConnectionDialog({
  workspaceId,
  connector,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  connector: ConnectorInfo;
  onClose: () => void;
  onCreated: (created: ConnectionCreated) => void;
}) {
  const [name, setName] = useState("");
  const [authType, setAuthType] = useState(connector.auth_schemes[0]?.type ?? "");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const initialConfig = (selectedAuth: string) => Object.fromEntries(
    configFieldsForAuth(connector, selectedAuth)
      .filter((field) => field.default !== null)
      .map((field) => [
        field.name,
        Array.isArray(field.default)
          ? field.default.join("\n")
          : typeof field.default === "boolean"
            ? field.default
            : String(field.default),
      ]),
  );
  const [config, setConfig] = useState<Record<string, string | boolean>>(() => initialConfig(authType));
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const scheme = findAuthScheme(connector, authType);

  const create = useMutation({
    mutationFn: () =>
      api<ConnectionCreated>(`/api/v1/workspaces/${workspaceId}/connections`, {
        method: "POST",
        body: {
          connector_type: connector.connector_type,
          name: name.trim(),
          auth_type: authType,
          credentials: Object.fromEntries(
            Object.entries(credentials).filter(([, value]) => value.trim() !== ""),
          ),
          config: coerceConnectorConfig(configFieldsForAuth(connector, authType), config),
        },
      }),
    onSuccess: onCreated,
  });

  return (
    <Dialog title={`Connect ${connector.display_name}`} open onClose={onClose} wide>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          const errors = validateConnectionForm(connector, name, authType, credentials);
          setFormErrors(errors);
          if (errors.length === 0) create.mutate();
        }}
      >
        <Field label="Connection name" hint="Unique in this workspace.">
          <Input
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={`${connector.display_name} (production)`}
          />
        </Field>

        <Field label="Authentication">
          <Select
            value={authType}
            onChange={(e) => {
              setAuthType(e.target.value);
              setCredentials({});
              setConfig(initialConfig(e.target.value));
            }}
          >
            {connector.auth_schemes.map((s) => (
              <option key={s.type} value={s.type}>
                {s.label}
              </option>
            ))}
          </Select>
        </Field>
        {scheme?.description ? <p className="text-xs text-dim">{scheme.description}</p> : null}

        {(scheme?.secret_fields ?? []).map((field) => (
          <Field
            key={field.name}
            label={field.label}
            hint="Stored encrypted (AES-256-GCM envelope); never displayed again."
          >
            {field.multiline ? (
              <Textarea
                rows={5}
                value={credentials[field.name] ?? ""}
                onChange={(e) =>
                  setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
                placeholder={field.placeholder}
                required={field.required}
                className="font-mono text-xs"
              />
            ) : (
              <Input
                type="password"
                autoComplete="off"
                value={credentials[field.name] ?? ""}
                onChange={(e) =>
                  setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
                placeholder={field.placeholder}
                required={field.required}
              />
            )}
          </Field>
        ))}

        {configFieldsForAuth(connector, authType)
          .filter((field) => field.name !== "allow_writes")
          .map((field) => (
            <Field key={field.name} label={field.label} hint={field.help}>
              {field.kind === "boolean" ? (
                <input
                  aria-label={field.label}
                  type="checkbox"
                  checked={config[field.name] === true}
                  onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.checked }))}
                />
              ) : field.kind === "string_list" ? (
                <Textarea
                  rows={3}
                  value={String(config[field.name] ?? "")}
                  onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  placeholder={field.placeholder}
                  required={field.required}
                />
              ) : (
                <Input
                  type={field.kind === "integer" ? "number" : "text"}
                  min={field.minimum ?? undefined}
                  max={field.maximum ?? undefined}
                  value={String(config[field.name] ?? "")}
                  onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.value }))}
                  placeholder={field.placeholder}
                  required={field.required}
                />
              )}
            </Field>
          ))}

        {configFieldsForAuth(connector, authType).some((field) => field.name === "allow_writes") ? (
          <details className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5">
            <summary className={`cursor-pointer rounded-md text-sm font-semibold text-ink ${focusRing}`}>Advanced database access</summary>
            <label className="mt-3 flex items-start gap-2 text-sm">
              <input
                aria-label="Allow database writes"
                type="checkbox"
                checked={config.allow_writes === true}
                onChange={(e) => setConfig((prev) => ({ ...prev, allow_writes: e.target.checked }))}
              />
              <span>
                Allow database writes
                <span className="block text-xs text-dim">Off by default. DDL is never available to agents.</span>
              </span>
            </label>
          </details>
        ) : null}

        {formErrors.length > 0 ? <ErrorNote message={formErrors.join(" ")} /> : null}
        <ErrorNote message={errText(create.error, "Creating the connection failed.")} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Connecting…" : "Create connection"}
          </Button>
        </div>
      </form>
    </Dialog>
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

        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-dim">
            Agent access
          </h3>
          {accessSummary.isPending ? <Spinner /> : accessSummary.data ? (
            <ConnectionAccessSummary summary={accessSummary.data} />
          ) : (
            <p className="text-xs text-danger">Access summary could not be loaded.</p>
          )}
        </section>

        <section>
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
