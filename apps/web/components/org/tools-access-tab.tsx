"use client";

/** Agent drawer "Tools & Access" tab (plan 17.5): capability grants,
 * approval-policy preset with its underlying rules, and autonomy display. */

import { useMutation } from "@tanstack/react-query";
import { Check, ChevronDown, Plus, ShieldCheck, ShieldOff, SlidersHorizontal, Trash2 } from "lucide-react";
import { useState } from "react";
import { Badge, Button, ErrorNote, Field, focusRing, Select, Spinner, StatusLabel } from "@/components/ui";
import { ScopeEditor } from "@/components/scope-editor";
import { api, ApiError } from "@/lib/api";
import { describeRisk } from "@/lib/apps";
import {
  buildToolScope,
  mcpServerSlug,
  mcpWildcardCapability,
  missingRequiredScopeKeys,
  type ToolScopeValues,
} from "@/lib/connectors";
import {
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useInvalidateAgentAccess,
  useOrgGraph,
  useTools,
} from "@/lib/hooks";
import {
  describeRule,
  formatScope,
  isCapabilityGranted,
  keptRules,
  PRESET_DESCRIPTIONS,
  PRESET_RULES,
  riskTone,
  sortGrants,
} from "@/lib/policy";
import type { Agent, ApprovalPreset, GrantEffect } from "@/lib/types";
import {
  isPresetGranted,
  missingPolicyRules,
  presetCapabilities,
  presetGrantsToAdd,
  presetGrantsToRevoke,
  presetMissingTools,
  presetScopeGaps,
  TOOL_PRESETS,
  type ToolPreset,
} from "@/lib/wizard";
import { useWorkspace } from "@/lib/workspace-context";

const PRESETS: ApprovalPreset[] = ["autonomous", "balanced", "restricted"];

/** Scope for an `organization.delegate` grant: relationship targets plus an
 * optional pin to one colleague (both must match at delegation time). */
export function delegationScope(targets: string, pinAgentId: string): Record<string, string> {
  const scope: Record<string, string> = { targets };
  if (pinAgentId) scope.target_agent_id = pinAgentId;
  return scope;
}

