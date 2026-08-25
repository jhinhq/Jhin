"use client";

/** Apps — the single place connections live. The top level stays friendly (a
 * searchable library plus what is already connected); everything the old
 * Advanced → Connectors route offered (verify, rotate credentials, webhook
 * setup, discovered tools with risk overrides, agent access, enable/disable/
 * delete) opens in the per-connection drawer, with the operational controls
 * behind an "Advanced settings" disclosure. */

import { Plus } from "lucide-react";
import { useState } from "react";
import { AppIcon, AppLibrary } from "@/components/app-library";
import { PageHeader } from "@/components/app-shell";
import { Disclosure, LoadError, StatusPill } from "@/components/company/bits";
import { ConnectionDetailDialog, WebhookSecretDialog } from "@/components/connection-detail";
import { CreateConnectionDialog } from "@/components/connection-create-dialog";
import { ConnectorsGallery } from "@/components/connectors-gallery";
import { Button, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { connectTarget, type ConnectTarget } from "@/lib/apps";
import { formatDateTime } from "@/lib/format";
import {
  useAppCatalog,
  useConnections,
  useConnectors,
  useInvalidateConnections,
  useMarkConnectionWebhookConfigured,
} from "@/lib/hooks";
import type {
  CatalogApp,
  ConnectionInfo,
  ConnectorInfo,
  WebhookSetup,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";
import type { ConnectionPrefill } from "@/components/connection-create-dialog";

function connectionStatus(connection: ConnectionInfo): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  if (connection.status === "active") return { label: "Connected", tone: "ok" };
  if (connection.status === "error") return { label: "Needs attention", tone: "danger" };
  return { label: "Turned off", tone: "neutral" };
}

interface CreateTarget {
  connector: ConnectorInfo;
  prefill?: ConnectionPrefill;
}

export default function AppsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const connections = useConnections(workspaceId, isAdmin);
  const connectors = useConnectors();
  const catalog = useAppCatalog();
  const invalidate = useInvalidateConnections(workspaceId);
  const markWebhookConfigured = useMarkConnectionWebhookConfigured(workspaceId);

  const [createFor, setCreateFor] = useState<CreateTarget | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  /** The connection returned by POST, so the drawer opens before the list refetches. */
  const [created, setCreated] = useState<ConnectionInfo | null>(null);
  const [webhookOnce, setWebhookOnce] = useState<{
    connection: ConnectionInfo;
    webhook: WebhookSetup;
  } | null>(null);
  const [unsupported, setUnsupported] = useState<string | null>(null);

  const connectorList = connectors.data ?? [];
  const connectionList = connections.data ?? [];
  const connectorFor = (type: string) => connectorList.find((connector) => connector.connector_type === type);

  const detail =
    connectionList.find((connection) => connection.id === detailId) ??
    (created && created.id === detailId ? created : null);
  const justConnected = created !== null && created.id === detailId;

  const closeDetail = () => {
    setDetailId(null);
    setCreated(null);
  };

  const onConnect = (entry: CatalogApp) => {
    const resolved: ConnectTarget = connectTarget(entry, connectorList);
    if (resolved.kind === "unsupported") {
      setUnsupported(resolved.reason);
      return;
    }
    setUnsupported(null);
    setCreateFor({
      connector: resolved.connector,
      prefill:
        resolved.kind === "mcp"
          ? {
              name: resolved.prefill.name,
              authType: resolved.prefill.authType,
              config: resolved.prefill.config,
              hint: resolved.prefill.hint,
            }
          : {
              name: resolved.prefill.name,
              config: resolved.prefill.config,
              hint: resolved.prefill.hint,
            },
    });
  };

  const mcpConnector = connectorFor("mcp");
  const addApp =
    isAdmin && mcpConnector ? (
      <Button variant="primary" onClick={() => setCreateFor({ connector: mcpConnector })}>
        <Plus size={14} /> Any MCP server
      </Button>
    ) : null;

  return (
    <>
      <PageHeader
        title="Apps"
        description="Connect the apps your agents work with — GitHub, Notion, Slack, Stripe, and any app with an MCP server"
        actions={addApp}
      />
      <div className="space-y-8 px-4 py-5 sm:px-8 sm:py-6">
        {!isAdmin ? (
          <EmptyState
            title="Apps are managed by admins"
            description="Ask a workspace admin to connect the apps your agents need. You can still see what each agent can use on its profile."
          />
        ) : (
          <>
            <section>
              <h2 className="mb-3 font-display text-base font-semibold tracking-tight text-ink">Connected</h2>
              {connections.isPending ? (
                <Spinner label="Loading apps…" />
              ) : connections.isError ? (
                <LoadError what="your connected apps" onRetry={() => void connections.refetch()} />
              ) : connectionList.length === 0 ? (
                <p className="rounded-2xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
                  Nothing connected yet. Pick an app from the library below.
                </p>
              ) : (
                <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {connectionList.map((connection) => {
                    const connector = connectorFor(connection.connector_type);
                    const status = connectionStatus(connection);
                    const subtitle =
                      connection.connector_type === "mcp"
                        ? `MCP server · ${String(connection.config_json.server_slug ?? "")}`
                        : connector?.display_name ?? connection.connector_type;
                    return (
                      <li
                        key={connection.id}
                        data-testid={`connection-${connection.name}`}
                        className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-2.5">
                            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-raised text-ink">
                              <AppIcon icon={connector?.icon ?? connection.connector_type} />
                            </span>
                            <div className="min-w-0">
                              <h3 className="truncate font-display text-base font-semibold tracking-tight">{connection.name}</h3>
                              <p className="truncate text-xs text-dim">{subtitle}</p>
                            </div>
                          </div>
                          <StatusPill status={status} className="shrink-0" />
                        </div>
                        {connection.status === "error" && connection.last_error ? (
                          <p className="rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
                            {connection.last_error}. Open it to re-check or replace the credentials.
                          </p>
                        ) : null}
                        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-faint">
                          <span>
                            {connection.last_verified_at
                              ? `Checked ${formatDateTime(connection.last_verified_at)}`
                              : "Not checked yet"}
                          </span>
                          <Button
                            size="sm"
                            data-testid={`manage-${connection.name}`}
                            onClick={() => {
                              setCreated(null);
                              setDetailId(connection.id);
                            }}
                          >
                            Manage
                          </Button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            <section>
              <h2 className="mb-1 font-display text-base font-semibold tracking-tight text-ink">Library</h2>
              <p className="mb-3 text-sm text-dim">
                Apps with a built-in connector or an MCP server. Every tool an app offers still needs a grant before an agent can use it.
              </p>
              <ErrorNote message={unsupported} />
              {catalog.isPending || connectors.isPending ? (
                <Spinner label="Loading the library…" />
              ) : catalog.isError || !catalog.data ? (
                <LoadError what="the app library" onRetry={() => void catalog.refetch()} />
              ) : (
                <AppLibrary
                  entries={catalog.data}
                  connections={connectionList}
                  canManage={isAdmin}
                  onConnect={onConnect}
                  onOpenConnection={(connection) => {
                    setCreated(null);
                    setDetailId(connection.id);
                  }}
                />
              )}
            </section>

            <section>
              <Disclosure
                label="Connect by service type instead"
                openLabel="Hide service types"
              >
                <div className="space-y-3">
                  <p className="text-sm text-dim">
                    The built-in connectors, including the ones with no library entry — a command-line
                    sandbox, plain HTTP, web search, and any MCP server.
                  </p>
                  {connectors.isPending ? (
                    <Spinner label="Loading service types…" />
                  ) : connectors.isError ? (
                    <LoadError what="the service types" onRetry={() => void connectors.refetch()} />
                  ) : (
                    <ConnectorsGallery
                      connectors={connectorList}
                      canManage={isAdmin}
                      onConnect={(connector) => setCreateFor({ connector })}
                    />
                  )}
                </div>
              </Disclosure>
            </section>
          </>
        )}
      </div>

      {createFor ? (
        <CreateConnectionDialog
          workspaceId={workspaceId}
          connector={createFor.connector}
          prefill={createFor.prefill}
          onClose={() => setCreateFor(null)}
          onCreated={(result) => {
            invalidate();
            setCreateFor(null);
            if (result.webhook) {
              // The one-time secret is the moment for webhook connectors;
              // the drawer is one click away on the new card.
              setWebhookOnce({ connection: result.connection, webhook: result.webhook });
            } else {
              setCreated(result.connection);
              setDetailId(result.connection.id);
            }
          }}
        />
      ) : null}

      {webhookOnce ? (
        <WebhookSecretDialog
          workspaceId={workspaceId}
          connectionId={webhookOnce.connection.id}
          connectionName={webhookOnce.connection.name}
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
          connector={connectorFor(detail.connector_type)}
          canManage={isAdmin}
          title={justConnected ? `${detail.name} is connected` : undefined}
          intro={
            justConnected
              ? "Here is what it offers. Give an agent access from its profile under Tools & Access."
              : undefined
          }
          initialTab={justConnected ? "tools" : "overview"}
          onClose={closeDetail}
          onChanged={() => invalidate()}
          onRemoved={() => {
            invalidate();
            closeDetail();
          }}
        />
      ) : null}
    </>
  );
}
