"use client";

/** Everything you can do to one connected app, in a single drawer: status and
 * agent access up front, the discovered tools on their own tab, and the
 * operational controls (rotate credentials, webhook secret, disable, delete)
 * behind an "Advanced settings" disclosure. This used to be a separate
 * `/connectors` route; it now opens from Apps so there is one place to look. */

import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  Copy,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserRound,
  Webhook,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ReconnectButton } from "@/components/connect/reconnect-banner";
import { ConnectionAccessSummary } from "@/components/connection-access-summary";
import { ConnectionTools } from "@/components/connection-tools";
import { Disclosure } from "@/components/company/bits";
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
  Tabs,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  agentAccessHref,
  allowedRepositoriesOf,
  bundleForConnector,
  sandboxRepositoryError,
} from "@/lib/bundles";
import { findAuthScheme, webhookPayloadUrl } from "@/lib/connectors";
import { formatDateTime } from "@/lib/format";
import {
  useAgents,
  useConnectionAccessSummary,
  useConnectionToolCalls,
  useConnectionTools,
  useInvalidateConnections,
  useUpdateConnectionConfig,
} from "@/lib/hooks";
import type {
  ConnectionDeleteImpact,
  ConnectionInfo,
  ConnectorInfo,
  VerifyResult,
  WebhookSetup,
} from "@/lib/types";

export function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** What else goes when this connection goes. Automations built on a
 * connection are deleted with it, along with everything they have ever run,
 * and neither comes back — so the delete is only informed if it says so. */
export function impactSentence(impact: ConnectionDeleteImpact | undefined): string | null {
  if (!impact) return null;
  const grants = impact.grant_count ?? 0;
  const revokes =
    grants > 0
      ? ` and revoke ${plural(grants, "grant", "grants")} on ${plural(impact.agent_count ?? 0, "agent", "agents")}`
      : "";
  if (impact.trigger_count === 0) {
    return grants > 0 ? `Deleting also revokes${revokes.slice(" and revoke".length)}.` : null;
  }
  const automations = plural(impact.trigger_count, "automation", "automations");
  if (impact.trigger_invocation_count === 0) {
    return `Deleting also removes ${automations} built on this app${revokes}.`;
  }
  const runs = plural(impact.trigger_invocation_count, "recorded run", "recorded runs");
  return `Deleting also removes ${automations} built on this app, and their ${runs}${revokes}.`;
}

/** "Give to an agent…": pick an agent, land on its Tools & Access tab with
 * the right bundle's setup dialog open on this connection. */
