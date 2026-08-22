"use client";

/** Apps: a friendly, read-mostly view of connected apps. Adding and
 * configuring stays in Advanced → Connectors. */

import { ExternalLink, GitBranch, Plug, Plus, Terminal } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/app-shell";
import { LoadError, StatusPill } from "@/components/company/bits";
import { Button, EmptyState, Spinner } from "@/components/ui";
import { formatDateTime } from "@/lib/format";
import { useConnections, useConnectors } from "@/lib/hooks";
import type { ConnectionInfo } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function ConnectorIcon({ icon }: { icon: string }) {
  if (icon === "github") return <GitBranch size={18} aria-hidden />;
  if (icon === "terminal") return <Terminal size={18} aria-hidden />;
  return <Plug size={18} aria-hidden />;
}

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
  const connectorFor = (type: string) => connectors.data?.find((connector) => connector.connector_type === type);

  const addApp = isAdmin ? (
    <Link href="/connectors">
      <Button variant="primary">
        <Plus size={14} /> Add an app
      </Button>
    </Link>
  ) : null;

  return (
    <>
      <PageHeader title="Apps" description="The tools your agents can reach — GitHub, Linear, and more" actions={addApp} />
      <div className="space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        {!isAdmin ? (
          <EmptyState
            title="Apps are managed by admins"
            description="Ask a workspace admin to connect the apps your agents need. You can still see what each agent can use on its profile."
          />
        ) : connections.isPending ? (
          <Spinner label="Loading apps…" />
        ) : connections.isError || !connections.data ? (
          <LoadError what="your connected apps" onRetry={() => void connections.refetch()} />
        ) : connections.data.length === 0 ? (
          <EmptyState
            title="No apps connected yet"
            description="Connect GitHub, Linear, or a command-line sandbox so your agents can read and act on real work."
            action={addApp ?? undefined}
          />
        ) : (
          <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {connections.data.map((connection) => {
              const connector = connectorFor(connection.connector_type);
              const status = connectionStatus(connection);
              return (
                <li key={connection.id} className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-2.5">
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-raised text-ink">
                        <ConnectorIcon icon={connector?.icon ?? connection.connector_type} />
                      </span>
                      <div className="min-w-0">
                        <h3 className="truncate font-display text-base font-semibold tracking-tight">{connection.name}</h3>
                        <p className="truncate text-xs text-dim">{connector?.display_name ?? connection.connector_type}</p>
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
                    <Link href="/connectors" className="inline-flex items-center gap-1 text-accent-strong hover:underline">
                      <ExternalLink size={12} aria-hidden /> Open in Advanced
                    </Link>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );
}