export function ToolsAccessTab({ agent, canEdit }: { agent: Agent; canEdit: boolean }) {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const grants = useAgentGrants(workspaceId, agent.id);
  const policy = useAgentPolicy(workspaceId, agent.id);
  const tools = useTools(workspaceId);
  const connections = useConnections(workspaceId, can("admin"));
  const graph = useOrgGraph(workspaceId);
  const invalidate = useInvalidateAgentAccess(workspaceId, agent.id);

  const [toolName, setToolName] = useState("");
  const [effect, setEffect] = useState<GrantEffect>("allow");
  const [scopeValues, setScopeValues] = useState<ToolScopeValues>({});
  const [delegationTargets, setDelegationTargets] = useState("subordinates");
  const [delegationPin, setDelegationPin] = useState("");
  const [error, setError] = useState<string | null>(null);

  const [wholeServer, setWholeServer] = useState(false);
  /** null = follow the grants (open when something was granted by hand). */
  const [advancedOverride, setAdvancedOverride] = useState<boolean | null>(null);

  const toolList = tools.data ?? [];
  const selectedTool = toolList.find((tool) => tool.name === toolName);
  const selectedServerSlug = selectedTool ? mcpServerSlug(selectedTool.name) : null;
  const capability =
    selectedServerSlug && wholeServer
      ? mcpWildcardCapability(selectedServerSlug)
      : selectedTool?.required_capability ?? "";
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
            ? delegationScope(delegationTargets, delegationPin)
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
      setDelegationPin("");
      setWholeServer(false);
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

  /** One capability preset on or off: add the grants it needs, or revoke the
   * grants it owns and no other capability that is still on needs. */
  const toggleCapability = useMutation({
    mutationFn: async (preset: ToolPreset) => {
      const current = grants.data ?? [];
      const catalog = tools.data ?? [];
      if (isPresetGranted(current, preset, catalog)) {
        const keep = TOOL_PRESETS.filter(
          (other) => other.id !== preset.id && isPresetGranted(current, other, catalog),
        );
        for (const grant of presetGrantsToRevoke(current, preset, catalog, keep)) {
          await api<void>(
            `/api/v1/workspaces/${workspaceId}/agents/${agent.id}/grants/${grant.id}`,
            { method: "DELETE" },
          );
        }
        return;
      }
      for (const body of presetGrantsToAdd(current, preset, catalog, connections.data ?? [])) {
        await api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/grants`, {
          method: "POST",
          body,
        });
      }
      // The bundle's own approval rules, the same ones the wizard writes: a
      // capability granted here must arrive with the gate the bundle promises,
      // not only when the agent was created through the wizard.
      const existing = policy.data?.rules ?? [];
      const missing = missingPolicyRules(existing, preset);
      if (missing.length > 0) {
        await api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/policy`, {
          method: "PUT",
          body: { rules: [...missing, ...existing] },
        });
      }
    },
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "Updating capabilities failed."),
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
  const appliedPresets = TOOL_PRESETS.filter((preset) =>
    isPresetGranted(grantList, preset, toolList),
  );
  const presetCapabilitySet = new Set(
    appliedPresets.flatMap((preset) => presetCapabilities(preset, toolList)),
  );
  const handPickedGrants = grantList.filter(
    (grant) => !presetCapabilitySet.has(grant.capability),
  );
  const advancedOpen = advancedOverride ?? handPickedGrants.length > 0;

  return (
    <div className="space-y-6">
      <ErrorNote message={error} />

      <section>
        <h3 className="mb-1 font-display text-base font-semibold">Capabilities</h3>
        <p className="mb-3 text-sm text-dim">
          What this agent can do, in plain language. Turning a capability off revokes the grants
          behind it; anything not listed here stays blocked.
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {TOOL_PRESETS.map((preset) => {
            const granted = isPresetGranted(grantList, preset, toolList);
            const missing = presetMissingTools(preset, toolList);
            const gaps = granted
              ? []
              : presetScopeGaps(preset, toolList, connections.data ?? []);
            const unavailable = missing.length === Object.keys(preset.tools).length;
            const blocked = !granted && gaps.length > 0;
            return (
              <button
                key={preset.id}
                type="button"
                data-testid={`capability-preset-${preset.id}`}
                aria-pressed={granted}
                title={
                  blocked
                    ? `Needs a connection first: ${gaps.join(", ")}`
                    : preset.description
                }
                disabled={!canEdit || unavailable || blocked || toggleCapability.isPending}
                onClick={() => toggleCapability.mutate(preset)}
                className={`rounded-xl border px-3 py-2.5 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing} ${
                  granted ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    aria-hidden
                    className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border ${
                      granted ? "border-accent bg-accent text-white" : "border-line-strong"
                    }`}
                  >
                    {granted ? <Check size={11} strokeWidth={3} /> : null}
                  </span>
                  <span className="font-medium">{preset.label}</span>
                </span>
                <span className="mt-1 block text-xs leading-snug text-dim">{preset.summary}</span>
              </button>
            );
          })}
        </div>
      </section>

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
                className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2 text-sm"
              >
                {grant.effect === "allow" ? (
                  <ShieldCheck size={15} className="shrink-0 text-ok" />
                ) : (
                  <ShieldOff size={15} className="shrink-0 text-danger" />
                )}
                <code className="min-w-0 flex-1 truncate font-mono text-[13px]" title={grant.capability}>{grant.capability}</code>
                <span className="min-w-0 truncate text-xs text-faint" title={formatScope(grant.scope_json)}>{formatScope(grant.scope_json)}</span>
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

        {canEdit ? null : (
          <p className="mt-2 text-sm text-dim">Grants can be changed by workspace admins.</p>
        )}
      </section>

      <div className="rounded-2xl border border-line bg-surface shadow-card">
        <button
          type="button"
          data-testid="advanced-access-toggle"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOverride(!advancedOpen)}
          className={`flex w-full items-center gap-3 rounded-2xl px-4 py-3 text-left ${focusRing}`}
        >
          <SlidersHorizontal size={15} className="shrink-0 text-faint" />
          <span className="min-w-0 flex-1">
            <span className="block text-[13px] font-medium">
              Advanced: choose individual tools
            </span>
            <span className="mt-0.5 block text-xs text-dim">
              {grantList.length > 0
                ? `${grantList.length} ${grantList.length === 1 ? "grant" : "grants"} · ${handPickedGrants.length} outside the capabilities above`
                : `Grant one of the ${toolList.length} tools in this workspace and scope it`}
            </span>
          </span>
          <ChevronDown
            size={15}
            className={`shrink-0 text-faint transition-transform ${advancedOpen ? "rotate-180" : ""}`}
          />
        </button>
        {advancedOpen ? (
          <div data-testid="advanced-access" className="space-y-6 border-t border-line px-4 py-4">
        {canEdit ? (
          <form
            className="mt-3 space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (selectedTool && missingRequired.length === 0) addGrant.mutate();
            }}
          >
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-0 flex-1">
                <Field label="Capability">
                  <Select
                    className="max-w-full"
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
                <Field
                  label="Pin to one colleague (optional)"
                  hint="Narrows the grant further: only this colleague can be delegated to, and they must also match the relationship above."
                >
                  <Select
                    value={delegationPin}
                    onChange={(e) => setDelegationPin(e.target.value)}
                    data-testid="delegation-pin"
                  >
                    <option value="">Anyone matching the relationship</option>
                    {(graph.data?.agents ?? [])
                      .filter((candidate) => candidate.id !== agent.id)
                      .map((candidate) => (
                        <option key={candidate.id} value={candidate.id}>
                          {candidate.name}
                          {candidate.role_title ? ` — ${candidate.role_title}` : ""}
                        </option>
                      ))}
                  </Select>
                </Field>
              </div>
            ) : selectedTool && selectedTool.scope_keys.length > 0 ? (
              <div
                data-testid={selectedTool.name.startsWith("cli.") ? "cli-scope" : "connector-scope"}
                className="rounded-xl border border-line bg-surface px-3 py-2.5"
              >
                {selectedServerSlug ? (
                  <label className="mb-3 flex items-start gap-2 text-sm" data-testid="mcp-whole-server">
                    <input
                      type="checkbox"
                      aria-label="Every tool on this server"
                      checked={wholeServer}
                      onChange={(event) => setWholeServer(event.target.checked)}
                    />
                    <span>
                      Every tool on this MCP server
                      <span className="block text-xs text-dim">
                        Grants <code className="font-mono">{mcpWildcardCapability(selectedServerSlug)}</code> instead of just this
                        tool. Narrow it with the tool pattern below (for example <code className="font-mono">get_*</code>).
                      </span>
                    </span>
                  </label>
                ) : null}
                <ScopeEditor
                  tool={selectedTool}
                  connections={matchingConnections}
                  values={scopeValues}
                  onChange={setScopeValues}
                />
              </div>
            ) : null}
          </form>
        ) : null}

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
                <div className="flex flex-wrap items-center gap-2">
                  <code className="min-w-0 truncate font-mono text-[13px] font-medium" title={tool.name}>{tool.name}</code>
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
                {mcpServerSlug(tool.name) ? (
                  <p className="text-[11px] text-faint">{describeRisk(tool.risk)} · from the “{mcpServerSlug(tool.name)}” MCP server</p>
                ) : null}
              </li>
            );
          })}
        </ul>
        </section>
          </div>
        ) : null}
      </div>

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
                {rule.capability !== "*" ? (
                  <Badge tone="neutral">kept when the mode changes</Badge>
                ) : null}
              </li>
            ))}
          </ul>
          {keptRules(rules).length > 0 ? (
            <p data-testid="kept-rules-note" className="mt-2 text-xs text-dim">
              A rule about one tool is a decision of its own: picking another mode restates the
              risk levels and leaves it standing.
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
