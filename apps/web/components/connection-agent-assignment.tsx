"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ScopeEditor } from "@/components/scope-editor";
import { Badge, Button, ErrorNote, Field, Select, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { buildToolScope, missingRequiredScopeKeys, type ToolScopeValues } from "@/lib/connectors";
import { useAgents, useConnectionTools, useInvalidateAgentAccess, useTools } from "@/lib/hooks";
import type { ConnectionInfo } from "@/lib/types";

type Props = { workspaceId: string; connection: ConnectionInfo };
type Result = { saved: boolean; error?: string };

export function ConnectionAgentAssignment(props: Props) {
  const [open, setOpen] = useState(false);
  return open ? <AssignmentForm {...props} /> : (
    <Button size="sm" variant="primary" onClick={() => setOpen(true)}>Give to an agent…</Button>
  );
}

function AssignmentForm({ workspaceId, connection }: Props) {
  const agents = useAgents(workspaceId);
  const catalog = useTools(workspaceId);
  const connectionTools = useConnectionTools(workspaceId, connection.id);
  const queryClient = useQueryClient();
  const [agentId, setAgentId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [scopes, setScopes] = useState<Record<string, ToolScopeValues>>({});
  const [results, setResults] = useState<Record<string, Result>>({});
  const invalidate = useInvalidateAgentAccess(workspaceId, agentId);
  // The workspace catalog supplies authoritative capabilities and required scopes.
  // The connection catalog limits this list to the tools this app actually offers.
  const definitions = new Map((catalog.data ?? []).map((tool) => [tool.name, tool]));
  const tools = (connectionTools.data?.tools ?? []).flatMap((connectionTool) => {
    const definition = definitions.get(connectionTool.name);
    return definition?.scope_keys.includes("connection_id")
      ? [{ ...definition, risk: connectionTool.risk ?? definition.risk }]
      : [];
  });
  const missingDefinitions = (connectionTools.data?.tools ?? []).some((tool) => !definitions.has(tool.name));
  const pending = tools.filter((tool) => selected.includes(tool.name) && !results[tool.name]?.saved);
  const savedCount = Object.values(results).filter((result) => result.saved).length;
  const failedCount = Object.values(results).filter((result) => !result.saved).length;
  const scopeFor = (name: string) => ({ ...scopes[name], connection_id: connection.id });
  const missingScope = pending.some((tool) => missingRequiredScopeKeys(tool, scopeFor(tool.name)).length > 0);
  const assign = useMutation({
    mutationFn: async () => {
      for (const tool of pending) {
        try {
          await api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/grants`, {
            method: "POST",
            body: { capability: tool.required_capability, scope: { ...buildToolScope(tool, scopeFor(tool.name)), connection_id: connection.id }, effect: "allow" },
          });
          setResults((previous) => ({ ...previous, [tool.name]: { saved: true } }));
        } catch (error) {
          setResults((previous) => ({ ...previous, [tool.name]: {
            saved: false,
            error: error instanceof ApiError ? error.detail : "Assignment failed. Try this tool again.",
          } }));
        }
      }
    },
    onSettled: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["connection-access-summary", workspaceId, connection.id] });
      void queryClient.invalidateQueries({ queryKey: ["tools", workspaceId] });
    },
  });

  if (agents.isPending || catalog.isPending || connectionTools.isPending) return <Spinner />;
  if (agents.error || catalog.error || connectionTools.error) return (
    <div className="space-y-2">
      <ErrorNote message="Could not load agents or app tools. Retry before assigning access." />
      <Button size="sm" onClick={() => { void agents.refetch(); void catalog.refetch(); void connectionTools.refetch(); }}>Retry</Button>
    </div>
  );
  const activeAgents = (agents.data ?? []).filter((agent) => agent.status !== "disabled");
  return (
    <div data-testid="give-to-agent" className="space-y-3 rounded-xl border border-line p-4">
      <p className="text-sm font-medium">Assign {connection.name} to an agent</p>
      <p className="text-xs text-dim">Choose tools for this connection. Existing deny grants and approval policies still apply.</p>
      {missingDefinitions ? <div className="space-y-2">
        <p className="text-xs text-dim">Some newly discovered tools are still loading into the tool catalog.</p>
        <Button size="sm" onClick={() => { void catalog.refetch(); }}>Refresh tool catalog</Button>
      </div> : null}
      <fieldset disabled={assign.isPending} className="min-w-0 space-y-3">
        <Field label="Agent">
          <Select aria-label="Agent" value={agentId} onChange={(event) => { setAgentId(event.target.value); setResults({}); }}>
            <option value="">Choose an agent…</option>
            {activeAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </Select>
        </Field>
        {activeAgents.length === 0 ? <p className="text-sm text-dim">Create or enable an agent before assigning this app.</p> : null}
        {connection.status !== "active" ? <ErrorNote message="Reconnect or enable this app before assigning tools." /> : null}
        {tools.length === 0 ? <p className="text-sm text-dim">No assignable tools are available. Check the Tools tab and refresh discovery for this app.</p> : (
          <>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => setSelected(tools.filter((tool) => tool.risk === "read").map((tool) => tool.name))}>Select read-only</Button>
              <Button size="sm" onClick={() => setSelected(tools.map((tool) => tool.name))}>Select all</Button>
              <Button size="sm" onClick={() => setSelected([])}>Clear selection</Button>
            </div>
            <div className="max-h-96 space-y-2 overflow-y-auto">
              {tools.map((tool) => (
                <div key={tool.name} className="space-y-2 rounded-lg border border-line p-3">
                  <label className="flex items-start gap-2 text-sm">
                    <input type="checkbox" aria-label={tool.name} className="mt-1" checked={selected.includes(tool.name)}
                      disabled={results[tool.name]?.saved}
                      onChange={(event) => setSelected((previous) => event.target.checked ? [...previous, tool.name] : previous.filter((name) => name !== tool.name))} />
                    <span className="min-w-0 flex-1 break-words">{tool.name}</span>
                    <Badge tone={tool.risk === "read" ? "neutral" : "warn"}>{tool.risk}</Badge>
                  </label>
                  {selected.includes(tool.name) ? <p className="text-xs text-dim">{tool.description}</p> : null}
                  {selected.includes(tool.name) && !results[tool.name]?.saved ? (
                    <ScopeEditor tool={{ ...tool, scope_keys: tool.scope_keys.filter((key) => key !== "connection_id") }} connections={[connection]}
                      values={scopes[tool.name] ?? {}} onChange={(values) => setScopes((previous) => ({ ...previous, [tool.name]: values }))} />
                  ) : null}
                  {results[tool.name]?.saved ? <p className="text-xs text-ok">Grant saved</p> : null}
                  {results[tool.name]?.error ? <ErrorNote message={results[tool.name].error!} /> : null}
                </div>
              ))}
            </div>
          </>
        )}
        {missingScope ? <p className="text-xs text-dim">Complete the required scopes for each selected tool.</p> : null}
        <Button size="sm" variant="primary" disabled={!agentId || pending.length === 0 || missingScope || connection.status !== "active"} onClick={() => assign.mutate()}>
          {assign.isPending ? "Assigning…" : failedCount > 0 ? "Retry failed assignments" : "Assign to agent"}
        </Button>
      </fieldset>
      {savedCount > 0 || failedCount > 0 ? <p role="status" className="text-sm text-dim">
        {savedCount} tool {savedCount === 1 ? "grant" : "grants"} saved{failedCount > 0 ? `; ${failedCount} failed. Review the errors and retry.` : ". Check Agent access below for effective permissions."}
      </p> : null}
    </div>
  );
}