export function GiveToAgent({
  workspaceId,
  connection,
}: {
  workspaceId: string;
  connection: ConnectionInfo;
}) {
  const agents = useAgents(workspaceId);
  const [open, setOpen] = useState(false);
  const [agentId, setAgentId] = useState("");
  const defaultBundle = bundleForConnector(connection.connector_type);
  const [bundleId, setBundleId] = useState(defaultBundle ?? "");
  const active = (agents.data ?? []).filter((agent) => agent.status !== "disabled");
  const bundleChoices =
    connection.connector_type === "github"
      ? [
          ["github-read", "GitHub (read)"],
          ["code-editing", "Code editing"],
        ]
      : defaultBundle
        ? [[defaultBundle, defaultBundle === "web-access" ? "Web search & browsing" : "Code editing"]]
        : [];
  return (
    <div data-testid="give-to-agent" className="space-y-2">
      {!open ? (
        <Button size="sm" variant="primary" onClick={() => setOpen(true)}>
          Give to an agent…
        </Button>
      ) : defaultBundle === null ? (
        <p className="text-xs text-dim">
          No capability bundle for this app yet; grant tools under the agent&rsquo;s Tools &amp; Access.
        </p>
      ) : (
        <div className="flex flex-wrap items-end gap-2 rounded-xl border border-line bg-raised px-3.5 py-3">
          <div className="min-w-[12rem] flex-1">
            <Field label="Agent">
              <Select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                <option value="">Choose an agent…</option>
                {active.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                    {agent.role_title ? ` — ${agent.role_title}` : ""}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <div className="min-w-[10rem]">
            <Field label="Capability">
              <Select value={bundleId} onChange={(event) => setBundleId(event.target.value)}>
                {bundleChoices.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          {agentId ? (
            <Link
              href={agentAccessHref(agentId, bundleId, connection.id)}
              data-testid="give-to-agent-link"
              className="inline-flex items-center rounded-xl bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-strong"
            >
              Open Tools &amp; Access
            </Link>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** The repositories a CLI Sandbox may use — the outer limit under every
 * agent's grants — editable in place through PATCH /config. */
export function AllowedRepositoriesEditor({
  workspaceId,
  connection,
}: {
  workspaceId: string;
  connection: ConnectionInfo;
}) {
  const [text, setText] = useState(allowedRepositoriesOf(connection).join("\n"));
  const [saved, setSaved] = useState(false);
  const update = useUpdateConnectionConfig(workspaceId, connection.id);
  const entries = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  const invalid = entries.some((entry) => sandboxRepositoryError(entry) !== null);
  return (
    <form
      data-testid="allowed-repositories"
      className="space-y-2 rounded-xl border border-line bg-raised px-3.5 py-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (invalid) return;
        setSaved(false);
        update.mutate(
          { ...connection.config_json, allowed_repositories: entries },
          { onSuccess: () => setSaved(true) },
        );
      }}
    >
      <Field label="Allowed repositories" hint="One owner/name per line; owner/* and * work too. Empty means no repository work at all.">
        <Textarea
          rows={3}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setSaved(false);
          }}
          className="font-mono text-xs"
        />
      </Field>
      {invalid ? (
        <p role="alert" className="text-xs text-danger">
          Every entry must be owner/name, owner/*, or *.
        </p>
      ) : null}
      <ErrorNote message={errText(update.error, "Saving the repositories failed.")} />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={invalid || update.isPending}>
          {update.isPending ? "Saving…" : "Save"}
        </Button>
        {saved ? <span className="text-xs text-ok">Saved.</span> : null}
      </div>
    </form>
  );
}

export function statusTone(status: string): "ok" | "danger" | "neutral" | "warn" {
  if (status === "active") return "ok";
  if (status === "error") return "danger";
  // A lapsed sign-in is a nudge, not a failure: the setup is intact.
  if (status === "needs_reauth") return "warn";
  return "neutral";
}

/** The machine word a person should not have to read. */
export function statusLabel(status: string): string {
  return status === "needs_reauth" ? "needs reconnecting" : status;
}

export function CopyRow({ label, value }: { label: string; value: string }) {
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

/** Shown once, right after a connection that supports webhooks is created:
 * the payload URL plus either the generated signing secret (never retrievable
 * again) or a form for the provider-generated one. */
export function WebhookSecretDialog({
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

/** The full detail view for one connection. Admin-only actions (rotate,
 * disable, delete, webhook secret, risk overrides) render only when
 * `canManage` is true. */
export function ConnectionDetailDialog({
  workspaceId,
  connection,
  connector,
  canManage,
  title,
  intro,
  initialTab = "overview",
  onClose,
  onChanged,
  onRemoved,
}: {
  workspaceId: string;
  connection: ConnectionInfo;
  connector: ConnectorInfo | undefined;
  canManage: boolean;
  /** Defaults to the connection name; the just-connected moment overrides it. */
  title?: string;
  /** One friendly line above the tabs, used right after connecting. */
  intro?: string;
  initialTab?: "overview" | "tools";
  onClose: () => void;
  /** A change that keeps the connection around (verify, rotate, enable…). */
  onChanged: () => void;
  /** The connection is gone; the caller should close the drawer. */
  onRemoved: () => void;
}) {
  const base = `/api/v1/workspaces/${workspaceId}/connections/${connection.id}`;
  const toolCalls = useConnectionToolCalls(workspaceId, connection.id);
  const accessSummary = useConnectionAccessSummary(workspaceId, connection.id);
  const tools = useConnectionTools(workspaceId, connection.id);
  const invalidateAll = useInvalidateConnections(workspaceId);
  const [tab, setTab] = useState<"overview" | "tools">(initialTab);
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [rotating, setRotating] = useState(false);
  const [rotateCredentials, setRotateCredentials] = useState<Record<string, string>>({});
  const [webhookSecret, setWebhookSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const scheme = findAuthScheme(connector, connection.auth_type);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  const verify = useMutation({
    mutationFn: () => api<VerifyResult>(`${base}/verify`, { method: "POST" }),
    onSuccess: (result) => {
      setError(null);
      setVerifyResult(result);
      onChanged();
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
    onSuccess: onRemoved,
    onError: (err) => setError(errText(err, "Deleting the connection failed.")),
    // Close the confirm either way so the error note is readable, not stuck
    // behind the confirm's backdrop.
    onSettled: () => setConfirmingDelete(false),
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
    <Dialog title={title ?? connection.name} open onClose={onClose} wide>
      <div className="space-y-5">
        <ErrorNote message={error} />
        {intro ? <p className="text-sm text-dim">{intro}</p> : null}

        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={statusTone(connection.status)}>{statusLabel(connection.status)}</Badge>
          <Badge tone="neutral">{connection.connector_type}</Badge>
          <Badge tone="neutral">
            <KeyRound size={11} /> {scheme?.label ?? connection.auth_type}
          </Badge>
          {connection.authorized_by ? (
            <Badge tone="neutral">
              <UserRound size={11} /> {connection.authorized_by.display_name}
            </Badge>
          ) : null}
          <span className="ml-auto text-xs text-faint">
            Last checked:{" "}
            {connection.last_verified_at ? formatDateTime(connection.last_verified_at) : "never"}
          </span>
        </div>

        {connection.status === "needs_reauth" ? (
          /* The one thing to do about this connection, above everything that
           * cannot work until it is done. Re-authorizing keeps the row, its
           * name, its config, and every grant that points at it. */
          <div
            data-testid="connection-needs-reauth"
            className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warn/40 bg-warn-soft px-3.5 py-3"
          >
            <p className="min-w-0 text-[13px] leading-relaxed text-ink">
              This app&rsquo;s sign-in lapsed, so agents cannot use it. Reconnecting keeps the setup
              and every grant exactly as they are.
              {connection.authorized_by
                ? ` It was connected by ${connection.authorized_by.display_name}.`
                : ""}
            </p>
            {canManage ? (
              <ReconnectButton workspaceId={workspaceId} connection={connection} />
            ) : null}
          </div>
        ) : null}

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
              verifyResult.status === "disabled"
                ? // A passing check on a turned-off app is not a green light.
                  "bg-warn-soft text-warn"
                : verifyResult.ok
                  ? "bg-ok-soft text-ok"
                  : "bg-danger-soft text-danger"
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
              canManage={canManage}
              onChanged={() => invalidateAll()}
            />
          </section>
        ) : null}

        {canManage ? (
          <section hidden={tab !== "overview"}>
            <GiveToAgent workspaceId={workspaceId} connection={connection} />
          </section>
        ) : null}

        {canManage && connection.connector_type === "cli" ? (
          <section hidden={tab !== "overview"}>
            <AllowedRepositoriesEditor workspaceId={workspaceId} connection={connection} />
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

        {canManage ? (
          <section hidden={tab !== "overview"} data-testid="connection-advanced">
            <Disclosure label="Advanced settings" openLabel="Hide advanced settings">
              <div className="space-y-4">
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

                <div className="flex flex-wrap items-center gap-2">
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
                    onClick={() => setConfirmingDelete(true)}
                  >
                    <Trash2 size={13} /> Delete
                  </Button>
                </div>
                <p className="text-xs text-faint">
                  Disabling keeps the setup but stops every agent from using it. Deleting is
                  permanent and breaks grants that reference this connection.
                  {impactSentence(accessSummary.data?.delete_impact)
                    ? ` ${impactSentence(accessSummary.data?.delete_impact)}`
                    : ""}
                </p>
              </div>
            </Disclosure>
          </section>
        ) : null}

        <footer className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          {canManage ? (
            <Button size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
              <ShieldCheck size={13} /> {verify.isPending ? "Checking…" : "Verify"}
            </Button>
          ) : null}
          <Button size="sm" variant="primary" className="ml-auto" onClick={onClose}>
            Done
          </Button>
        </footer>
      </div>
      {confirmingDelete ? (
        <ConfirmDialog
          open
          title={`Delete “${connection.name}”?`}
          body={[
            "Grants that reference it stop working.",
            impactSentence(accessSummary.data?.delete_impact),
            "This cannot be undone.",
          ]
            .filter(Boolean)
            .join(" ")}
          confirmLabel="Delete connection"
          busy={remove.isPending}
          onConfirm={() => remove.mutate()}
          onClose={() => setConfirmingDelete(false)}
        />
      ) : null}
    </Dialog>
  );
}
