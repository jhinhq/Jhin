"use client";
import { useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input, Select, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useOrgGraph } from "@/lib/hooks";
import { variableBase, type ScopedVariable } from "@/lib/scoped-variables";

export function CopyVariableForm({ workspaceId, item, onClose, onSaved }: {workspaceId:string;item:ScopedVariable;onClose:()=>void;onSaved:()=>void}) {
  const graph = useOrgGraph(workspaceId);
  const [busy, setBusy] = useState(false), [stale, setStale] = useState(false), [error, setError] = useState<string|null>(null);
  const targets = [{value:`company:${workspaceId}`,label:"Company"},...(graph.data?.teams ?? []).map((team)=>({value:`team:${team.id}`,label:`Team: ${team.name}`})),...(graph.data?.agents ?? []).map((agent)=>({value:`agent:${agent.id}`,label:`Agent: ${agent.name}`}))].filter((target)=>target.value!==`${item.scope}:${item.scope_id}`);
  const submit = async (event:React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if(busy || stale) return;
    const form = new FormData(event.currentTarget), [scope,scopeId] = String(form.get("destination")).split(":");
    if(!targets.some((target)=>target.value===`${scope}:${scopeId}`)) return;
    setBusy(true);setError(null);
    try {await api(`${variableBase(workspaceId)}/${item.id}/copy`,{method:"POST",body:{expected_version:item.version,scope,scope_id:scopeId,name:String(form.get("name")).trim()}});onSaved();onClose();}
    catch(failure) {const conflict=failure instanceof ApiError && failure.status===409;setStale(conflict);setError(conflict ? "This variable changed or its name is already used at the destination. Close and reload before copying again." : "Couldn't copy this variable. Check your access and try again.");}
    finally {setBusy(false);}
  };
  return <Dialog open title={`Copy ${item.name}`} description="This creates a separate variable in the chosen scope and keeps the original. Secret values are copied securely without revealing them." onClose={onClose} footer={<div className="flex justify-end gap-2"><Button onClick={onClose}>Cancel</Button><Button form="copy-variable-form" type="submit" disabled={busy || stale || !targets.length || graph.isPending} variant="primary">Copy variable</Button></div>}><form id="copy-variable-form" className="space-y-4" onSubmit={(event)=>void submit(event)}><ErrorNote message={error || (graph.isError ? "Couldn't load the available scopes." : null)} />{graph.isPending ? <Spinner /> : null}<Field label="Destination"><Select name="destination" required>{targets.map((target)=><option key={target.value} value={target.value}>{target.label}</option>)}</Select></Field><Field label="Name at destination"><Input name="name" required defaultValue={item.name} maxLength={120} pattern="[A-Za-z_][A-Za-z0-9_.\-]{0,119}" /></Field></form></Dialog>;
}
