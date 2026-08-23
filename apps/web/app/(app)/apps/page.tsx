"use client";

/** Apps: a library of well-known apps plus what is already connected.
 * Connecting opens the shared connection dialog — pre-filled for apps that
 * speak MCP, or the native connector when Jhin has one. Deeper configuration
 * stays in Advanced → Connectors. */

import { ExternalLink, Plus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { AppIcon, AppLibrary } from "@/components/app-library";
import { PageHeader } from "@/components/app-shell";
import { LoadError, StatusPill } from "@/components/company/bits";
import { CreateConnectionDialog } from "@/components/connection-create-dialog";
import { ConnectionTools } from "@/components/connection-tools";
import { Button, Dialog, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { connectTarget, type ConnectTarget } from "@/lib/apps";
import { formatDateTime } from "@/lib/format";
import {
  useAppCatalog,
  useConnections,
  useConnectionTools,
  useConnectors,
  useInvalidateConnections,
} from "@/lib/hooks";
import type { CatalogApp, ConnectionInfo } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function connectionStatus(connection: ConnectionInfo): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  if (connection.status === "active") return { label: "Connected", tone: "ok" };
  if (connection.status === "error") return { label: "Needs attention", tone: "danger" };
  return { label: "Turned off", tone: "neutral" };
}

export default function AppsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const connections = useConnections(workspaceId, isAdmin);
  const connectors = useConnectors();
  const catalog = useAppCatalog();
  const invalidate = useInvalidateConnections(workspaceId);
  const [target, setTarget] = useState<{ entry: CatalogApp; target: ConnectTarget } | null>(null);
  const [manage, setManage] = useState<ConnectionInfo | null>(null);
  const [created, setCreated] = useState<ConnectionInfo | null>(null);
  const [unsupported, setUnsupported] = useState<string | null>(null);

  const connectorFor = (type: string) => connectors.data?.find((connector) => connector.connector_type === type);
  const connectionList = connections.data ?? [];

  const onConnect = (entry: CatalogApp) => {
    const resolved = connectTarget(entry, connectors.data ?? []);
    if (resolved.kind === "unsupported") {
      setUnsupported(resolved.reason);
      return;
    }
    setUnsupported(null);
    setTarget({ entry, target: resolved });
  };

  const addApp = isAdmin ? (
    <Link href="/connectors">
      <Button variant="primary">
        <Plus size={14} /> Any MCP server
      </Button>
    </Link>
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
                      <li key={connection.id} className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5">
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
                            {connection.last_error}. Re-check the credentials in Advanced → Connectors.
                          </p>
                        ) : null}
                        <div className="mt-auto flex items-center justify-between gap-2 pt-1 text-xs text-faint">
                          <span>
                            {connection.last_verified_at
                              ? `Checked ${formatDateTime(connection.last_verified_at)}`
                              : "Not checked yet"}
                          </span>
                          <span className="flex items-center gap-3">
                            <button
                              type="button"
                              className="text-accent-strong hover:underline"
                              onClick={() => setManage(connection)}
                            >
                              Tools
                            </button>
                            <Link href="/connectors" className="inline-flex items-center gap-1 text-accent-strong hover:underline">
                              <ExternalLink size={12} aria-hidden /> Open in Advanced
                            </Link>
                          </span>
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
                  onOpenConnection={setManage}
                />
              )}
            </section>
          </>
        )}
      </div>

      {target && target.target.kind !== "unsupported" ? (
        <CreateConnectionDialog
          workspaceId={workspaceId}
          connector={target.target.connector}
          prefill={
            target.target.kind === "mcp"
              ? {
                  name: target.target.prefill.name,
                  authType: target.target.prefill.authType,
                  config: target.target.prefill.config,
                  hint: target.target.prefill.hint,
                }
              : { name: target.entry.name }
          }
          onClose={() => setTarget(null)}
          onCreated={(result) => {
            invalidate();
            setTarget(null);
            setCreated(result.connection);
          }}
        />
      ) : null}

      {created ? (
        <ConnectionToolsDialog
          workspaceId={workspaceId}
          connection={created}
          title={`${created.name} is connected`}
          intro="Here is what it offers. Give an agent access from its profile under Tools & Access."
          onClose={() => setCreated(null)}
        />
      ) : manage ? (
        <ConnectionToolsDialog
          workspaceId={workspaceId}
          connection={manage}
          title={manage.name}
          intro="Tools this app offers and how risky each one is."
          onClose={() => setManage(null)}
        />
      ) : null}
    </>
  );
}

function ConnectionToolsDialog({
  workspaceId,
  connection,
  title,
  intro,
  onClose,
}: {
  workspaceId: string;
  connection: ConnectionInfo;
  title: string;
  intro: string;
  onClose: () => void;
}) {
  const tools = useConnectionTools(workspaceId, connection.id);
  const invalidate = useInvalidateConnections(workspaceId);
  return (
    <Dialog title={title} open onClose={onClose} wide>
      <div className="space-y-3">
        <p className="text-sm text-dim">{intro}</p>
        <ConnectionTools
          workspaceId={workspaceId}
          connectionId={connection.id}
          data={tools.data}
          isPending={tools.isPending}
          error={tools.error}
          canManage
          onChanged={() => invalidate()}
        />
        <div className="flex justify-end gap-2">
          <Link href="/connectors">
            <Button variant="ghost">Open in Advanced</Button>
          </Link>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
