"use client";

/** Automations: a friendly, read-mostly view of triggers. The full builder
 * stays in Advanced → Triggers. */

import { ExternalLink, Zap } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/app-shell";
import { Avatar } from "@/components/avatar";
import { triggerWhen } from "@/components/company/agent-helpers";
import { LoadError, StatusPill } from "@/components/company/bits";
import { Button, EmptyState, Spinner } from "@/components/ui";
import { timeAgo } from "@/lib/activity";
import { formatDateTime } from "@/lib/format";
import { useAgents, useConnections, useTriggers } from "@/lib/hooks";
import type { Trigger } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function onOffText(trigger: Trigger): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  // An automation switched off because its agent was deleted reads as an
  // ordinary "Off" otherwise, and nobody goes looking for the reason.
  if (trigger.target_state === "agent_deleted") return { label: "Needs an agent", tone: "warn" };
  if (!trigger.enabled) return { label: "Off", tone: "neutral" };
  if (trigger.target_warning) return { label: "On, but stuck", tone: "warn" };
  return { label: "On", tone: "ok" };
}

function lastRunText(trigger: Trigger): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  const last = trigger.last_invocation;
  if (!last) return { label: "Hasn’t run yet", tone: "neutral" };
  const when = timeAgo(last.created_at);
  if (last.status === "started") return { label: `Ran ${when}`, tone: "ok" };
  if (last.status === "duplicate") return { label: `Skipped a repeat ${when}`, tone: "neutral" };
  return { label: `Failed ${when}`, tone: "danger" };
}

export default function AutomationsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const triggers = useTriggers(workspaceId);
  const agents = useAgents(workspaceId);
  const connections = useConnections(workspaceId, can("admin"));
  const agentName = (id: string | null) => (id ? agents.data?.find((agent) => agent.id === id)?.name : undefined);
  const connectionName = (id: string | null) =>
    id ? connections.data?.find((connection) => connection.id === id)?.name : undefined;

  const advanced = (
    <Link href="/triggers">
      <Button>
        <ExternalLink size={14} /> Open in Advanced
      </Button>
    </Link>
  );

  return (
    <>
      <PageHeader
        title="Automations"
        description="Automations watch your apps and hand work to an agent"
        actions={advanced}
      />
      <div className="space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        {triggers.isPending ? (
          <Spinner label="Loading automations…" />
        ) : triggers.isError || !triggers.data ? (
          <LoadError what="your automations" onRetry={() => void triggers.refetch()} />
        ) : triggers.data.length === 0 ? (
          <EmptyState
            title="No automations yet"
            description="An automation waits for something to happen in one of your apps — a new issue, a merged pull request — and hands the work to an agent. Build one in Advanced."
            action={can("admin") ? advanced : undefined}
          />
        ) : (
          <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {triggers.data.map((trigger) => {
              const agent = agentName(trigger.target_agent_id);
              const run = lastRunText(trigger);
              return (
                <li key={trigger.id} className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-2.5">
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
                        <Zap size={16} aria-hidden />
                      </span>
                      <h3 className="truncate font-display text-base font-semibold tracking-tight">{trigger.name}</h3>
                    </div>
                    <StatusPill status={onOffText(trigger)} className="shrink-0" />
                  </div>
                  <p className="text-sm text-ink/90">
                    When {triggerWhen(trigger.event_type, connectionName(trigger.connection_id))}
                    {" → "}
                    {agent ? (
                      <>
                        assign to{" "}
                        <Link
                          href={trigger.target_agent_id ? `/agents/${trigger.target_agent_id}` : "/agents"}
                          className="inline-flex items-center gap-1 font-medium text-accent-strong hover:underline"
                        >
                          <Avatar name={agent} size="xs" /> {agent}
                        </Link>
                      </>
                    ) : trigger.target_team_id ? (
                      "hand to the team"
                    ) : (
                      "assign to an agent"
                    )}
                  </p>
                  <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                    <StatusPill status={run} />
                    {trigger.last_invocation ? (
                      <time
                        dateTime={trigger.last_invocation.created_at}
                        title={formatDateTime(trigger.last_invocation.created_at)}
                        className="text-xs text-faint"
                      >
                        {formatDateTime(trigger.last_invocation.created_at)}
                      </time>
                    ) : null}
                  </div>
                  {trigger.target_warning ? (
                    <p className="rounded-xl border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn">
                      {trigger.target_warning}
                    </p>
                  ) : null}
                  {trigger.last_invocation?.status === "failed" && trigger.last_invocation.error_message ? (
                    <p className="rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
                      Last run failed. {trigger.last_invocation.error_message}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </>
  );
}
