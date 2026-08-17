"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, CheckSquare, PauseCircle, Users, UsersRound } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/app-shell";
import { Badge, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useAuditEvents, useMembers, useOrgGraph, usePendingApprovalCount } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

interface ReadinessReport {
  status: "ok" | "degraded";
  dependencies: { name: string; status: "ok" | "error"; latency_ms: number }[];
}

function StatCard({
  label,
  value,
  icon: Icon,
  href,
}: {
  label: string;
  value: number | string;
  icon: typeof Bot;
  href: string;
}) {
  return (
    <Link
      href={href}
      className="group rounded-xl border border-line bg-surface p-5 transition-colors hover:border-line-strong hover:bg-raised"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-dim">{label}</span>
        <Icon size={16} className="text-faint group-hover:text-accent-strong" strokeWidth={1.8} />
      </div>
      <p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p>
    </Link>
  );
}

export default function OverviewPage() {
  const { workspace, can } = useWorkspace();
  const graph = useOrgGraph(workspace.workspace_id);
  const members = useMembers(workspace.workspace_id);
  const recentAudit = useAuditEvents(workspace.workspace_id, { limit: 8 }, can("admin"));
  const pendingApprovals = usePendingApprovalCount(workspace.workspace_id);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api<ReadinessReport>("/api/v1/health/ready"),
    refetchInterval: 15_000,
  });

  const agents = graph.data?.agents ?? [];
  const activeAgents = agents.filter((agent) => agent.status === "active").length;
  const pausedAgents = agents.filter((agent) => agent.status === "paused").length;

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Workspace · ${workspace.workspace_name}`}
        actions={
          health.data ? (
            <Badge tone={health.data.status === "ok" ? "ok" : "warn"}>
              stack {health.data.status}
            </Badge>
          ) : null
        }
      />
      <div className="space-y-8 px-8 py-6">
        {graph.isPending ? (
          <Spinner />
        ) : (
          <section className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StatCard
              label="Active agents"
              value={activeAgents}
              icon={Bot}
              href="/organization"
            />
            <StatCard
              label="Paused agents"
              value={pausedAgents}
              icon={PauseCircle}
              href="/organization"
            />
            <StatCard
              label="Pending approvals"
              value={pendingApprovals.data ?? "—"}
              icon={CheckSquare}
              href="/approvals"
            />
            <StatCard
              label="Teams"
              value={graph.data?.teams.length ?? 0}
              icon={UsersRound}
              href="/organization"
            />
            <StatCard
              label="Members"
              value={members.data?.length ?? "—"}
              icon={Users}
              href="/settings"
            />
          </section>
        )}

        <section className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-xl border border-line bg-surface">
            <header className="flex items-center justify-between border-b border-line px-5 py-3">
              <h2 className="text-sm font-semibold">System</h2>
            </header>
            <ul className="divide-y divide-line px-5">
              {(health.data?.dependencies ?? []).map((dep) => (
                <li key={dep.name} className="flex items-center justify-between py-2.5 text-sm">
                  <span className="capitalize">{dep.name}</span>
                  <span className="flex items-center gap-3">
                    <span className="text-xs tabular-nums text-faint">
                      {dep.latency_ms.toFixed(0)} ms
                    </span>
                    <Badge tone={dep.status === "ok" ? "ok" : "danger"}>{dep.status}</Badge>
                  </span>
                </li>
              ))}
              {health.isError ? (
                <li className="py-2.5 text-sm text-danger">API unreachable</li>
              ) : null}
            </ul>
          </div>

          <div className="rounded-xl border border-line bg-surface">
            <header className="flex items-center justify-between border-b border-line px-5 py-3">
              <h2 className="text-sm font-semibold">Recent activity</h2>
              {can("admin") ? (
                <Link
                  href="/audit"
                  className="flex items-center gap-1 text-xs text-accent-strong hover:underline"
                >
                  Audit log <ArrowRight size={12} />
                </Link>
              ) : null}
            </header>
            {can("admin") ? (
              <ul className="divide-y divide-line px-5">
                {(recentAudit.data?.events ?? []).map((event) => (
                  <li key={event.id} className="flex items-center justify-between py-2.5 text-sm">
                    <code className="text-[13px] text-ink">{event.action}</code>
                    <span className="text-xs tabular-nums text-faint">
                      {formatDateTime(event.created_at)}
                    </span>
                  </li>
                ))}
                {recentAudit.data && recentAudit.data.events.length === 0 ? (
                  <li className="py-2.5 text-sm text-dim">No activity yet.</li>
                ) : null}
              </ul>
            ) : (
              <p className="px-5 py-4 text-sm text-dim">
                Audit activity is visible to workspace admins.
              </p>
            )}
          </div>
        </section>

        <p className="text-xs text-faint">
          Spend and connector health cards arrive with Phases 5 and 10.
        </p>
      </div>
    </>
  );
}
