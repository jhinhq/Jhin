"use client";

/** Agent drawer "Tools & Access" tab (plan 17.5): capability grants,
 * approval-policy preset with its underlying rules, and autonomy display. */

import { useMutation } from "@tanstack/react-query";
import { Plus, ShieldCheck, ShieldOff, Trash2 } from "lucide-react";
import { useState } from "react";
import { Badge, Button, ErrorNote, Field, focusRing, Select, Spinner, StatusLabel } from "@/components/ui";
import { ScopeEditor } from "@/components/scope-editor";
import { api, ApiError } from "@/lib/api";
import { buildToolScope, missingRequiredScopeKeys, type ToolScopeValues } from "@/lib/connectors";
import {
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useInvalidateAgentAccess,
  useTools,
} from "@/lib/hooks";
import {
  describeRule,
  formatScope,
  isCapabilityGranted,
  PRESET_DESCRIPTIONS,
  PRESET_RULES,
  riskTone,
  sortGrants,
} from "@/lib/policy";
import type { Agent, ApprovalPreset, GrantEffect } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const PRESETS: ApprovalPreset[] = ["autonomous", "balanced", "restricted"];

export function ToolsAccessTab({ agent, canEdit }: { agent: Agent; canEdit: boolean }) {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const grants = useAgentGrants(workspaceId, agent.id);
  const policy = useAgentPolicy(workspaceId, agent.id);
  const tools = useTools(workspaceId);
  const connections = useConnections(workspaceId, can("admin"));
  const invalidate = useInvalidateAgentAccess(workspaceId, agent.id);

  const [toolName, setToolName] = useState("");
  const [effect, setEffect] = useState<GrantEffect>("allow");
  const [scopeValues, setScopeValues] = useState<ToolScopeValues>({});
  const [delegationTargets, setDelegationTargets] = useState("subordinates");
  const [error, setError] = useState<string | null>(null);

  const toolList = tools.data ?? [];
  const selectedTool = toolList.find((tool) => tool.name === toolName);
  const capability = selectedTool?.required_capability ?? "";
  const isDelegate = capability === "organization.delegate";
  const matchingConnections = (connections.data ?? []).filter(
    (connection) => connection.connector_type === selectedTool?.name.split(".", 1)[0],
  );
  const missingRequired = selectedTool
    ? missingRequiredScopeKeys(selectedTool, scopeValues)
    : [];

  const addGrant = useMutation({
    mutationFn: () =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/grants`, {
        method: "POST",
        body: {
          capability,
          scope: isDelegate
            ? { targets: delegationTargets }
            : selectedTool
              ? buildToolScope(selectedTool, scopeValues)
              : {},
          effect,
        },
      }),
    onSuccess: () => {
      setError(null);
      setToolName("");
      setScopeValues({});
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Adding the grant failed."),
  });

  const revokeGrant = useMutation({
    mutationFn: (grantId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/grants/${grantId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Revoking failed."),
  });

  const setPreset = useMutation({
    mutationFn: (preset: ApprovalPreset) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/policy`, {
        method: "PUT",
        body: { preset },
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : "Updating policy failed."),
  });

  if (grants.isPending || tools.isPending || policy.isPending) {
    return <Spinner label="Loading tools & access…" />;
  }

  const grantList = sortGrants(grants.data ?? []);
  const currentPreset = policy.data?.preset ?? null;
  const rules = policy.data?.rules ?? [];

  return (
    <div className="space-y-6">
      <ErrorNote message={error} />

      <section>
        <h3 className="mb-1 font-display text-base font-semibold">Capability grants</h3>
        <p className="mb-3 text-sm text-dim">
          Deny-by-default: this agent can only call tools matching an allow grant, and an
          explicit deny always wins. Changes apply immediately, even mid-run.
        </p>
        {grantList.length === 0 ? (
          <p
            data-testid="no-grants"
            className="rounded-2xl border border-dashed border-line-strong bg-surface/60 px-4 py-5 text-center text-sm text-dim"
          >
            No grants — this agent cannot call any tool.
          </p>
        ) : (
          <ul className="space-y-2">
            {grantList.map((grant) => (
              <li
                key={grant.id}
                className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2 text-sm"
              >
                {grant.effect === "allow" ? (
                  <ShieldCheck size={15} className="shrink-0 text-ok" />
                ) : (
                  <ShieldOff size={15} className="shrink-0 text-danger" />
                )}
                <code className="min-w-0 flex-1 truncate font-mono text-[13px]">{grant.capability}</code>
                <span className="text-xs text-faint">{formatScope(grant.scope_json)}</span>
                <Badge tone={grant.effect === "allow" ? "ok" : "danger"}>{grant.effect}</Badge>
                {canEdit ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Revoke ${grant.capability}`}
                    disabled={revokeGrant.isPending}
                    onClick={() => revokeGrant.mutate(grant.id)}
                  >
                    <Trash2 size={13} />
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}

        {canEdit ? (
          <form
            className="mt-3 space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (selectedTool && missingRequired.length === 0) addGrant.mutate();
            }}
          >
            <div className="flex items-end gap-2">
              <div className="flex-1">
                <Field label="Capability">
                  <Select
                    value={toolName}
                    onChange={(e) => {
                      setToolName(e.target.value);
                      setScopeValues({});
                    }}
                  >
                    <option value="">Choose a capability…</option>
                    {toolList.map((tool) => (
                      <option key={tool.name} value={tool.name}>
                        {tool.name}
                        {tool.required_capability !== tool.name
                          ? ` — ${tool.required_capability}`
                          : ""}{" "}
                        ({tool.risk})
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
              <div className="w-28">
                <Field label="Effect">
                  <Select
                    value={effect}
                    onChange={(e) => setEffect(e.target.value as GrantEffect)}
                  >
                    <option value="allow">allow</option>
                    <option value="deny">deny</option>
                  </Select>
                </Field>
              </div>
              <Button
                type="submit"
                variant="primary"
                disabled={!selectedTool || missingRequired.length > 0 || addGrant.isPending}
              >
                <Plus size={13} /> Add
              </Button>
            </div>
            {isDelegate ? (
              <div
                data-testid="delegation-scope"
                className="rounded-xl border border-line bg-surface px-3 py-2.5"
              >
                <Field
                  label="Delegation targets"
                  hint="Who this agent may delegate tasks to. Deny-by-default: without this grant the agent cannot delegate at all."
                >
                  <Select
                    value={delegationTargets}
                    onChange={(e) => setDelegationTargets(e.target.value)}
                  >
                    <option value="subordinates">
                      Subordinates — direct and indirect reports
                    </option>
                    <option value="team">Team — agents on the same team</option>
                    <option value="any">Any agent in the workspace</option>
                  </Select>
                </Field>
              </div>
            ) : selectedTool && selectedTool.scope_keys.length > 0 ? (
              <div
                data-testid={selectedTool.name.startsWith("cli.") ? "cli-scope" : "connector-scope"}
                className="rounded-xl border border-line bg-surface px-3 py-2.5"
              >
                <ScopeEditor
                  tool={selectedTool}
                  connections={matchingConnections}
                  values={scopeValues}
                  onChange={setScopeValues}
                />
              </div>
            ) : null}
          </form>
        ) : (
          <p className="mt-2 text-sm text-dim">Grants can be changed by workspace admins.</p>
        )}
      </section>

      <section>
        <h3 className="mb-1 font-display text-base font-semibold">Available tools</h3>
        <p className="mb-3 text-sm text-dim">
          The registered catalog. A tool is callable only when its capability is granted above.
        </p>
        <ul className="space-y-2">
          {toolList.map((tool) => {
            const granted = isCapabilityGranted(grantList, tool.required_capability);
            return (
              <li
                key={tool.name}
                data-testid={`tool-${tool.name}`}
                className="rounded-xl border border-line bg-surface px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  <code className="font-mono text-[13px] font-medium">{tool.name}</code>
                  <Badge tone={riskTone(tool.risk)}>{tool.risk}</Badge>
                  {tool.supports_approval ? <Badge tone="neutral">approvable</Badge> : null}
                  <span className="ml-auto">
                    {granted ? (
                      <StatusLabel tone="ok" className="!text-xs">granted</StatusLabel>
                    ) : (
                      <StatusLabel tone="neutral" className="!text-xs text-faint">not granted</StatusLabel>
                    )}
                  </span>
                </div>
                <p className="mt-1 text-xs text-dim">{tool.description}</p>
              </li>
            );
          })}
        </ul>
      </section>

      <section>
        <h3 className="mb-1 font-display text-base font-semibold">Approval policy</h3>
        <p className="mb-3 text-sm text-dim">
          Presets are shortcuts — the explicit rules below are what is saved and enforced.
          Autonomy: <Badge tone="neutral">{agent.autonomy_level}</Badge>
        </p>
        <div className="grid gap-2 sm:grid-cols-3">
          {PRESETS.map((preset) => {
            const active = currentPreset === preset;
            return (
              <button
                key={preset}
                type="button"
                disabled={!canEdit || setPreset.isPending}
                onClick={() => setPreset.mutate(preset)}
                data-testid={`preset-${preset}`}
                aria-pressed={active}
                className={`rounded-xl border px-3 py-2.5 text-left transition-colors ${focusRing} ${
                  active
                    ? "border-accent bg-accent-soft"
                    : "border-line bg-raised hover:border-line-strong"
                } ${canEdit ? "" : "cursor-not-allowed opacity-60"}`}
              >
                <p className="text-[13px] font-medium capitalize">{preset}</p>
                <p className="mt-1 text-xs leading-snug text-dim">
                  {PRESET_DESCRIPTIONS[preset]}
                </p>
              </button>
            );
          })}
        </div>
        <div className="mt-3 rounded-2xl border border-line bg-surface px-4 py-3 shadow-card">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">
            {currentPreset
              ? `Rules set by “${currentPreset}”`
              : rules.length > 0
                ? "Custom rules"
                : "No rules — risk-level defaults apply"}
          </p>
          <ul className="space-y-1">
            {(rules.length > 0 ? rules : PRESET_RULES.balanced).map((rule, index) => (
              <li key={index} className="flex items-center gap-2 text-xs">
                <Badge tone={riskTone(rule.risk)}>{rule.risk ?? "any"}</Badge>
                <span className={rules.length > 0 ? "text-ink" : "text-faint"}>
                  {describeRule(rule)}
                  {rules.length === 0 ? " (default)" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </div>
  );
}
