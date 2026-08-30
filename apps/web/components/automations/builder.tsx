"use client";

/** The automation builder dialog: WHEN (connection + canonical event type),
 * IF (condition rows over the safe filter DSL, with a "State changes to Todo"
 * preset and a team picker sourced from connector metadata), THEN (assign to
 * an agent, optional comment-back), plus a test panel that dry-runs the
 * filter against a sample event with per-condition pass/fail explanations.
 *
 * Used by /automations. The API keeps calling these records "triggers"; the
 * UI calls them automations everywhere a person reads.
 */

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, FlaskConical, Plus, Trash2, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { triggerWhen } from "@/components/company/agent-helpers";
import { LoadError } from "@/components/company/bits";
import {
  Badge,
  Button,
  Dialog,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Select,
  Textarea,
} from "@/components/ui";
import { api, errorText } from "@/lib/api";
import {
  useAgents,
  useConnectionMetadata,
  useConnections,
  useConnectors,
  useInvalidateTriggers,
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
  ConditionExplanation,
  LinearTeamMetadata,
  Trigger,
  TriggerTestResult,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export function AutomationBuilder({
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

  // The test endpoint evaluates the saved filter, so unsaved edits to the
  // WHEN/IF clauses would silently dry-run the wrong thing.
  const filterDirty =
    existing !== null &&
    (eventType !== (existing.event_type ?? "") ||
      JSON.stringify(rows) !== JSON.stringify(filterToRows(existing.filter_json)));

  const save = useMutation({
    mutationFn: () => {
      // Start from the saved definition: the API stores keys this dialog does
      // not edit (e.g. implementer_agent_id, which routes the ticket) and a
      // rename must not silently strip them.
      const definition: Record<string, unknown> | null =
        template === "engineering_ticket"
          ? {
              ...(existingDef?.template === "engineering_ticket" ? existingDef : {}),
              template: "engineering_ticket",
              manager_review: managerReview,
              max_retest_cycles: Number(retestCycles) || 3,
            }
          : null;
      if (definition) {
        if (qaAgentId) definition.qa_agent_id = qaAgentId;
        else delete definition.qa_agent_id;
      }
      const body = {
        name,
        connection_id: connectionId || null,
        event_type: eventType || null,
        filter: rowsToFilter(rows),
        target_agent_id: agentId || null,
        action_config: { comment_back: commentBack },
        dedupe_window_seconds: Number(dedupeWindow) || 0,
        workflow_definition: definition,
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
    onError: (err) =>
      setError(errorText(err, "We couldn’t save this automation. Check the fields and try again.")),
  });

  const applyTodoPreset = () => {
    const teamKey = teams[0]?.key ?? "ENG";
    setRows([
      { path: "data.team.key", op: "eq", value: teamKey },
      { path: "data.state.name", op: "transitioned_to", value: "Todo" },
    ]);
  };

  return (
    <Dialog title={existing ? "Edit automation" : "New automation"} open onClose={onClose} wide>
      <div className="space-y-5">
        {connections.isError ? (
          <LoadError what="your connections" onRetry={() => void connections.refetch()} />
        ) : null}
        {connectors.isError ? (
          <LoadError what="the event list" onRetry={() => void connectors.refetch()} />
        ) : null}
        {agents.isError ? (
          <LoadError what="your agents" onRetry={() => void agents.refetch()} />
        ) : null}
        <Field label="Name (required)">
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
                disabled={connections.isPending}
                onChange={(event) => setConnectionId(event.target.value)}
              >
                {connections.isPending ? (
                  <option value="">Loading…</option>
                ) : (
                  <>
                    <option value="">Select a connection…</option>
                    {connections.data?.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} ({item.connector_type})
                      </option>
                    ))}
                  </>
                )}
              </Select>
            </Field>
            <Field
              label="Event type (required)"
              hint={
                connectors.isPending || (connector && connector.canonical_events.length > 0)
                  ? undefined
                  : "Pick a connection to choose from a list, or type the exact event name."
              }
            >
              {connectors.isPending ? (
                <Select value="" disabled>
                  <option value="">Loading…</option>
                </Select>
              ) : connector && connector.canonical_events.length > 0 ? (
                <Select
                  value={eventType}
                  onChange={(event) => setEventType(event.target.value)}
                >
                  <option value="">Select an event…</option>
                  {connector.canonical_events.map((item) => (
                    <option key={item} value={item}>
                      {triggerWhen(item, undefined)}
                    </option>
                  ))}
                  {eventType && !connector.canonical_events.includes(eventType) ? (
                    <option value={eventType}>{triggerWhen(eventType, undefined)}</option>
                  ) : null}
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
            <div key={index} className="grid gap-2 sm:flex sm:items-center">
              <Input
                className="flex-[2]"
                value={row.path}
                placeholder="data.state.name"
                aria-label={`Condition ${index + 1} field path`}
                onChange={(event) =>
                  setRows(rows.map((r, i) => (i === index ? { ...r, path: event.target.value } : r)))
                }
              />
              <Select
                className="w-full flex-none sm:w-40"
                value={row.op}
                aria-label={`Condition ${index + 1} comparison`}
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
                    aria-label={`Condition ${index + 1} value`}
                    onChange={(event) =>
                      setRows(
                        rows.map((r, i) =>
                          i === index ? { ...r, value: event.target.value } : r,
                        ),
                      )
                    }
                  >
                    <option value="">Select a team…</option>
                    {teams.map((team) => (
                      <option key={team.key} value={team.key}>
                        {team.key} — {team.name}
                      </option>
                    ))}
                    {row.value && !teams.some((team) => team.key === row.value) ? (
                      <option value={row.value}>{row.value}</option>
                    ) : null}
                  </Select>
                ) : (
                  <Input
                    className="flex-1"
                    value={row.value}
                    placeholder="Todo"
                    aria-label={`Condition ${index + 1} value`}
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
                aria-label={`Remove condition ${index + 1}`}
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
            <Field label="Assign to agent (required)">
              <Select
                value={agentId}
                disabled={agents.isPending}
                onChange={(event) => setAgentId(event.target.value)}
              >
                {agents.isPending ? (
                  <option value="">Loading…</option>
                ) : (
                  <>
                    <option value="">Select an agent…</option>
                    {agents.data?.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </>
                )}
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
                    disabled={agents.isPending}
                    onChange={(event) => setQaAgentId(event.target.value)}
                  >
                    {agents.isPending ? (
                      <option value="">Loading…</option>
                    ) : (
                      <>
                        <option value="">Auto (same team)</option>
                        {agents.data?.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </>
                    )}
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
          dirty={filterDirty}
        />

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={save.isPending || !name || !eventType || !agentId}
            onClick={() => save.mutate()}
          >
            {existing ? "Save changes" : "Create automation"}
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
  dirty,
}: {
  trigger: Trigger | null;
  eventType: string;
  teamKey: string;
  /** True when the WHEN/IF clauses differ from what is saved; the test
   * endpoint only knows the saved version. */
  dirty: boolean;
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
        err instanceof SyntaxError
          ? "Sample event is not valid JSON."
          : errorText(err, "The test didn’t run. Try again."),
      ),
  });

  if (!trigger) {
    return (
      <p className="text-sm text-faint">
        Save the automation first, then reopen it to dry-run sample events against its filter.
      </p>
    );
  }

  return (
    <section className="space-y-3 rounded-xl border border-dashed border-line-strong p-4">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-faint">
          <FlaskConical size={13} aria-hidden /> Test against a sample event
        </p>
        <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending || dirty}>
          Run test
        </Button>
      </div>
      {dirty ? (
        <p className="text-[13px] text-faint">
          Save your changes first — the test runs against the saved version.
        </p>
      ) : null}
      <Textarea
        rows={8}
        className="font-mono text-xs"
        aria-label="Sample event JSON"
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
                <span className="min-w-0 font-mono text-dim [overflow-wrap:anywhere]">
                  {condition.path} {condition.op}{" "}
                  {condition.op === "exists" ? "" : JSON.stringify(condition.value)}
                </span>
                <span className="min-w-0 text-faint [overflow-wrap:anywhere]">
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
