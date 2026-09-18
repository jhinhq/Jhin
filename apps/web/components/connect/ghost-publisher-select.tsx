"use client";
import { useState } from "react";
import { Button, ErrorNote, Field, Select } from "@/components/ui";
import { useAgents, useUpdateConnectionConfig } from "@/lib/hooks";
import type { ConnectionInfo } from "@/lib/types";

/** A stored ID stays intact if its agent is paused or the directory is unavailable. */
export function GhostPublisherSelect({ workspaceId, value, onChange, disabled = false }: {workspaceId:string;value:string;onChange:(value:string)=>void;disabled?:boolean}) {
  const query=useAgents(workspaceId);
  const active=(query.data??[]).filter(agent=>agent.status==="active");
  const unavailable=value&&!active.some(agent=>agent.id===value);
  const current=(query.data??[]).find(agent=>agent.id===value);
  return <><Select aria-label="Publishing director" value={value} onChange={event=>onChange(event.target.value)} disabled={disabled||query.isPending||query.isError}>
    <option value="">Drafts only</option>
    {unavailable?<option value={value} disabled>{current?`${current.name} (${current.status})`:"Current director (unavailable)"}</option>:null}
    {active.map(agent=><option key={agent.id} value={agent.id}>{agent.name}{agent.role_title?` — ${agent.role_title}`:""}</option>)}
  </Select>{query.isPending?<span className="block text-xs text-dim">Loading agents…</span>:null}{query.isError?<span className="block text-xs text-danger">Couldn&rsquo;t load agents. <button type="button" className="underline" onClick={()=>void query.refetch()}>Retry</button></span>:null}</>;
}

export function GhostPublisherSettings({workspaceId,connection,canManage}:{workspaceId:string;connection:Pick<ConnectionInfo,"id"|"config_json">;canManage:boolean}) {
  const initial=String(connection.config_json.publisher_agent_id??"");
  const [selected,setSelected]=useState(initial),[saved,setSaved]=useState(false);
  const update=useUpdateConnectionConfig(workspaceId,connection.id);
  return <form className="space-y-3 rounded-xl border border-line bg-raised p-4" onSubmit={event=>{event.preventDefault();if(!canManage||update.isPending)return;setSaved(false);update.mutate({...connection.config_json,publisher_agent_id:selected},{onSuccess:()=>setSaved(true)});}}>
    <Field label="Publishing director" hint="Only this agent may approve and publish an exact reviewed draft. Drafts only disables publication."><GhostPublisherSelect workspaceId={workspaceId} value={selected} onChange={value=>{setSelected(value);setSaved(false);}} disabled={!canManage||update.isPending}/></Field>
    <p className="text-xs text-dim">Changing the director requires a new review for pending drafts.</p>
    <ErrorNote message={update.isError?"Couldn't save the publishing assignment. Reload and try again.":null}/>
    {saved?<p role="status" className="text-xs text-dim">Publishing assignment saved.</p>:null}
    {canManage?<Button size="sm" type="submit" disabled={update.isPending||selected===initial}>{update.isPending?"Saving…":"Save publishing assignment"}</Button>:null}
  </form>;
}
