"use client";

/** Task detail (plan 17.7 minimal): timeline + conversation + cost, with
 * pause/resume/cancel and mid-run instructions. Polls while the task is
 * active; SSE replaces polling in a later phase (plan 18). */

import { useMutation } from "@tanstack/react-query";
import {
  Ban,
  Bot,
  CircleDollarSign,
  Eye,
  GitFork,
  Hand,
  Hourglass,
  Pause,
  Play,
  Send,
  ShieldQuestion,
  TerminalSquare,
  User,
  Wrench,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import {
  AGENT_MESSAGE_TYPES,
  isActiveState,
  MessageTypeBadge,
  StateBadge,
  StructuredMessageBody,
} from "@/components/task-bits";
import { Badge, Button, ErrorNote, Input, Spinner, focusRing } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { riskTone } from "@/lib/policy";
import { formatCostMicros, formatDateTime, formatTokens, shortId } from "@/lib/format";
import {
  useAgents,
  useInvalidateTasks,
  useTask,
  useTaskMessages,
  useTaskTimeline,
  useTaskTree,
} from "@/lib/hooks";
import type { RunEvent, TaskMessage, TaskTreeNode } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export default function TaskDetailPage() {
  const params = useParams<{ id: string }>();
  const taskId = params.id;
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const invalidate = useInvalidateTasks(workspaceId);
  const [actionError, setActionError] = useState<string | null>(null);

  const detail = useTask(workspaceId, taskId, true);
  const live = detail.data ? isActiveState(detail.data.task.state) : true;
  const timeline = useTaskTimeline(workspaceId, taskId, live);
  const messages = useTaskMessages(workspaceId, taskId, live);
  const tree = useTaskTree(workspaceId, taskId, live);
  const agents = useAgents(workspaceId);

  const act = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel") =>
      api(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/${action}`, { method: "POST" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) =>
      setActionError(error instanceof ApiError ? error.detail : "Action failed."),
  });

  if (detail.isPending) {
    return (
      <>
        <PageHeader title="Task" />
        <PageBody>
          <Spinner label="Loading task…" />
        </PageBody>
      </>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <>
        <PageHeader title="Task" />
        <PageBody>
          <ErrorNote message="Could not load this task." />
        </PageBody>
      </>
    );
  }

  const { task, runs, total_input_tokens, total_output_tokens, total_cost_micros } = detail.data;
  const agent = agents.data?.find((a) => a.id === task.assigned_agent_id);
  const canOperate = can("member");
  const active = isActiveState(task.state);
  const waitingApproval = runs.some((run) => run.status === "waiting_approval");
  const waitingDelegation = runs.some((run) => run.status === "waiting_delegation");
  const queueInfo =
    task.state === "queued" &&
    typeof task.metadata_json.queue === "object" &&
    task.metadata_json.queue !== null
      ? (task.metadata_json.queue as Record<string, unknown>)
      : null;
  const treeRoot = tree.data?.root;
  const hasLineage = Boolean(treeRoot && (treeRoot.children.length > 0 || task.parent_task_id));

  return (
    <>
      <PageHeader
        title={task.title}
        description={agent ? `Assigned to ${agent.name}` : "Unassigned"}
        actions={
          canOperate && active ? (
            <>
              {task.state === "paused" ? (
                <Button size="sm" onClick={() => act.mutate("resume")} disabled={act.isPending}>
                  <Play size={13} /> Resume
                </Button>
              ) : (
                <Button size="sm" onClick={() => act.mutate("pause")} disabled={act.isPending}>
                  <Pause size={13} /> Pause
                </Button>
              )}
              <Button
                size="sm"
                variant="danger"
                disabled={act.isPending}
                onClick={() => {
                  if (window.confirm("Cancel this task? The run stops after the current step.")) {
                    act.mutate("cancel");
                  }
                }}
              >
                <Ban size={13} /> Cancel
              </Button>
            </>
          ) : null
        }
      />
      <PageBody className="grid gap-6 xl:grid-cols-[1fr_380px]">
        <div className="min-w-0 space-y-6">
          <ErrorNote message={actionError} />

          {task.trigger_id ? (
            <section
              data-testid="trigger-origin-banner"
              className="flex items-center gap-3 rounded-2xl border border-accent/30 bg-accent-soft px-5 py-3.5"
            >
              <Zap size={16} className="shrink-0 text-accent-strong" aria-hidden />
              <p className="min-w-0 flex-1 text-sm text-dim">
                Started by trigger{" "}
                <Link
                  href="/triggers"
                  className={`rounded-md font-medium text-accent-strong hover:underline ${focusRing}`}
                >
                  {String(task.metadata_json.trigger_name ?? shortId(task.trigger_id))}
                </Link>
                {task.external_source ? (
                  <>
                    {" "}
                    from {task.external_source}{" "}
                    {typeof task.metadata_json.external_url === "string" ? (
                      <a
                        href={task.metadata_json.external_url}
                        target="_blank"
                        rel="noreferrer"
                        className={`rounded-md font-medium text-accent-strong hover:underline ${focusRing}`}
                      >
                        {task.external_id}
                      </a>
                    ) : (
                      <span className="font-medium text-ink">{task.external_id}</span>
                    )}
                  </>
                ) : null}
              </p>
            </section>
          ) : null}

          {queueInfo ? (
            <section
              data-testid="queued-banner"
              className="flex items-center gap-3 rounded-2xl border border-line-strong bg-raised px-5 py-3.5"
            >
              <Hourglass size={16} className="shrink-0 text-dim" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink">Queued — waiting for a free slot</p>
                <p className="text-xs text-dim">
                  {queueInfo.reason === "agent_concurrency"
                    ? "The assigned agent is at its max concurrent runs. The task starts automatically when a run finishes."
                    : queueInfo.reason === "workspace_concurrency"
                      ? "The workspace is at its max concurrent runs. The task starts automatically when a slot frees."
                      : "The task starts automatically when a concurrency slot frees."}
                </p>
              </div>
            </section>
          ) : null}

          {waitingDelegation ? (
            <section
              data-testid="waiting-delegation-banner"
              className="flex items-center gap-3 rounded-2xl border border-accent/30 bg-accent-soft px-5 py-3.5"
            >
              <GitFork size={16} className="shrink-0 text-accent-strong" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink">Waiting on a delegated subtask</p>
                <p className="text-xs text-dim">
                  This run is parked durably until the child task below returns its result
                  summary.
                </p>
              </div>
            </section>
          ) : null}

          {waitingApproval ? (
            <section
              data-testid="waiting-approval-banner"
              className="flex items-center gap-3 rounded-2xl border border-warn/30 bg-warn-soft px-5 py-3.5"
            >
              <ShieldQuestion size={18} className="shrink-0 text-warn" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-warn">Waiting for approval</p>
                <p className="text-xs text-dim">
                  The agent requested a gated tool call and the run is parked durably until
                  someone decides.
                </p>
              </div>
              <Link href="/approvals">
                <Button size="sm" variant="primary">
                  Review approval
                </Button>
              </Link>
            </section>
          ) : null}

          <section className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-2xl border border-line bg-surface px-5 py-3.5 text-sm shadow-card">
            <StateBadge state={task.state} />
            <span className="text-dim">
              <CircleDollarSign
                size={13}
                className="mr-1 inline align-[-2px] text-accent-strong"
                aria-hidden
              />
              {formatCostMicros(total_cost_micros)}
            </span>
            <span className="text-dim">
              {formatTokens(total_input_tokens)} in · {formatTokens(total_output_tokens)} out
            </span>
            <span className="text-dim">
              {runs.length} run{runs.length === 1 ? "" : "s"}
            </span>
            <span className="ml-auto text-xs text-faint">
              created {formatDateTime(task.created_at)}
            </span>
          </section>

          {task.description ? (
            <section className="rounded-2xl border border-line bg-surface px-5 py-4 shadow-card">
              <h2 className="mb-2 font-display text-base font-semibold text-ink">Instruction</h2>
              <p className="whitespace-pre-wrap text-sm text-ink">{task.description}</p>
            </section>
          ) : null}

          <Conversation
            messages={messages.data ?? []}
            agentName={agent?.name ?? "Agent"}
            taskId={taskId}
            workspaceId={workspaceId}
            canInstruct={canOperate && active}
            onChanged={invalidate}
          />
        </div>

        <aside className="space-y-6">
          {hasLineage && treeRoot ? (
            <section data-testid="delegation-chain">
              <h2 className="mb-3 flex items-center gap-1.5 font-display text-base font-semibold text-ink">
                <GitFork size={14} className="text-accent-strong" aria-hidden /> Delegation chain
              </h2>
              <TreeNode node={treeRoot} focusId={taskId} depth={0} />
            </section>
          ) : null}

          <section>
            <h2 className="mb-3 font-display text-base font-semibold text-ink">Timeline</h2>
            <Timeline events={timeline.data ?? []} live={active} />
          </section>

          {runs.length > 0 ? (
            <section>
              <h2 className="mb-3 font-display text-base font-semibold text-ink">Runs</h2>
              <div className="space-y-2">
                {runs.map((run) => (
                  <Link
                    key={run.id}
                    href="/runs"
                    className={`block rounded-xl border border-line bg-surface px-4 py-3 text-sm shadow-card transition-colors hover:border-accent ${focusRing}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <code className="font-mono text-xs text-dim">{shortId(run.id)}</code>
                      <StateBadge state={run.status} />
                    </div>
                    <p className="mt-1.5 text-xs text-dim">
                      {formatTokens(run.input_tokens)} in · {formatTokens(run.output_tokens)} out
                      {" · "}
                      {formatCostMicros(run.estimated_cost_micros)} · {run.steps_used} step
                      {run.steps_used === 1 ? "" : "s"}
                    </p>
                    {run.error_message ? (
                      <p className="mt-1 truncate text-xs text-danger">{run.error_message}</p>
                    ) : null}
                  </Link>
                ))}
              </div>
            </section>
          ) : null}
        </aside>
      </PageBody>
    </>
  );
}

