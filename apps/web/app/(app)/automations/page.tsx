"use client";

/** Automations: the one place they live. Friendly plain-language cards over
 * the workspace's triggers, with the full builder (WHEN / IF / THEN, presets,
 * dry-run test) right here — creating, editing, switching on and off, and
 * deleting never leave the page. */

import { useMutation } from "@tanstack/react-query";
import { History, Pencil, Plus, Power, Trash2, Zap } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { AutomationBuilder } from "@/components/automations/builder";
import { Avatar } from "@/components/avatar";
import { triggerWhen } from "@/components/company/agent-helpers";
import { Chip, LoadError, StatusPill } from "@/components/company/bits";
import { Badge, Button, ConfirmDialog, EmptyState, ErrorNote, focusRing, Spinner } from "@/components/ui";
import { timeAgo } from "@/lib/activity";
import { api, errorText } from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import { useAgents, useConnections, useInvalidateTriggers, useTriggerInvocations, useTriggers } from "@/lib/hooks";
import type { Trigger, TriggerInvocation } from "@/lib/types";
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

const RUN_TONE: Record<string, "ok" | "neutral" | "danger"> = {
  started: "ok",
  duplicate: "neutral",
  failed: "danger",
};

const RUN_LABEL: Record<string, string> = {
  started: "Started a task",
  duplicate: "Skipped a repeat",
  failed: "Failed",
};

export default function AutomationsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const triggers = useTriggers(workspaceId);
  const agents = useAgents(workspaceId);
  const connections = useConnections(workspaceId, isAdmin);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [editing, setEditing] = useState<Trigger | null>(null);
  const agentName = (id: string | null) => (id ? agents.data?.find((agent) => agent.id === id)?.name : undefined);
  const connectionName = (id: string | null) =>
    id ? connections.data?.find((connection) => connection.id === id)?.name : undefined;

  const newAutomation = (
    <Button
      variant="primary"
      onClick={() => {
        setEditing(null);
        setBuilderOpen(true);
      }}
    >
      <Plus size={14} /> New automation
    </Button>
  );

  return (
    <>
      <PageHeader
        title="Automations"
        description="Automations watch your apps and hand work to an agent"
        actions={isAdmin ? newAutomation : undefined}
      />
      <PageBody className="space-y-5">
        {triggers.isPending ? (
          <Spinner label="Loading automations…" />
        ) : triggers.isError || !triggers.data ? (
          <LoadError what="your automations" onRetry={() => void triggers.refetch()} />
        ) : triggers.data.length === 0 ? (
          <EmptyState
            title="No automations yet"
            description="An automation waits for something to happen in one of your apps — a new issue, a merged pull request — and hands the work to an agent. It keeps watch so nobody has to."
            action={isAdmin ? newAutomation : undefined}
          />
        ) : (
          <ul className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
            {triggers.data.map((trigger) => (
              <AutomationCard
                key={trigger.id}
                trigger={trigger}
                agentName={agentName(trigger.target_agent_id)}
                connectionName={connectionName(trigger.connection_id)}
                isAdmin={isAdmin}
                onEdit={() => {
                  setEditing(trigger);
                  setBuilderOpen(true);
                }}
              />
            ))}
          </ul>
        )}
      </PageBody>
      {builderOpen ? (
        <AutomationBuilder
          existing={editing}
          onClose={() => {
            setBuilderOpen(false);
            setEditing(null);
          }}
        />
      ) : null}
    </>
  );
}

