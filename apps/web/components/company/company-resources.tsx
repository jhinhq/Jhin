"use client";
import { useState } from "react";
import { Button, ButtonLink, Dialog, Field, Select } from "@/components/ui";
import { VariablesPanel } from "@/components/variables/variables-panel";
import { MemorySummary } from "@/components/agents/memory-summary";
import type { OrgTeamNode } from "@/lib/types";

export function CompanyResources({ workspaceId, teams, canWrite }: { workspaceId: string; teams: OrgTeamNode[]; canWrite: boolean }) {
  const [open, setOpen] = useState(false), [teamId, setTeamId] = useState("");
  const team = teams.find((item) => item.id === teamId);
  return <><div className="flex flex-wrap gap-2"><Button onClick={() => setOpen(true)}>Variables & memory</Button><ButtonLink href="/automations">Schedules & automations</ButtonLink><ButtonLink href="/editorial-reviews">Director reviews</ButtonLink><ButtonLink href="/review-policies">Review policies</ButtonLink></div>
    {open ? <Dialog open wide title="Company & team context" onClose={() => setOpen(false)} footer={<Button onClick={() => setOpen(false)}>Done</Button>}>
      <div className="space-y-5"><Field label="Scope"><Select value={teamId} onChange={(event) => setTeamId(event.target.value)}><option value="">Company</option>{teams.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</Select></Field>
        <VariablesPanel key={teamId} workspaceId={workspaceId} scope={team ? "team" : "company"} scopeId={team?.id ?? workspaceId} scopeName={team?.name ?? "the company"} canWrite={canWrite} />
        <MemorySummary key={`memory:${teamId}`} workspaceId={workspaceId} scope={team ? "team" : "workspace"} scopeId={team?.id ?? workspaceId} canRebuild={canWrite} />
      </div>
    </Dialog> : null}
  </>;
}
