"use client";

/** "Details" panel: agent card, current work, cost, work episodes, activity. */

import { ExternalLink, Pause, Play, Square } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui";
import { relativeTime, statusLabelFor } from "@/lib/chat";
import { formatCostMicros, formatTokens } from "@/lib/format";
import type { ActivityCard, ConversationDetail, Task } from "@/lib/types";

const TASK_STATE_LABELS: Record<string, string> = {
  queued: "Waiting for a free slot",
  running: "Working",
  paused: "Paused",
  completed: "Finished",
  failed: "Ran into a problem",
  cancelled: "Stopped",
};

const TASK_STATE_TONES: Record<string, string> = {
  queued: "text-dim",
  running: "text-accent-strong",
  paused: "text-warn",
  completed: "text-ok",
  failed: "text-danger",
  cancelled: "text-dim",
};

function taskStateLabel(task: Pick<Task, "state">): string {
  return TASK_STATE_LABELS[task.state] ?? task.state;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">{title}</h3>
      {children}
    </section>
  );
}

export function ContextPanel({
  detail,
  activity,
  canAct,
  acting = false,
  onPause,
  onResume,
  onCancel,
}: {
  detail: ConversationDetail;
  activity: ActivityCard[];
  canAct: boolean;
  acting?: boolean;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}) {
  const { conversation, agent, tasks } = detail;
  const live = statusLabelFor(conversation);
  const activeTask = tasks.find((task) => task.id === conversation.active_task_id) ?? null;

  return (
    <div className="space-y-6 text-sm">
      <Section title="Agent">
        {agent ? (
          <div className="rounded-2xl border border-line bg-surface p-4">
            <p className="font-medium text-ink">{agent.name}</p>
            {agent.role_title ? <p className="text-xs text-dim">{agent.role_title}</p> : null}
            {agent.public_purpose ? (
              <p className="mt-2 text-sm text-dim">{agent.public_purpose}</p>
            ) : null}
            <p className="mt-2 text-xs text-dim">
              {agent.status === "active"
                ? agent.availability === "available"
                  ? "Available"
                  : "Busy right now"
                : agent.status === "paused"
                  ? "Paused by an admin"
                  : "Turned off"}
            </p>
            <Link
              href={`/agents/${agent.id}`}
              className="mt-3 inline-flex min-h-[40px] items-center gap-1 text-accent-strong hover:underline"
            >
              View profile <ExternalLink size={13} />
            </Link>
          </div>
        ) : (
          <p className="text-dim">This agent is no longer in the workspace.</p>
        )}
      </Section>

      <Section title="Current work">
        {live && activeTask ? (
          <div className="space-y-3 rounded-2xl border border-line bg-surface p-4">
            <p className="font-medium text-ink">{live.label}</p>
            <p className="text-xs text-dim">{activeTask.title}</p>
            {canAct ? (
              <div className="flex flex-wrap gap-2">
                {activeTask.state === "paused" ? (
                  <Button size="sm" disabled={acting} onClick={onResume} aria-label="Resume work">
                    <Play size={13} /> Resume
                  </Button>
                ) : (
                  <Button size="sm" disabled={acting} onClick={onPause} aria-label="Pause work">
                    <Pause size={13} /> Pause
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="danger"
                  disabled={acting}
                  onClick={onCancel}
                  aria-label="Stop work"
                >
                  <Square size={13} /> Stop
                </Button>
              </div>
            ) : null}
          </div>
        ) : (
          <p className="text-dim">Nothing in progress. Send a message to start something new.</p>
        )}
      </Section>

      <Section title="Usage">
        <dl className="grid grid-cols-3 gap-2 rounded-2xl border border-line bg-surface p-4">
          <div>
            <dt className="text-[11px] text-faint">Cost</dt>
            <dd className="font-medium tabular-nums text-ink">
              {formatCostMicros(detail.total_cost_micros)}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] text-faint">Tokens in</dt>
            <dd className="font-medium tabular-nums text-ink">
              {formatTokens(detail.total_input_tokens)}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] text-faint">Tokens out</dt>
            <dd className="font-medium tabular-nums text-ink">
              {formatTokens(detail.total_output_tokens)}
            </dd>
          </div>
        </dl>
      </Section>

      <Section title="Work episodes">
        {tasks.length === 0 ? (
          <p className="text-dim">No work yet.</p>
        ) : (
          <ul className="divide-y divide-line rounded-2xl border border-line bg-surface">
            {tasks.map((task) => (
              <li key={task.id} className="flex items-center gap-3 px-4 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-ink">{task.title}</p>
                  <p className="text-xs">
                    <span className={TASK_STATE_TONES[task.state] ?? "text-dim"}>
                      {taskStateLabel(task)}
                    </span>
                    <span className="text-faint"> · {relativeTime(task.updated_at)}</span>
                  </p>
                </div>
                <Link
                  href={`/tasks/${task.id}`}
                  className="inline-flex min-h-[40px] shrink-0 items-center gap-1 text-xs text-accent-strong hover:underline"
                >
                  Open in Advanced <ExternalLink size={12} />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Activity">
        {activity.length === 0 ? (
          <p className="text-dim">No activity yet.</p>
        ) : (
          <ol className="space-y-2">
            {activity.map((card) => (
              <li key={card.id} className="rounded-xl border border-line bg-surface px-3 py-2">
                <p className="flex items-baseline justify-between gap-2">
                  <span className="font-medium text-ink">
                    {card.actor_agent_name ? `${card.actor_agent_name} · ` : ""}
                    {card.label}
                    {card.target_agent_name ? ` (${card.target_agent_name})` : ""}
                  </span>
                  <time
                    dateTime={card.created_at}
                    className="shrink-0 text-[11px] tabular-nums text-faint"
                  >
                    {relativeTime(card.created_at)}
                  </time>
                </p>
                {card.summary ? <p className="mt-0.5 text-xs text-dim">{card.summary}</p> : null}
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}
