"use client";

/** Tasks page: workspace task list with live polling + create/assign dialog. */

import { useMutation } from "@tanstack/react-query";
import { GitFork, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { isActiveState, StateBadge } from "@/components/task-bits";
import {
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useAgents, useInvalidateTasks, useTasks } from "@/lib/hooks";
import type { Task } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const STATE_FILTERS = ["", "queued", "running", "paused", "completed", "failed", "cancelled"];

export default function TasksPage() {
  return (
    <Suspense
      fallback={
        <div className="px-8 py-6">
          <Spinner />
        </div>
      }
    >
      <TasksPageInner />
    </Suspense>
  );
}

function TasksPageInner() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agentFilter = useSearchParams().get("agent") ?? undefined;
  const [stateFilter, setStateFilter] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const tasks = useTasks(workspaceId, {
    state: stateFilter || undefined,
    agent_id: agentFilter,
    limit: 100,
  });
  const agents = useAgents(workspaceId);
  const agentName = (id: string | null) =>
    id ? (agents.data?.find((a) => a.id === id)?.name ?? "—") : "unassigned";

  return (
    <>
      <PageHeader
        title="Tasks"
        description="Work assigned to agents, executed durably through Temporal"
        actions={
          can("member") ? (
            <Button variant="primary" onClick={() => setDialogOpen(true)}>
              <Plus size={14} /> New task
            </Button>
          ) : null
        }
      />
      <div className="space-y-4 px-8 py-6">
        <div className="flex items-center gap-2">
          <Select
            className="w-44"
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            aria-label="Filter by state"
          >
            {STATE_FILTERS.map((state) => (
              <option key={state} value={state}>
                {state || "All states"}
              </option>
            ))}
          </Select>
          {tasks.data ? (
            <span className="text-xs text-dim">{tasks.data.total} total</span>
          ) : null}
        </div>

        {tasks.isPending ? (
          <Spinner label="Loading tasks…" />
        ) : (tasks.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title="No tasks yet"
            description="Create a task and assign it to an agent, or message an agent from the organization view — every conversation becomes a task."
            action={
              can("member") ? (
                <Button variant="primary" onClick={() => setDialogOpen(true)}>
                  <Plus size={14} /> Create first task
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="overflow-hidden rounded-xl border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-raised text-left text-xs uppercase tracking-wider text-dim">
                  <th className="px-4 py-2.5 font-medium">Task</th>
                  <th className="px-4 py-2.5 font-medium">State</th>
                  <th className="px-4 py-2.5 font-medium">Agent</th>
                  <th className="px-4 py-2.5 font-medium">Priority</th>
                  <th className="px-4 py-2.5 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {(tasks.data?.items ?? []).map((task: Task) => (
                  <tr key={task.id} className="border-b border-line last:border-0 hover:bg-hover/50">
                    <td className="max-w-md px-4 py-2.5">
                      <Link
                        href={`/tasks/${task.id}`}
                        className="flex items-center gap-1.5 truncate font-medium hover:text-accent-strong"
                      >
                        {task.parent_task_id ? (
                          <GitFork
                            size={12}
                            className="shrink-0 text-accent"
                            aria-label="Delegated subtask"
                          />
                        ) : null}
                        <span className="truncate">{task.title}</span>
                      </Link>
                    </td>
                    <td className="px-4 py-2.5">
                      <StateBadge state={task.state} />
                      {isActiveState(task.state) ? (
                        <span className="ml-2 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent align-middle" />
                      ) : null}
                      {task.state === "queued" &&
                      typeof task.metadata_json.queue === "object" &&
                      task.metadata_json.queue !== null ? (
                        <span className="ml-2 text-[11px] text-faint">
                          {String(
                            (task.metadata_json.queue as Record<string, unknown>).reason ?? "",
                          ).replace("_", " ")}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5 text-dim">{agentName(task.assigned_agent_id)}</td>
                    <td className="px-4 py-2.5 text-dim">{task.priority}</td>
                    <td className="px-4 py-2.5 text-dim">{formatDateTime(task.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {dialogOpen ? (
        <CreateTaskDialog workspaceId={workspaceId} onClose={() => setDialogOpen(false)} />
      ) : null}
    </>
  );
}

function CreateTaskDialog({
  workspaceId,
  onClose,
}: {
  workspaceId: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const agents = useAgents(workspaceId);
  const invalidate = useInvalidateTasks(workspaceId);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [agentId, setAgentId] = useState("");
  const [priority, setPriority] = useState("normal");

  const create = useMutation({
    mutationFn: () =>
      api<Task>(`/api/v1/workspaces/${workspaceId}/tasks`, {
        method: "POST",
        body: {
          title: title.trim(),
          description,
          priority,
          agent_id: agentId || null,
        },
      }),
    onSuccess: (task) => {
      invalidate();
      onClose();
      router.push(`/tasks/${task.id}`);
    },
  });

  const activeAgents = (agents.data ?? []).filter((agent) => agent.status === "active");

  return (
    <Dialog title="New task" open onClose={onClose} wide>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Title">
          <Input
            required
            maxLength={500}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Write a launch blog post"
          />
        </Field>
        <Field label="Description" hint="Becomes the agent's instruction.">
          <Textarea
            rows={5}
            maxLength={20000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What needs to be done, context, acceptance criteria…"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Assign to agent"
            hint={agentId ? "The run starts immediately." : "Unassigned tasks stay queued."}
          >
            <Select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              <option value="">Leave unassigned</option>
              {activeAgents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                  {agent.role_title ? ` — ${agent.role_title}` : ""}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Priority">
            <Select value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="low">low</option>
              <option value="normal">normal</option>
              <option value="high">high</option>
              <option value="urgent">urgent</option>
            </Select>
          </Field>
        </div>
        <ErrorNote
          message={
            create.error instanceof ApiError
              ? create.error.detail
              : create.error
                ? "Creating the task failed."
                : null
          }
        />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Creating…" : agentId ? "Create & start" : "Create task"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
