"use client";

/** Agent directory: search, filters, and a responsive card grid. Each card
 * leads to a chat or the agent's profile. */

import { Plus, Search } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { AgentDirectoryCard } from "@/components/company/agent-directory-card";
import {
  matchesAvailability,
  matchesSearch,
  type AvailabilityFilter,
} from "@/components/company/agent-helpers";
import { LoadError, Segmented } from "@/components/company/bits";
import { useWorkingAgentIds } from "@/components/company/use-working";
import { Button, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { useAgents, useTeams } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const AVAILABILITY_OPTIONS: { id: AvailabilityFilter; label: string }[] = [
  { id: "all", label: "Everyone" },
  { id: "available", label: "Available" },
  { id: "working", label: "Working now" },
  { id: "paused", label: "Paused" },
];

export default function AgentsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agents = useAgents(workspaceId);
  const teams = useTeams(workspaceId);
  const working = useWorkingAgentIds(workspaceId);
  const isAdmin = can("admin");

  const [query, setQuery] = useState("");
  const [teamId, setTeamId] = useState("");
  const [availability, setAvailability] = useState<AvailabilityFilter>("all");

  const teamName = useMemo(() => new Map((teams.data ?? []).map((team) => [team.id, team.name])), [teams.data]);

  const visible = useMemo(() => {
    const all = agents.data ?? [];
    return all
      .filter((agent) => agent.discoverability !== "hidden" || isAdmin)
      .filter((agent) => (teamId ? agent.team_id === teamId : true))
      .filter((agent) => matchesSearch(agent, query))
      .filter((agent) => matchesAvailability(agent, availability, working.has(agent.id)))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [agents.data, teamId, query, availability, working, isAdmin]);

  const newAgent = isAdmin ? (
    <Link href="/agents/new">
      <Button variant="primary">
        <Plus size={14} /> New agent
      </Button>
    </Link>
  ) : null;

  return (
    <>
      <PageHeader title="Agents" description="Everyone who works here, and what they’re good at" actions={newAgent} />
      <div className="space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        {agents.isPending ? (
          <Spinner label="Loading agents…" />
        ) : agents.isError || !agents.data ? (
          <LoadError what="the agent directory" onRetry={() => void agents.refetch()} />
        ) : agents.data.length === 0 ? (
          <EmptyState
            title="No agents yet"
            description="Agents are the teammates that do the work. Create your first one, give it a role, and start a chat."
            action={
              isAdmin ? (
                <Link href="/agents/new">
                  <Button variant="primary">
                    <Plus size={14} /> Create your first agent
                  </Button>
                </Link>
              ) : undefined
            }
          />
        ) : (
          <>
            <div className="flex flex-col gap-3 md:flex-row md:flex-wrap md:items-center">
              <label className="relative block w-full md:max-w-sm">
                <span className="sr-only">Search agents</span>
                <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
                <Input
                  placeholder="Search by name, role, or expertise"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="pl-9"
                />
              </label>
              {(teams.data ?? []).length > 0 ? (
                <label className="block w-full md:w-52">
                  <span className="sr-only">Team</span>
                  <Select value={teamId} onChange={(event) => setTeamId(event.target.value)}>
                    <option value="">All teams</option>
                    {(teams.data ?? []).map((team) => (
                      <option key={team.id} value={team.id}>
                        {team.name}
                      </option>
                    ))}
                  </Select>
                </label>
              ) : null}
              <Segmented label="Availability" options={AVAILABILITY_OPTIONS} value={availability} onChange={setAvailability} />
            </div>

            {visible.length === 0 ? (
              <EmptyState
                title="No agents match"
                description="Try a different search or clear the filters."
                action={
                  <Button
                    onClick={() => {
                      setQuery("");
                      setTeamId("");
                      setAvailability("all");
                    }}
                  >
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                {visible.map((agent) => (
                  <AgentDirectoryCard
                    key={agent.id}
                    agent={agent}
                    teamName={agent.team_id ? teamName.get(agent.team_id) : undefined}
                    working={working.has(agent.id)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
