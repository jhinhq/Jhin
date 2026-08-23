"use client";

/** "Tools" tab of a connection: every tool reachable through it with its
 * enforced risk. For MCP connections admins can re-discover and override
 * the risk of individual tools (docs/architecture/mcp.md). */

import { useMutation } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { Badge, Button, ErrorNote, Select, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { describeRisk } from "@/lib/apps";
import { formatDateTime } from "@/lib/format";
import { riskTone } from "@/lib/policy";
import type { ConnectionToolInfo, ConnectionToolsOut, RiskLevel } from "@/lib/types";

const RISKS: RiskLevel[] = ["read", "write", "elevated", "destructive"];

function annotationChips(tool: ConnectionToolInfo): string[] {
  const chips: string[] = [];
  const a = tool.annotations;
  if (a.read_only_hint === true) chips.push("read-only");
  if (a.destructive_hint === true) chips.push("destructive");
  if (a.idempotent_hint === true) chips.push("idempotent");
  if (a.open_world_hint === true) chips.push("reaches the open web");
  return chips;
}

export function ConnectionTools({
  workspaceId,
  connectionId,
  data,
  isPending,
  error,
  canManage,
  onChanged,
}: {
  workspaceId: string;
  connectionId: string;
  data: ConnectionToolsOut | undefined;
  isPending: boolean;
  error: unknown;
  canManage: boolean;
  onChanged: (next: ConnectionToolsOut) => void;
}) {
  const base = `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/tools`;
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useMutation({
    mutationFn: () => api<ConnectionToolsOut>(`${base}?refresh=true`),
    onSuccess: (next) => {
      setActionError(null);
      onChanged(next);
    },
    onError: (err) => setActionError(err instanceof ApiError ? err.detail : "Could not re-check the server's tools."),
  });

  const override = useMutation({
    mutationFn: ({ slug, risk }: { slug: string; risk: RiskLevel | null }) =>
      api<ConnectionToolsOut>(base, {
        method: "PATCH",
        body: { tool_risk_overrides: { [slug]: risk } },
      }),
    onSuccess: (next) => {
      setActionError(null);
      onChanged(next);
    },
    onError: (err) => setActionError(err instanceof ApiError ? err.detail : "Could not change the risk level."),
  });

  if (isPending) return <Spinner label="Checking the tools…" />;
  if (error || !data) {
    return (
      <ErrorNote
        message={error instanceof ApiError ? error.detail : "The tools could not be loaded. Check the connection and try again."}
      />
    );
  }

  return (
    <div className="space-y-3" data-testid="connection-tools">
      <ErrorNote message={actionError} />
      <div className="flex flex-wrap items-center gap-2 text-xs text-dim">
        <span>
          {data.tools.length} {data.tools.length === 1 ? "tool" : "tools"}
          {data.dynamic && data.discovered_at ? ` · checked ${formatDateTime(data.discovered_at)}` : ""}
        </span>
        {data.capability_pattern ? (
          <span>
            · grant <code className="font-mono text-ink">{data.capability_pattern}</code> for everything here
          </span>
        ) : null}
        {data.dynamic && canManage ? (
          <Button
            size="sm"
            className="ml-auto"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            <RefreshCw size={12} /> {refresh.isPending ? "Re-checking…" : "Re-check tools"}
          </Button>
        ) : null}
      </div>
      {data.tools.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-strong px-3 py-4 text-center text-sm text-dim">
          No tools found yet. Verify the connection to discover them.
        </p>
      ) : (
        <ul className="max-h-80 space-y-2 overflow-y-auto pr-1">
          {data.tools.map((tool) => {
            const slug = tool.name.split(".").at(-1) ?? tool.name;
            const chips = annotationChips(tool);
            return (
              <li
                key={tool.name}
                data-testid={`connection-tool-${tool.name}`}
                className="rounded-xl border border-line bg-raised px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-[13px] font-medium text-ink">{tool.name}</code>
                  <Badge tone={riskTone(tool.risk)}>{tool.risk}</Badge>
                  {tool.risk_override ? <Badge tone="neutral">admin override</Badge> : null}
                  {chips.map((chip) => (
                    <span key={chip} className="text-[11px] text-faint">
                      {chip}
                    </span>
                  ))}
                  {data.dynamic && canManage ? (
                    <label className="ml-auto flex items-center gap-1.5 text-[11px] text-dim">
                      Risk
                      <Select
                        aria-label={`Risk level for ${tool.name}`}
                        value={tool.risk_override ?? ""}
                        disabled={override.isPending}
                        onChange={(event) =>
                          override.mutate({
                            slug,
                            risk: (event.target.value || null) as RiskLevel | null,
                          })
                        }
                        className="!py-1 !text-xs"
                      >
                        <option value="">Server says: {tool.derived_risk ?? tool.risk}</option>
                        {RISKS.map((risk) => (
                          <option key={risk} value={risk}>
                            {risk}
                          </option>
                        ))}
                      </Select>
                    </label>
                  ) : null}
                </div>
                <p className="mt-1 text-xs text-dim">
                  {tool.description.replace(/^\[MCP: [^\]]+\]\s*/, "") || "No description from the server."}
                </p>
                <p className="text-[11px] text-faint">{describeRisk(tool.risk)}</p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