/** One node of the delegation chain (plan 45 "Task parent/child display"). */
function TreeNode({
  node,
  focusId,
  depth,
}: {
  node: TaskTreeNode;
  focusId: string;
  depth: number;
}) {
  const isFocus = node.task.id === focusId;
  return (
    <div className={depth > 0 ? "mt-1.5 border-l border-line pl-3" : ""}>
      <Link
        href={`/tasks/${node.task.id}`}
        className={`block rounded-xl border px-3 py-2 text-sm transition-colors ${focusRing} ${
          isFocus
            ? "border-accent/50 bg-accent-soft"
            : "border-line bg-surface shadow-card hover:border-accent"
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate font-medium">{node.task.title}</span>
          <StateBadge state={node.task.state} />
        </div>
        <p className="mt-0.5 text-xs text-dim">
          {node.agent_name ?? "unassigned"}
          {node.latest_run_status ? ` · run ${node.latest_run_status}` : ""}
          {isFocus ? " · this task" : ""}
        </p>
      </Link>
      {node.children.map((child) => (
        <TreeNode key={child.task.id} node={child} focusId={focusId} depth={depth + 1} />
      ))}
    </div>
  );
}

/** Dot color + optional icon per event family (tool/approval events, plan 17.7). */
function eventStyle(event: RunEvent): { dot: string; icon: React.ReactNode | null } {
  const type = event.event_type;
  const payload = event.payload_json;
  if (type === "run.failed") return { dot: "bg-danger", icon: null };
  if (type === "run.completed") return { dot: "bg-ok", icon: null };
  if (type === "tool.call") {
    const status = payload.status;
    return {
      dot: status === "executed" ? "bg-ok" : status === "needs_approval" ? "bg-warn" : "bg-danger",
      icon: <Wrench size={12} className="text-dim" />,
    };
  }
  if (type === "sandbox.job") {
    const exit = payload.exit_code;
    return {
      dot: payload.job_status === "completed" && exit === 0 ? "bg-ok" : "bg-danger",
      icon: <TerminalSquare size={12} className="text-dim" />,
    };
  }
  if (type === "node.execute_tool") return { dot: "bg-accent", icon: <Wrench size={12} className="text-dim" /> };
  if (type === "node.observe") return { dot: "bg-accent", icon: <Eye size={12} className="text-dim" /> };
  if (type === "node.request_approval" || type === "approval.requested") {
    return { dot: "bg-warn", icon: <ShieldQuestion size={12} className="text-warn" /> };
  }
  if (type === "approval.approved") return { dot: "bg-ok", icon: <Hand size={12} className="text-ok" /> };
  if (type === "approval.rejected") return { dot: "bg-danger", icon: <Hand size={12} className="text-danger" /> };
  return { dot: "bg-accent", icon: null };
}

/** One sandbox job in the timeline (plan 14): command, exit code, duration,
 * and collapsible sanitized output. */
function SandboxJobEvent({ payload }: { payload: Record<string, unknown> }) {
  const command = typeof payload.command === "string" ? payload.command : "";
  const exitCode = typeof payload.exit_code === "number" ? payload.exit_code : null;
  const durationMs = typeof payload.job_duration_ms === "number" ? payload.job_duration_ms : null;
  const jobStatus = typeof payload.job_status === "string" ? payload.job_status : "";
  const stdout = typeof payload.stdout === "string" ? payload.stdout : "";
  const stderr = typeof payload.stderr === "string" ? payload.stderr : "";
  return (
    <div
      data-testid="sandbox-job-event"
      className="mt-1.5 space-y-1 rounded-xl border border-line bg-raised px-3 py-2"
    >
      <div className="flex items-center gap-2 text-xs">
        <code className="min-w-0 flex-1 truncate font-mono text-xs text-ink">
          {command || "(command)"}
        </code>
        <Badge tone={jobStatus === "completed" && exitCode === 0 ? "ok" : "danger"}>
          {jobStatus === "completed" && exitCode !== null ? `exit ${exitCode}` : jobStatus}
        </Badge>
      </div>
      <p className="text-xs text-faint">
        sandbox container{durationMs !== null ? ` · ${durationMs}ms` : ""}
      </p>
      {stdout || stderr ? (
        <details className="group">
          <summary
            className={`cursor-pointer select-none rounded-md text-xs text-dim hover:text-ink ${focusRing}`}
          >
            Show output
          </summary>
          {stdout ? (
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-surface px-2.5 py-1.5 font-mono text-xs leading-relaxed text-ink">
              {stdout}
            </pre>
          ) : null}
          {stderr ? (
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-danger-soft px-2.5 py-1.5 font-mono text-xs leading-relaxed text-danger">
              {stderr}
            </pre>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}

function Timeline({ events, live }: { events: RunEvent[]; live: boolean }) {
  if (events.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-line-strong bg-surface/60 px-4 py-6 text-center text-sm text-dim">
        {live ? "Waiting for the first run event…" : "No run events recorded."}
      </p>
    );
  }
  return (
    <ol className="relative space-y-0 border-l border-line-strong pl-4">
      {events.map((event) => {
        const payload = event.payload_json;
        const { dot, icon } = eventStyle(event);
        const toolName = typeof payload.tool_name === "string" ? payload.tool_name : null;
        const risk = typeof payload.risk === "string" ? payload.risk : null;
        const status = typeof payload.status === "string" ? payload.status : null;
        return (
          <li key={event.id} className="relative pb-4 last:pb-0">
            <span
              aria-hidden
              className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-surface ${dot}`}
            />
            <p className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
              {icon}
              {event.event_type}
              {event.event_type === "tool.call" && risk ? (
                <Badge tone={riskTone(risk)}>{risk}</Badge>
              ) : null}
            </p>
            {toolName ? (
              <p className="text-xs text-dim">
                <code>{toolName}</code>
                {status ? ` · ${status}` : ""}
                {typeof payload.reason === "string" && payload.reason
                  ? ` · ${payload.reason}`
                  : ""}
              </p>
            ) : null}
            <p className="text-xs text-dim">
              {typeof payload.detail === "string" ? `${payload.detail} · ` : ""}
              {formatDateTime(event.created_at)}
            </p>
            {typeof payload.input_tokens === "number" ? (
              <p className="text-xs text-faint">
                {formatTokens(payload.input_tokens as number)} in ·{" "}
                {formatTokens((payload.output_tokens as number) ?? 0)} out
                {typeof payload.cost_micros === "number"
                  ? ` · ${formatCostMicros(payload.cost_micros as number)}`
                  : ""}
                {typeof payload.latency_ms === "number" ? ` · ${payload.latency_ms}ms` : ""}
              </p>
            ) : null}
            {typeof payload.error_message === "string" && payload.error_message ? (
              <p className="mt-0.5 text-xs text-danger">{payload.error_message}</p>
            ) : null}
            {event.event_type === "sandbox.job" ? <SandboxJobEvent payload={payload} /> : null}
          </li>
        );
      })}
      {live ? (
        <li className="relative">
          <span
            aria-hidden
            className="absolute -left-[21px] top-1 h-2.5 w-2.5 animate-pulse rounded-full border-2 border-surface bg-accent"
          />
          <p className="text-xs text-faint">live — updating…</p>
        </li>
      ) : null}
    </ol>
  );
}

