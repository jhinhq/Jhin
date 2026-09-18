"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Globe } from "lucide-react";
import { useState } from "react";
import { Button, ErrorNote, Field, focusRing, Select } from "@/components/ui";
import { api, ApiError } from "@/lib/api";

export type TerminalInternetStatus = {
  enabled: boolean;
  status: "enabled" | "blocked" | "off" | "custom";
  connection_id: string | null;
  connections: { id: string; name: string }[];
  has_custom_grants: boolean;
};

const LABELS: Record<TerminalInternetStatus["status"], string> = {
  enabled: "On",
  blocked: "Off — explicitly blocked",
  off: "Off",
  custom: "Custom permissions",
};

export function TerminalInternetControl({ workspaceId, agentId, onUpdated }: {
  workspaceId: string;
  agentId: string;
  onUpdated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const queryKey = ["terminal-internet", workspaceId, agentId];
  const endpoint = `/api/v1/workspaces/${workspaceId}/agents/${agentId}/terminal-internet`;
  const status = useQuery({
    queryKey,
    queryFn: () => api<TerminalInternetStatus>(endpoint),
    enabled: open,
  });
  const save = useMutation({
    mutationFn: (body: { enabled: boolean; connection_id?: string }) =>
      api<TerminalInternetStatus>(endpoint, { method: "PUT", body }),
    onSuccess: (result) => {
      queryClient.setQueryData(queryKey, result);
      setSelection(null);
      onUpdated();
    },
  });
  const data = status.data;
  const choices = data?.connections ?? [];
  const selectedId = selection ?? data?.connection_id ?? (choices.length === 1 ? choices[0].id : "");
  const selected = choices.some((connection) => connection.id === selectedId);
  const alreadyEnabled = data?.enabled && selectedId === data.connection_id;
  const failure = save.error ?? status.error;

  return (
    <section className="min-w-0 rounded-2xl border border-line bg-surface shadow-card" aria-label="Terminal Internet">
      <button
        type="button"
        aria-label="Configure Internet access"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={`flex w-full items-center gap-3 rounded-2xl px-4 py-3 text-left ${focusRing}`}
      >
        <Globe size={16} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium">Terminal Internet</span>
          <span className="mt-0.5 block text-xs text-dim">Allow this agent’s terminal commands to reach the Internet.</span>
        </span>
        <ChevronDown size={15} className={`shrink-0 text-faint transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open ? (
        <div className="min-w-0 space-y-3 border-t border-line px-4 py-4">
          <p className="text-xs text-dim">This applies to terminal commands. Isolated tests stay offline; Git operations and app connections keep their own permissions. Approval rules still apply.</p>
          <ErrorNote message={failure ? (failure instanceof ApiError ? failure.detail : "Could not update terminal Internet access.") : null} />
          {status.isPending ? <p role="status" className="text-sm text-dim">Loading Internet permissions…</p> : null}
          {!data && status.isError ? <Button size="sm" onClick={() => void status.refetch()}>Retry loading permissions</Button> : null}
          {data ? (
            <>
              <p role="status" className="text-sm font-medium">{LABELS[data.status]}</p>
              {data.has_custom_grants || data.status === "custom" ? (
                <p className="text-xs text-warn">Advanced grants may allow Internet access or restrict individual commands. Turning this off blocks terminal Internet access while preserving those grants.</p>
              ) : null}
              {choices.length > 0 ? (
                <Field label="CLI sandbox">
                  <Select
                    className="w-full min-w-0 max-w-full"
                    value={selectedId}
                    disabled={save.isPending}
                    onChange={(event) => { setSelection(event.target.value); save.reset(); }}
                  >
                    <option value="">Choose a sandbox…</option>
                    {choices.map((connection) => <option key={connection.id} value={connection.id}>{connection.name}</option>)}
                  </Select>
                </Field>
              ) : <p className="text-xs text-dim">Add or enable a CLI Sandbox in Apps, or ask an admin with app read access.</p>}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="primary" disabled={!selected || alreadyEnabled || save.isPending} onClick={() => save.mutate({ enabled: true, connection_id: selectedId })}>Enable Internet</Button>
                <Button size="sm" disabled={data.status === "blocked" || save.isPending} onClick={() => save.mutate({ enabled: false })}>Turn off Internet</Button>
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
