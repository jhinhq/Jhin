"use client";

/** Triggers page (plan 17.10): WHEN / IF / THEN automation.
 *
 * List with enabled toggles and last invocations; a builder dialog with a
 * WHEN clause (connection + canonical event type), IF clause (condition
 * rows over the safe filter DSL, with a "State changes to Todo" preset and
 * a team picker sourced from connector metadata), THEN clause (assign to an
 * agent, optional comment-back); and a test panel that dry-runs the filter
 * against a sample event showing per-condition pass/fail.
 */

import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  Plus,
  Trash2,
  XCircle,
  Zap,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Select,
  Spinner,
  StatusLabel,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import {
  useAgents,
  useConnectionMetadata,
  useConnections,
  useConnectors,
  useInvalidateTriggers,
  useTriggerInvocations,
  useTriggers,
} from "@/lib/hooks";
import {
  type ConditionRow,
  filterToRows,
  OP_LABELS,
  rowsToFilter,
  sampleEventFor,
  summarySentence,
  TRIGGER_OPS,
} from "@/lib/triggers";
import type {
  Agent,
  ConditionExplanation,
  LinearTeamMetadata,
  Trigger,
  TriggerInvocation,
  TriggerTestResult,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const invocationTone: Record<string, "ok" | "neutral" | "danger"> = {
  started: "ok",
  duplicate: "neutral",
  failed: "danger",
};

export default function TriggersPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const triggers = useTriggers(workspaceId);
  const agents = useAgents(workspaceId);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [editing, setEditing] = useState<Trigger | null>(null);

  return (
    <>
      <PageHeader
        title="Triggers"
        description="Start agent work automatically when something happens in a connected service."
        actions={
          isAdmin ? (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setEditing(null);
                setBuilderOpen(true);
              }}
            >
              <Plus size={14} /> New trigger
            </Button>
          ) : null
        }
      />
      <PageBody className="space-y-4">
        {triggers.isPending ? <Spinner label="Loading triggers…" /> : null}
        {triggers.isError ? <ErrorNote message="Could not load triggers." /> : null}
        {triggers.data && triggers.data.length === 0 ? (
          <EmptyState
            title="No triggers yet"
            description="Create one to react to connector events — e.g. start a task when a Linear issue moves to Todo."
            action={
              isAdmin ? (
                <Button variant="primary" size="sm" onClick={() => setBuilderOpen(true)}>
                  <Plus size={14} /> New trigger
                </Button>
              ) : undefined
            }
          />
        ) : null}
        {triggers.data?.map((trigger) => (
          <TriggerCard
            key={trigger.id}
            trigger={trigger}
            agents={agents.data ?? []}
            isAdmin={isAdmin}
            onEdit={() => {
              setEditing(trigger);
              setBuilderOpen(true);
            }}
          />
        ))}
      </PageBody>
      {builderOpen ? (
        <TriggerBuilder
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

function TriggerCard({
  trigger,
  agents,
  isAdmin,
  onEdit,
}: {
  trigger: Trigger;
  agents: Agent[];
  isAdmin: boolean;
  onEdit: () => void;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const invalidate = useInvalidateTriggers(workspaceId);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const agent = agents.find((a) => a.id === trigger.target_agent_id);
  const invocations = useTriggerInvocations(workspaceId, expanded ? trigger.id : null);

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
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Toggle failed."),
  });

  const remove = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}/triggers/${trigger.id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Delete failed."),
  });

  const last = trigger.last_invocation;
  return (
    <section className="rounded-2xl border border-line bg-surface shadow-card">
      <div className="flex items-center gap-4 px-5 py-4">
        <span
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
            trigger.enabled ? "bg-accent-soft text-accent-strong" : "bg-raised text-faint"
          }`}
        >
          <Zap size={16} aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate font-display text-sm font-semibold text-ink">{trigger.name}</p>
            {trigger.target_state === "agent_deleted" ? (
              <Badge tone="warn">needs an agent</Badge>
            ) : !trigger.enabled ? (
              <Badge tone="neutral">disabled</Badge>
            ) : null}
          </div>
          <p className="mt-0.5 truncate text-xs text-dim">
            {trigger.event_type ?? "any event"}
            {agent ? ` → ${agent.name}` : ""}
            {trigger.workflow_definition?.template === "engineering_ticket"
              ? " · engineering template"
              : ""}
            {trigger.action_config_json.comment_back ? " · comments back" : ""}
          </p>
        </div>
        {last ? (
          <div className="hidden items-center gap-2 sm:flex">
            <Badge tone={invocationTone[last.status] ?? "neutral"}>{last.status}</Badge>
            <span className="text-xs text-faint">{formatDateTime(last.created_at)}</span>
          </div>
        ) : (
          <StatusLabel tone="neutral" className="hidden text-xs text-faint sm:inline-flex">never fired</StatusLabel>
        )}
        {isAdmin ? (
          <div className="flex shrink-0 items-center gap-1.5">
            <Button size="sm" onClick={onEdit}>
              Edit
            </Button>
            <Button size="sm" onClick={() => toggle.mutate()} disabled={toggle.isPending}>
              {trigger.enabled ? "Disable" : "Enable"}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm(`Delete trigger “${trigger.name}”?`)) remove.mutate();
              }}
            >
              <Trash2 size={13} />
            </Button>
          </div>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setExpanded((value) => !value)}
          aria-label="Toggle invocations"
        >
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </Button>
      </div>
      {trigger.target_warning ? (
        <p className="mx-5 mb-3 rounded-xl border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn">
          {trigger.target_warning}
        </p>
      ) : null}
      {error ? (
        <div className="px-5 pb-3">
          <ErrorNote message={error} />
        </div>
      ) : null}
      {expanded ? (
        <div className="border-t border-line px-5 py-4">
          <p className="mb-2 text-xs font-medium uppercase tracking-wider text-faint">
            Recent invocations
          </p>
          {invocations.isPending ? <Spinner label="Loading…" /> : null}
          {invocations.data && invocations.data.length === 0 ? (
            <p className="text-sm text-faint">This trigger has not fired yet.</p>
          ) : null}
          <ul className="space-y-1.5">
            {invocations.data?.map((invocation: TriggerInvocation) => (
              <li key={invocation.id} className="flex items-center gap-3 text-sm">
                <Badge tone={invocationTone[invocation.status] ?? "neutral"}>
                  {invocation.status}
                </Badge>
                <span className="text-xs text-faint">
                  {formatDateTime(invocation.created_at)}
                </span>
                {invocation.task_id ? (
                  <Link
                    href={`/tasks/${invocation.task_id}`}
                    className={`rounded-md text-xs font-medium text-accent-strong hover:underline ${focusRing}`}
                  >
                    task {shortId(invocation.task_id)}
                  </Link>
                ) : null}
                {invocation.error_message ? (
                  <span className="truncate text-xs text-danger">{invocation.error_message}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function TriggerBuilder({
  existing,
  onClose,
}: {
  existing: Trigger | null;
  onClose: () => void;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const invalidate = useInvalidateTriggers(workspaceId);
  const connectors = useConnectors();
  const connections = useConnections(workspaceId);
  const agents = useAgents(workspaceId);

  const [name, setName] = useState(existing?.name ?? "");
  const [connectionId, setConnectionId] = useState(existing?.connection_id ?? "");
  const [eventType, setEventType] = useState(existing?.event_type ?? "");
  const [rows, setRows] = useState<ConditionRow[]>(
    existing ? filterToRows(existing.filter_json) : [],
  );
  const [agentId, setAgentId] = useState(existing?.target_agent_id ?? "");
  const [commentBack, setCommentBack] = useState(
    Boolean(existing?.action_config_json.comment_back),
  );
  const [dedupeWindow, setDedupeWindow] = useState(
    String(existing?.dedupe_window_seconds ?? 300),
  );
  // Workflow template picker (Phase 8, plan 8.4): default vs engineering.
  const existingDef = existing?.workflow_definition ?? null;
  const [template, setTemplate] = useState(
    existingDef?.template === "engineering_ticket" ? "engineering_ticket" : "",
  );
  const [qaAgentId, setQaAgentId] = useState(
    typeof existingDef?.qa_agent_id === "string" ? existingDef.qa_agent_id : "",
  );
  const [managerReview, setManagerReview] = useState(Boolean(existingDef?.manager_review));
  const [retestCycles, setRetestCycles] = useState(
    String(existingDef?.max_retest_cycles ?? 3),
  );
  const [error, setError] = useState<string | null>(null);

  const connection = connections.data?.find((c) => c.id === connectionId);
  const connector = connectors.data?.find(
    (c) => c.connector_type === connection?.connector_type,
  );
  const metadata = useConnectionMetadata(workspaceId, connectionId || null, true);
  const teams = (metadata.data?.teams as LinearTeamMetadata[] | undefined) ?? [];
  const agent = agents.data?.find((a) => a.id === agentId);

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name,
        connection_id: connectionId || null,
        event_type: eventType || null,
        filter: rowsToFilter(rows),
        target_agent_id: agentId || null,
        action_config: { comment_back: commentBack },
        dedupe_window_seconds: Number(dedupeWindow) || 0,
        workflow_definition:
          template === "engineering_ticket"
            ? {
                template: "engineering_ticket",
                ...(qaAgentId ? { qa_agent_id: qaAgentId } : {}),
                manager_review: managerReview,
                max_retest_cycles: Number(retestCycles) || 3,
              }
            : null,
      };
      return existing
        ? api(`/api/v1/workspaces/${workspaceId}/triggers/${existing.id}`, {
            method: "PATCH",
            body,
          })
        : api(`/api/v1/workspaces/${workspaceId}/triggers`, { method: "POST", body });
    },
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Save failed."),
  });

  const applyTodoPreset = () => {
    const teamKey = teams[0]?.key ?? "ENG";
    setRows([
      { path: "data.team.key", op: "eq", value: teamKey },
      { path: "data.state.name", op: "transitioned_to", value: "Todo" },
    ]);
  };

  return (
    <Dialog title={existing ? "Edit trigger" : "New trigger"} open onClose={onClose} wide>
      <div className="space-y-5">
        <Field label="Name">
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Pick up new engineering tickets"
          />
        </Field>

        <section className="space-y-3 rounded-xl border border-line bg-raised/60 p-4">
          <p className="text-xs font-semibold uppercase tracking-wider text-accent-strong">When</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Connection">
              <Select
                value={connectionId}
                onChange={(event) => setConnectionId(event.target.value)}
              >
                <option value="">Select a connection…</option>
                {connections.data?.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.connector_type})
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Event type">
              {connector && connector.canonical_events.length > 0 ? (
                <Select
                  value={eventType}
                  onChange={(event) => setEventType(event.target.value)}
                >
                  <option value="">Select an event…</option>
                  {connector.canonical_events.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  value={eventType}
                  onChange={(event) => setEventType(event.target.value)}
                  placeholder="connector.linear.issue.updated"
                />
              )}
            </Field>
          </div>
        </section>

        <section className="space-y-3 rounded-xl border border-line bg-raised/60 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wider text-accent-strong">If</p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={applyTodoPreset}>
                Preset: state changes to Todo
              </Button>
              <Button
                size="sm"
                onClick={() => setRows([...rows, { path: "", op: "eq", value: "" }])}
              >
                <Plus size={13} /> Condition
              </Button>
            </div>
          </div>
          {rows.length === 0 ? (
            <p className="text-sm text-faint">No conditions: every event matches.</p>
          ) : null}
          {rows.map((row, index) => (
            <div key={index} className="flex items-center gap-2">
              <Input
                className="flex-[2]"
                value={row.path}
                placeholder="data.state.name"
                onChange={(event) =>
                  setRows(rows.map((r, i) => (i === index ? { ...r, path: event.target.value } : r)))
                }
              />
              <Select
                className="w-40 flex-none"
                value={row.op}
                onChange={(event) =>
                  setRows(rows.map((r, i) => (i === index ? { ...r, op: event.target.value } : r)))
                }
              >
                {TRIGGER_OPS.map((op) => (
                  <option key={op} value={op}>
                    {OP_LABELS[op]}
                  </option>
                ))}
              </Select>
              {row.op !== "exists" ? (
                row.path === "data.team.key" && teams.length > 0 ? (
                  <Select
                    className="flex-1"
                    value={row.value}
                    onChange={(event) =>
                      setRows(
                        rows.map((r, i) =>
                          i === index ? { ...r, value: event.target.value } : r,
                        ),
                      )
                    }
                  >
                    {teams.map((team) => (
                      <option key={team.key} value={team.key}>
                        {team.key} — {team.name}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input
                    className="flex-1"
                    value={row.value}
                    placeholder="Todo"
                    onChange={(event) =>
                      setRows(
                        rows.map((r, i) =>
                          i === index ? { ...r, value: event.target.value } : r,
                        ),
                      )
                    }
                  />
                )
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                aria-label="Remove condition"
                onClick={() => setRows(rows.filter((_, i) => i !== index))}
              >
                <Trash2 size={13} />
              </Button>
            </div>
          ))}
        </section>

        <section className="space-y-3 rounded-xl border border-line bg-raised/60 p-4">
          <p className="text-xs font-semibold uppercase tracking-wider text-accent-strong">Then</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Assign to agent">
              <Select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                <option value="">Select an agent…</option>
                {agents.data?.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Dedupe window (seconds)" hint="Identical transitions within this window fire once.">
              <Input
                type="number"
                value={dedupeWindow}
                onChange={(event) => setDedupeWindow(event.target.value)}
              />
            </Field>
          </div>
          <label className="flex items-center gap-2 text-sm text-dim">
            <input
              type="checkbox"
              checked={commentBack}
              onChange={(event) => setCommentBack(event.target.checked)}
              className={`h-4 w-4 rounded accent-accent ${focusRing}`}
            />
            Comment the outcome back on the source issue when the task finishes
          </label>

          <div data-testid="workflow-template" className="space-y-3 border-t border-line pt-3">
            <Field
              label="Workflow"
              hint="Standard runs the assigned agent once. The engineering template adds delegated QA review with a failure → fix → retest loop."
            >
              <Select
                value={template}
                onChange={(event) => setTemplate(event.target.value)}
              >
                <option value="">Standard (default)</option>
                <option value="engineering_ticket">Engineering ticket template</option>
              </Select>
            </Field>
            {template === "engineering_ticket" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field
                  label="QA agent"
                  hint="Empty = auto-pick a teammate of the implementer."
                >
                  <Select
                    value={qaAgentId}
                    onChange={(event) => setQaAgentId(event.target.value)}
                  >
                    <option value="">Auto (same team)</option>
                    {agents.data?.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Max retest cycles" hint="Bounds the fail → fix → retest loop.">
                  <Input
                    type="number"
                    min={1}
                    max={10}
                    value={retestCycles}
                    onChange={(event) => setRetestCycles(event.target.value)}
                  />
                </Field>
                <label className="flex items-center gap-2 text-sm text-dim sm:col-span-2">
                  <input
                    type="checkbox"
                    checked={managerReview}
                    onChange={(event) => setManagerReview(event.target.checked)}
                    className={`h-4 w-4 rounded accent-accent ${focusRing}`}
                  />
                  Ask the implementer&apos;s manager for a review before QA
                </label>
              </div>
            ) : null}
          </div>
        </section>

        <p className="rounded-xl border border-line bg-raised px-3.5 py-2.5 text-sm text-dim">
          {summarySentence(eventType, rows, agent, connection)}
        </p>

        <TestPanel
          trigger={existing}
          eventType={eventType}
          teamKey={teams[0]?.key ?? "ENG"}
        />

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={save.isPending || !name || !eventType || !agentId}
            onClick={() => save.mutate()}
          >
            {existing ? "Save changes" : "Create trigger"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

function TestPanel({
  trigger,
  eventType,
  teamKey,
}: {
  trigger: Trigger | null;
  eventType: string;
  teamKey: string;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const initial = useMemo(() => sampleEventFor(eventType, teamKey), [eventType, teamKey]);
  const [sample, setSample] = useState<string | null>(null);
  const [result, setResult] = useState<TriggerTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () => {
      const parsed: unknown = JSON.parse(sample ?? initial);
      return api<TriggerTestResult>(
        `/api/v1/workspaces/${workspaceId}/triggers/${trigger?.id}/test`,
        { method: "POST", body: { event: parsed } },
      );
    },
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (err) =>
      setError(
        err instanceof ApiError
          ? err.detail
          : err instanceof SyntaxError
            ? "Sample event is not valid JSON."
            : "Test failed.",
      ),
  });

  if (!trigger) {
    return (
      <p className="text-sm text-faint">
        Save the trigger first, then reopen it to dry-run sample events against its filter.
      </p>
    );
  }

  return (
    <section className="space-y-3 rounded-xl border border-dashed border-line-strong p-4">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-faint">
          <FlaskConical size={13} aria-hidden /> Test against a sample event
        </p>
        <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
          Run test
        </Button>
      </div>
      <Textarea
        rows={8}
        className="font-mono text-xs"
        value={sample ?? initial}
        onChange={(event) => setSample(event.target.value)}
      />
      <ErrorNote message={error} />
      {result ? (
        <div className="space-y-2" data-testid="trigger-test-result">
          <div className="flex items-center gap-2">
            {result.matched ? (
              <Badge tone="ok">
                <CheckCircle2 size={12} /> matched
              </Badge>
            ) : (
              <Badge tone="danger">
                <XCircle size={12} /> not matched
              </Badge>
            )}
            {!result.event_type_matches ? (
              <span className="text-xs text-danger">event type does not match</span>
            ) : null}
          </div>
          <ul className="space-y-1">
            {result.conditions.map((condition: ConditionExplanation, index: number) => (
              <li key={index} className="flex items-start gap-2 text-xs">
                {condition.passed ? (
                  <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-ok" />
                ) : (
                  <XCircle size={13} className="mt-0.5 shrink-0 text-danger" />
                )}
                <span className="font-mono text-dim">
                  {condition.path} {condition.op}{" "}
                  {condition.op === "exists" ? "" : JSON.stringify(condition.value)}
                </span>
                <span className="text-faint">
                  {condition.detail ||
                    (condition.actual_present
                      ? `actual: ${JSON.stringify(condition.actual)}`
                      : "path absent")}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