function Conversation({
  messages,
  agentName,
  taskId,
  workspaceId,
  canInstruct,
  onChanged,
}: {
  messages: TaskMessage[];
  agentName: string;
  taskId: string;
  workspaceId: string;
  canInstruct: boolean;
  onChanged: () => void;
}) {
  const [text, setText] = useState("");

  const send = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/instruction`, {
        method: "POST",
        body: { text: text.trim() },
      }),
    onSuccess: () => {
      setText("");
      onChanged();
    },
  });

  return (
    <section className="rounded-2xl border border-line bg-surface shadow-card">
      <h2 className="border-b border-line px-5 py-4 font-display text-base font-semibold text-ink">
        Conversation
      </h2>
      <div className="max-h-[32rem] space-y-4 overflow-y-auto px-5 py-4">
        {messages.length === 0 ? (
          <p className="py-4 text-center text-sm text-dim">No messages yet.</p>
        ) : (
          messages.map((message) => {
            const fromAgent = message.sender_type === "agent";
            const fromSystem = message.sender_type === "system";
            // Structured agent-to-agent messages (plan 29) render with a type
            // badge + summary/artifacts; plain messages keep their text body.
            const structured =
              AGENT_MESSAGE_TYPES.has(message.message_type) &&
              message.message_type !== "instruction" &&
              (fromAgent || fromSystem);
            const body =
              typeof message.content_json.text === "string" ? message.content_json.text : "";
            const senderLabel =
              typeof message.content_json.from_agent_name === "string"
                ? message.content_json.from_agent_name
                : fromAgent
                  ? agentName
                  : fromSystem
                    ? "System"
                    : "You";
            return (
              <div key={message.id} className="flex gap-3">
                <span
                  className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
                    fromAgent
                      ? "bg-accent-soft text-accent-strong"
                      : fromSystem
                        ? "bg-danger-soft text-danger"
                        : "bg-hover text-dim"
                  }`}
                  aria-hidden
                >
                  {fromAgent ? <Bot size={14} /> : <User size={14} />}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 text-xs text-dim">
                    <span className="font-medium text-ink">{senderLabel}</span>
                    <span>· {formatDateTime(message.created_at)}</span>
                    {structured ? (
                      <MessageTypeBadge
                        type={message.message_type}
                        content={message.content_json}
                      />
                    ) : message.message_type === "instruction" ? (
                      <span>· instruction</span>
                    ) : null}
                  </p>
                  {structured ? (
                    <StructuredMessageBody content={message.content_json} />
                  ) : (
                    <p
                      className={`mt-1 whitespace-pre-wrap text-sm ${
                        fromSystem ? "text-danger" : "text-ink"
                      }`}
                    >
                      {body}
                    </p>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
      {canInstruct ? (
        <form
          className="flex gap-2 border-t border-line px-5 py-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (text.trim()) send.mutate();
          }}
        >
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Send an instruction to the running agent…"
            maxLength={20000}
          />
          <Button type="submit" variant="primary" disabled={send.isPending || !text.trim()}>
            <Send size={14} />
          </Button>
        </form>
      ) : null}
      {send.error instanceof ApiError ? (
        <p role="alert" className="px-5 pb-4 text-xs text-danger">
          {send.error.detail}
        </p>
      ) : null}
    </section>
  );
}