function AutomationCard({
  trigger,
  agentName,
  connectionName,
  isAdmin,
  onEdit,
}: {
  trigger: Trigger;
  agentName: string | undefined;
  connectionName: string | undefined;
  isAdmin: boolean;
  onEdit: () => void;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const invalidate = useInvalidateTriggers(workspaceId);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = lastRunText(trigger);

  const toggle = useMutation({
    mutationFn: () =>
      api(
        `/api/v1/workspaces/${workspaceId}/triggers/${trigger.id}/${trigger.enabled ? "disable" : "enable"}`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(errorText(err, "The switch didn’t take. Try again.")),
  });

  const remove = useMutation({
    mutationFn: () => api(`/api/v1/workspaces/${workspaceId}/triggers/${trigger.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmingDelete(false);
      invalidate();
    },
    onError: (err) => {
      setConfirmingDelete(false);
      setError(errorText(err, "Delete failed. Try again."));
    },
  });

  return (
    <li className="flex min-w-0 flex-col gap-3 rounded-2xl border border-line bg-surface p-5 shadow-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
              trigger.enabled ? "bg-accent-soft text-accent-strong" : "bg-raised text-faint"
            }`}
          >
            <Zap size={16} aria-hidden />
          </span>
          <h3 className="truncate font-display text-base font-semibold tracking-tight">{trigger.name}</h3>
        </div>
        <StatusPill status={onOffText(trigger)} className="shrink-0" />
      </div>
      <p className="text-sm text-ink/90">
        When {triggerWhen(trigger.event_type, connectionName)}
        {" → "}
        {agentName ? (
          <>
            assign to{" "}
            <Link
              href={trigger.target_agent_id ? `/agents/${trigger.target_agent_id}` : "/agents"}
              className="inline-flex max-w-full items-center gap-1 font-medium text-accent-strong hover:underline"
            >
              <Avatar name={agentName} size="xs" /> <span className="truncate">{agentName}</span>
            </Link>
          </>
        ) : trigger.target_team_id ? (
          "hand to the team"
        ) : (
          "assign to an agent"
        )}
      </p>
      {/* Behaviors configured in the builder that would otherwise be invisible
          until someone opens Edit. */}
      {trigger.workflow_definition?.template === "engineering_ticket" ||
      trigger.action_config_json.comment_back ? (
        <div className="flex flex-wrap gap-1.5">
          {trigger.workflow_definition?.template === "engineering_ticket" ? (
            <Chip>QA review loop</Chip>
          ) : null}
          {trigger.action_config_json.comment_back ? <Chip>Comments back on the issue</Chip> : null}
        </div>
      ) : null}
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
      <ErrorNote message={error} />
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1">
        <StatusPill status={run} />
        <button
          type="button"
          aria-expanded={showRuns}
          onClick={() => setShowRuns((value) => !value)}
          className={`inline-flex items-center gap-1 rounded-md text-xs font-medium text-accent-strong hover:underline ${focusRing}`}
        >
          <History size={12} aria-hidden /> {showRuns ? "Hide history" : "History"}
        </button>
      </div>
      {showRuns ? <RecentRuns triggerId={trigger.id} /> : null}
      {isAdmin ? (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-line pt-3">
          <Button
            size="sm"
            onClick={() => toggle.mutate()}
            disabled={toggle.isPending}
            aria-label={trigger.enabled ? `Turn off ${trigger.name}` : `Turn on ${trigger.name}`}
          >
            <Power size={13} /> {trigger.enabled ? "Turn off" : "Turn on"}
          </Button>
          <Button size="sm" onClick={onEdit} aria-label={`Edit ${trigger.name}`}>
            <Pencil size={13} /> Edit
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto text-danger"
            aria-label={`Delete ${trigger.name}`}
            onClick={() => setConfirmingDelete(true)}
          >
            <Trash2 size={13} />
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        open={confirmingDelete}
        title="Delete this automation?"
        body={
          <>
            “{trigger.name}” will stop watching and won’t start any more work. Tasks it already
            started are not affected.
          </>
        }
        confirmLabel="Delete automation"
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
        onClose={() => setConfirmingDelete(false)}
      />
    </li>
  );
}

/** Lazy: mounts (and fetches) only once someone opens the history. */
function RecentRuns({ triggerId }: { triggerId: string }) {
  const { workspace } = useWorkspace();
  const invocations = useTriggerInvocations(workspace.workspace_id, triggerId);
  return (
    <div className="rounded-xl border border-line bg-raised/60 px-3 py-2.5">
      {invocations.isPending ? <Spinner label="Loading history…" /> : null}
      {invocations.isError ? (
        <LoadError what="the run history" onRetry={() => void invocations.refetch()} />
      ) : null}
      {invocations.data && invocations.data.length === 0 ? (
        <p className="text-[13px] text-faint">This automation hasn’t run yet.</p>
      ) : null}
      <ul className="space-y-1.5">
        {invocations.data?.map((invocation: TriggerInvocation) => (
          <li key={invocation.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
            <Badge tone={RUN_TONE[invocation.status] ?? "neutral"}>
              {RUN_LABEL[invocation.status] ?? invocation.status}
            </Badge>
            <span className="text-xs text-faint">{formatDateTime(invocation.created_at)}</span>
            {invocation.task_id ? (
              <Link
                href={`/tasks/${invocation.task_id}`}
                className={`rounded-md text-xs font-medium text-accent-strong hover:underline ${focusRing}`}
              >
                task {shortId(invocation.task_id)}
              </Link>
            ) : null}
            {invocation.error_message ? (
              <span className="w-full truncate text-xs text-danger" title={invocation.error_message}>
                {invocation.error_message}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
