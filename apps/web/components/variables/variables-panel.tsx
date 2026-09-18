"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, ConfirmDialog, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { listVariables, variableBase, type ScopedVariable, type VariableScope } from "@/lib/scoped-variables";
import { VariableForm } from "./variable-form";
import { CopyVariableForm } from "./copy-variable-form";

export function VariablesPanel({ workspaceId, scope, scopeId, scopeName, canWrite }: { workspaceId: string; scope: VariableScope; scopeId: string; scopeName: string; canWrite: boolean }) {
  const client = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<{ item?: ScopedVariable; replaceSecret?: boolean } | null>(null);
  const [copying, setCopying] = useState<ScopedVariable|null>(null);
  const [deleting, setDeleting] = useState<ScopedVariable | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const key = ["scoped-variables", workspaceId, scope, scopeId];
  const query = useQuery({ queryKey: [...key, offset], queryFn: () => listVariables(workspaceId, scope, scopeId, offset) });
  const refresh = () => { void client.invalidateQueries({ queryKey: key }); };
  const remove = async () => {
    if (!deleting || busy) return;
    setBusy(true); setError(null);
    try { await api(`${variableBase(workspaceId)}/${deleting.id}`, { method: "DELETE", params: { expected_version: deleting.version } }); setDeleting(null); refresh(); }
    catch (failure) { setDeleting(null); setError(failure instanceof ApiError && failure.status === 409 ? "This variable changed. Reload before deleting the current version." : "Couldn't delete this variable. Check your access and try again."); }
    finally { setBusy(false); }
  };
  return <section className="space-y-4 min-w-0" aria-label={`${scopeName} variables`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-display text-lg font-semibold">Variables</h2><p className="text-sm text-dim">Saved context and credentials for {scopeName}. Agents use these within their granted scope.</p></div><div className="flex gap-2"><Button size="sm" onClick={refresh}>Reload</Button>{canWrite ? <Button size="sm" onClick={() => setEditing({})}>Add variable</Button> : null}</div></div>
    <ErrorNote message={error || (query.isError ? "Couldn't load variables. Check your access and reload." : null)} />
    {query.isPending ? <Spinner label="Loading variables…" /> : null}
    {query.data?.total === 0 ? <p className="rounded-xl border border-dashed border-line p-5 text-sm text-dim">No variables saved in this scope yet.</p> : null}
    <ul className="space-y-3">{query.data?.items.map((item) => <li key={item.id} className="min-w-0 rounded-xl border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="break-all font-medium">{item.name}</h3><Badge>{item.sensitive ? item.configured ? "Secret configured" : "Secret not configured" : "Readable value"}</Badge></div>
      {item.description ? <p className="mt-1 text-sm text-dim whitespace-pre-wrap break-words">{item.description}</p> : null}
      {!item.sensitive ? <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-raised p-3 text-sm">{item.value}</pre> : null}
      <p className="mt-3 text-xs text-faint">Version {item.version} · Updated {formatDateTime(item.updated_at)} · Created by {item.created_by_type}</p>
      {item.source_variable_id ? <p className="mt-1 text-xs text-faint">Copied from source version {item.source_version}. Changes to the original do not update this copy.</p> : null}
      {canWrite ? <div className="mt-3 flex flex-wrap gap-2"><Button size="sm" onClick={() => setEditing({ item })}>Edit</Button>{item.sensitive ? <Button size="sm" onClick={() => setEditing({ item, replaceSecret: true })}>Replace secret</Button> : null}<Button size="sm" onClick={() => setCopying(item)}>Copy to scope</Button><Button size="sm" variant="ghost" onClick={() => setDeleting(item)}>Delete</Button></div> : null}
    </li>)}</ul>
    {query.data && query.data.total > 100 ? <div className="flex items-center gap-3"><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</Button><span className="text-xs text-dim">{offset + 1}–{Math.min(offset + 100, query.data.total)} of {query.data.total}</span><Button disabled={offset + 100 >= query.data.total} onClick={() => setOffset(offset + 100)}>Next</Button></div> : null}
    {editing ? <VariableForm key={`${scope}:${scopeId}:${editing.item?.id ?? "new"}:${editing.replaceSecret ?? false}`} workspaceId={workspaceId} scope={scope} scopeId={scopeId} {...editing} onClose={() => setEditing((current)=>current===editing ? null : current)} onSaved={refresh} /> : null}
    {copying ? <CopyVariableForm workspaceId={workspaceId} item={copying} onClose={()=>setCopying((current)=>current===copying ? null : current)} onSaved={()=>{void client.invalidateQueries({queryKey:["scoped-variables",workspaceId]});}} /> : null}
    <ConfirmDialog open={!!deleting} title={`Delete ${deleting?.name ?? "variable"}?`} body="Deleting this variable disables apps using it and clears their credential bindings. Agents can no longer use the variable. Running external actions may already have used it." confirmLabel="Delete variable" busy={busy} onClose={() => setDeleting(null)} onConfirm={() => void remove()} />
  </section>;
}
