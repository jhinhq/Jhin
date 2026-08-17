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
import { PageHeader } from "@/components/app-shell";
import { ConnectorsGallery } from "@/components/connectors-gallery";
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
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  findAuthScheme,
  validateConnectionForm,
  webhookPayloadUrl,
} from "@/lib/connectors";
import { formatDateTime } from "@/lib/format";
import {
  useConnections,
  useConnectionToolCalls,
  useConnectors,
  useInvalidateConnections,
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

  const [createFor, setCreateFor] = useState<ConnectorInfo | null>(null);
  const [webhookOnce, setWebhookOnce] = useState<{
    connectionName: string;
    webhook: WebhookSetup;
  } | null>(null);
  const [detail, setDetail] = useState<ConnectionInfo | null>(null);

  if (connectors.isPending) {
    return (
      <>
        <PageHeader title="Connectors" />
        <div className="px-8 py-6">
          <Spinner label="Loading connectors…" />
        </div>
      </>
    );
  }

  const connectorList = connectors.data ?? [];
  const connectionList = connections.data ?? [];
  const detailConnector = detail
    ? connectorList.find((c) => c.connector_type === detail.connector_type)
    : undefined;

  return (
    <>
      <PageHeader
        title="Connectors"
        description="Authenticated integrations with per-connection scoping and webhooks"
      />
      <div className="space-y-8 px-8 py-6">
        <section>
          <h2 className="mb-3 text-sm font-semibold text-dim">Available connectors</h2>
          <ConnectorsGallery
            connectors={connectorList}
            canManage={isAdmin}
            onConnect={setCreateFor}
          />
        </section>

        {isAdmin ? (
          <section>
            <h2 className="mb-3 text-sm font-semibold text-dim">Connections</h2>
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
                    onClick={() => setDetail(connection)}
                    className="flex flex-col gap-2 rounded-xl border border-line bg-surface px-5 py-4 text-left transition-colors hover:border-line-strong"
                  >
                    <header className="flex items-center justify-between gap-2">
                      <h3 className="min-w-0 truncate text-sm font-semibold">
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
                      <p className="truncate rounded-md bg-danger/10 px-2 py-1 text-xs text-danger">
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
      </div>

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
                connectionName: created.connection.name,
                webhook: created.webhook,
              });
            }
          }}
        />
      ) : null}

      {webhookOnce ? (
        <WebhookSecretDialog
          connectionName={webhookOnce.connectionName}
          webhook={webhookOnce.webhook}
          onClose={() => setWebhookOnce(null)}
        />
      ) : null}

      {detail ? (
        <ConnectionDetailDialog
          workspaceId={workspaceId}
          connection={detail}
          connector={detailConnector}
          onClose={() => setDetail(null)}
          onChanged={() => {
            invalidate();
            setDetail(null);
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
  const [config, setConfig] = useState<Record<string, string>>({});
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
          config: Object.fromEntries(
            Object.entries(config).filter(([, value]) => value.trim() !== ""),
          ),
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
                className="font-mono text-[12px]"
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

        {connector.config_fields.map((field) => (
          <Field key={field.name} label={field.label} hint={field.help}>
            <Input
              value={config[field.name] ?? ""}
              onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.value }))}
              placeholder={field.placeholder}
              required={field.required}
            />
          </Field>
        ))}

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
        <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-raised px-2.5 py-1.5 text-xs">
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
  connectionName,
  webhook,
  onClose,
}: {
  connectionName: string;
  webhook: WebhookSetup;
  onClose: () => void;
}) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return (
    <Dialog title="Webhook setup — shown once" open onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-dim">
          Configure the provider webhook for <strong className="text-ink">{connectionName}</strong>{" "}
          with this payload URL and signing secret. The secret is{" "}
          <strong className="text-ink">not retrievable later</strong> — store it now.
        </p>
        <CopyRow label="Payload URL" value={webhookPayloadUrl(webhook.url_path, origin)} />
        <CopyRow label="Signing secret" value={webhook.secret} />
        <p className="rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
          Choose content type “application/json” and secret-based HMAC (SHA-256) signing.
          Deliveries with a missing or invalid signature are rejected.
        </p>
        <div className="flex justify-end">
          <Button variant="primary" onClick={onClose}>
            I stored the secret
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

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
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [rotating, setRotating] = useState(false);
  const [rotateCredentials, setRotateCredentials] = useState<Record<string, string>>({});
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
          <dl className="space-y-1 rounded-lg border border-line bg-raised px-3 py-2 text-xs">
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
            className={`flex items-start gap-1.5 rounded-md px-2.5 py-1.5 text-xs ${
              verifyResult.ok ? "bg-ok/10 text-ok" : "bg-danger/10 text-danger"
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
          <p className="rounded-md bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
            {connection.last_error}
          </p>
        ) : null}

        {connector?.supports_webhooks ? (
          <div className="space-y-2 rounded-lg border border-line bg-surface px-3 py-2.5">
            <p className="flex items-center gap-1.5 text-xs font-medium">
              <Webhook size={13} /> Webhook
            </p>
            <CopyRow
              label="Payload URL"
              value={webhookPayloadUrl(
                `/api/v1/webhooks/${connection.connector_type}/${connection.public_id}`,
                origin,
              )}
            />
            <p className="text-[11px] text-faint">
              The signing secret was shown once at creation and cannot be retrieved. Delete and
              recreate the connection if it was lost.
            </p>
          </div>
        ) : null}

        {rotating ? (
          <form
            className="space-y-3 rounded-lg border border-accent/40 bg-accent-soft/30 px-3 py-3"
            onSubmit={(event) => {
              event.preventDefault();
              rotate.mutate();
            }}
          >
            <p className="text-xs font-medium">Re-enter credentials ({scheme?.label})</p>
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
                    className="font-mono text-[12px]"
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
                  className="flex items-center gap-2 rounded-md border border-line bg-raised px-2.5 py-1.5 text-xs"
                >
                  <code className="min-w-0 flex-1 truncate">{call.tool_name}</code>
                  <Badge tone={call.status === "completed" ? "ok" : "danger"}>
                    {call.status}
                  </Badge>
                  <span className="shrink-0 text-faint">{formatDateTime(call.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <footer className="flex items-center gap-2 border-t border-line pt-3">
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
