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
  Hand,
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
import { PageHeader } from "@/components/app-shell";
import { isActiveState, StateBadge } from "@/components/task-bits";
import { Badge, Button, ErrorNote, Input, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { riskTone } from "@/lib/policy";
import { formatCostMicros, formatDateTime, formatTokens, shortId } from "@/lib/format";
import {
  useAgents,
  useInvalidateTasks,
  useTask,
  useTaskMessages,
  useTaskTimeline,
} from "@/lib/hooks";
import type { RunEvent, TaskMessage } from "@/lib/types";
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
        <div className="px-8 py-6">
          <Spinner label="Loading task…" />
        </div>
      </>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <>
        <PageHeader title="Task" />
        <div className="px-8 py-6">
          <ErrorNote message="Could not load this task." />
        </div>
      </>
    );
  }

  const { task, runs, total_input_tokens, total_output_tokens, total_cost_micros } = detail.data;
  const agent = agents.data?.find((a) => a.id === task.assigned_agent_id);
  const canOperate = can("member");
  const active = isActiveState(task.state);
  const waitingApproval = runs.some((run) => run.status === "waiting_approval");

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
      <div className="grid gap-6 px-8 py-6 xl:grid-cols-[1fr_380px]">
        <div className="min-w-0 space-y-6">
          <ErrorNote message={actionError} />

          {task.trigger_id ? (
            <section
              data-testid="trigger-origin-banner"
              className="flex items-center gap-3 rounded-xl border border-accent/30 bg-accent-soft px-5 py-3.5"
            >
              <Zap size={16} className="shrink-0 text-accent" />
              <p className="min-w-0 flex-1 text-sm text-dim">
                Started by trigger{" "}
                <Link href="/triggers" className="font-medium text-accent hover:underline">
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
                        className="font-medium text-accent hover:underline"
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

          {waitingApproval ? (
            <section
              data-testid="waiting-approval-banner"
              className="flex items-center gap-3 rounded-xl border border-warn/40 bg-warn/10 px-5 py-3.5"
            >
              <ShieldQuestion size={18} className="shrink-0 text-warn" />
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

          <section className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-line bg-surface px-5 py-3.5 text-sm">
            <StateBadge state={task.state} />
            <span className="text-dim">
              <CircleDollarSign size={13} className="mr-1 inline align-[-2px] text-accent" />
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
            <section className="rounded-xl border border-line bg-surface px-5 py-4">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-dim">
                Instruction
              </h2>
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
          <section>
            <h2 className="mb-3 text-sm font-semibold text-dim">Timeline</h2>
            <Timeline events={timeline.data ?? []} live={active} />
          </section>

          {runs.length > 0 ? (
            <section>
              <h2 className="mb-3 text-sm font-semibold text-dim">Runs</h2>
              <div className="space-y-2">
                {runs.map((run) => (
                  <Link
                    key={run.id}
                    href="/runs"
                    className="block rounded-lg border border-line bg-surface px-4 py-3 text-sm transition-colors hover:border-accent/40"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <code className="text-xs text-dim">{shortId(run.id)}</code>
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
      </div>
    </>
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
      className="mt-1.5 space-y-1 rounded-md border border-line bg-raised px-2.5 py-2"
    >
      <div className="flex items-center gap-2 text-xs">
        <code className="min-w-0 flex-1 truncate text-[11px]">{command || "(command)"}</code>
        <Badge tone={jobStatus === "completed" && exitCode === 0 ? "ok" : "danger"}>
          {jobStatus === "completed" && exitCode !== null ? `exit ${exitCode}` : jobStatus}
        </Badge>
      </div>
      <p className="text-[11px] text-faint">
        sandbox container{durationMs !== null ? ` · ${durationMs}ms` : ""}
      </p>
      {stdout || stderr ? (
        <details className="group">
          <summary className="cursor-pointer select-none text-[11px] text-dim hover:text-ink">
            Show output
          </summary>
          {stdout ? (
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-surface px-2 py-1.5 text-[11px] leading-relaxed text-ink">
              {stdout}
            </pre>
          ) : null}
          {stderr ? (
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-danger/5 px-2 py-1.5 text-[11px] leading-relaxed text-danger">
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
      <p className="rounded-lg border border-dashed border-line-strong px-4 py-6 text-center text-xs text-dim">
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
              className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-surface ${dot}`}
            />
            <p className="flex items-center gap-1.5 text-[13px] font-medium">
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
          <span className="absolute -left-[21px] top-1 h-2.5 w-2.5 animate-pulse rounded-full border-2 border-surface bg-accent/60" />
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
    <section className="rounded-xl border border-line bg-surface">
      <h2 className="border-b border-line px-5 py-3 text-xs font-semibold uppercase tracking-wider text-dim">
        Conversation
      </h2>
      <div className="max-h-[32rem] space-y-4 overflow-y-auto px-5 py-4">
        {messages.length === 0 ? (
          <p className="py-4 text-center text-xs text-dim">No messages yet.</p>
        ) : (
          messages.map((message) => {
            const fromAgent = message.sender_type === "agent";
            const fromSystem = message.sender_type === "system";
            const body =
              typeof message.content_json.text === "string" ? message.content_json.text : "";
            return (
              <div key={message.id} className="flex gap-3">
                <span
                  className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
                    fromAgent
                      ? "bg-accent-soft text-accent-strong"
                      : fromSystem
                        ? "bg-danger/10 text-danger"
                        : "bg-hover text-dim"
                  }`}
                >
                  {fromAgent ? <Bot size={14} /> : <User size={14} />}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-dim">
                    <span className="font-medium text-ink">
                      {fromAgent ? agentName : fromSystem ? "System" : "You"}
                    </span>{" "}
                    · {formatDateTime(message.created_at)}
                    {message.message_type === "instruction" ? " · instruction" : ""}
                  </p>
                  <p
                    className={`mt-1 whitespace-pre-wrap text-sm ${
                      fromSystem ? "text-danger" : "text-ink"
                    }`}
                  >
                    {body}
                  </p>
                </div>
              </div>
            );
          })
        )}
      </div>
      {canInstruct ? (
        <form
          className="flex gap-2 border-t border-line px-5 py-3"
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
        <p className="px-5 pb-3 text-xs text-danger">{send.error.detail}</p>
      ) : null}
    </section>
  );
}
