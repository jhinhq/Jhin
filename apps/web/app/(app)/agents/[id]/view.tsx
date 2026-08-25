"use client";

/** Agent profile: who this agent is, who it works with, what it can use,
 * and what it has been doing. Operational settings stay in the AgentDrawer
 * (admins) and under an Advanced disclosure. */

import { useMutation } from "@tanstack/react-query";
import { ArrowLeft, ImagePlus, MessageSquare, Pause, Pencil, Play } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ActivityFeed } from "@/components/activity/activity-feed";
import { AvatarDialog } from "@/components/agents/avatar-dialog";
import { HelpDirectory } from "@/components/agents/help-directory";
import { MemoryPanel } from "@/components/agents/memory-panel";
import { SkillsPanel } from "@/components/agents/skills-panel";
import { TeamStatus } from "@/components/agents/team-status";
import { PageHeader } from "@/components/app-shell";
import { Avatar } from "@/components/avatar";
import { chatHref } from "@/components/company/agent-directory-card";
import {
  describeGrant,
  expertiseOf,
  policySummary,
  purposeOf,
  relationshipLabel,
  relationshipOtherId,
  statusTextOf,
} from "@/components/company/agent-helpers";
import { Chip, Disclosure, LoadError, SectionCard, StatusPill, Tabs } from "@/components/company/bits";
import { useWorkingAgentIds } from "@/components/company/use-working";
import { AgentDrawer } from "@/components/org/agent-drawer";
import { Button, ErrorNote, Spinner } from "@/components/ui";
import { useSegmentAfter } from "@/lib/use-route-segment";
import { api, ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/activity";
import { formatDateTime } from "@/lib/format";
import {
  useAgent,
  useAgentAvatarMap,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useConversations,
  useInvalidateOrg,
  useOrgGraph,
  useTools,
} from "@/lib/hooks";
import { avatarProps, identityAvatarProps } from "@/lib/media";
import { formatScope } from "@/lib/policy";
import type { Agent, AgentAvatar } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

type TabId =
  | "about"
  | "colleagues"
  | "team"
  | "memory"
  | "skills"
  | "access"
  | "activity"
  | "chats";

const TABS: { id: TabId; label: string }[] = [
  { id: "about", label: "About" },
  { id: "colleagues", label: "Colleagues" },
  { id: "team", label: "Team status" },
  { id: "memory", label: "Memory" },
  { id: "skills", label: "Skills" },
  { id: "access", label: "What it can use" },
  { id: "activity", label: "Recent activity" },
  { id: "chats", label: "Chats" },
];

function PersonRow({
  id,
  name,
  role,
  note,
  avatar,
}: {
  id: string;
  name: string;
  role: string;
  note?: string;
  avatar?: AgentAvatar | null;
}) {
  return (
    <li>
      <Link
        href={`/agents/${id}`}
        className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2.5 transition-colors hover:border-line-strong"
      >
        <Avatar name={name} size="sm" {...avatarProps(avatar)} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{name}</span>
          <span className="block truncate text-xs text-dim">{role || "Agent"}</span>
        </span>
        {note ? <span className="shrink-0 text-xs text-faint">{note}</span> : null}
      </Link>
    </li>
  );
}

export default function AgentProfilePage() {
  const agentId = useSegmentAfter("agents");
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  const agentQuery = useAgent(workspaceId, agentId);
  const graph = useOrgGraph(workspaceId);
  const avatars = useAgentAvatarMap(workspaceId);
  const working = useWorkingAgentIds(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);
  const [tab, setTab] = useState<TabId>("about");
  const [editing, setEditing] = useState(false);
  const [changingAvatar, setChangingAvatar] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const toggle = useMutation({
    mutationFn: (next: "pause" | "resume") =>
      api<Agent>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/${next}`, { method: "POST" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) =>
      setActionError(
        `${error instanceof ApiError ? error.detail : "The request failed"}. Try again in a moment.`,
      ),
  });

  const agent = agentQuery.data;
  const nodes = useMemo(() => graph.data?.agents ?? [], [graph.data]);
  const teams = graph.data?.teams ?? [];
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const team = teams.find((entry) => entry.id === agent?.team_id);
  const manager = agent?.manager_agent_id ? byId.get(agent.manager_agent_id) : undefined;
  const reports = nodes.filter((node) => node.manager_agent_id === agentId);
  const isManager = reports.length > 0;
  const visibleTabs = isManager ? TABS : TABS.filter((entry) => entry.id !== "team");

  if (agentQuery.isPending) {
    return (
      <>
        <PageHeader title="Agent" />
        <div className="px-4 py-6 sm:px-8">
          <Spinner label="Loading profile…" />
        </div>
      </>
    );
  }
  if (agentQuery.isError || !agent) {
    const missing = agentQuery.error instanceof ApiError && agentQuery.error.status === 404;
    return (
      <>
        <PageHeader title="Agent" />
        <div className="space-y-3 px-4 py-6 sm:px-8">
          {missing ? (
            <ErrorNote message="This agent doesn’t exist or was removed. Head back to the directory to find who you’re looking for." />
          ) : (
            <LoadError what="this agent’s profile" onRetry={() => void agentQuery.refetch()} />
          )}
          <Link href="/agents" className="inline-flex items-center gap-1 text-sm text-accent-strong hover:underline">
            <ArrowLeft size={14} /> All agents
          </Link>
        </div>
      </>
    );
  }

  const status = statusTextOf(agent, working.has(agent.id));
  const purpose = purposeOf(agent);
  const expertise = expertiseOf(agent);
  const relationships = (agent.relationships ?? []).filter((relationship) => relationship.status === "active");

  return (
    <>
      <PageHeader
        title={agent.name}
        description={agent.role_title || "Agent"}
        actions={
          <Link href="/agents" className="inline-flex items-center gap-1 text-sm text-dim hover:text-ink">
            <ArrowLeft size={14} /> All agents
          </Link>
        }
      />
      <div className="space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        <section className="rounded-2xl border border-line bg-surface p-5 sm:p-6">
          <div className="flex flex-col gap-5 sm:flex-row sm:items-start">
            <Avatar name={agent.name} size="xl" label={`${agent.name} avatar`} {...identityAvatarProps(agent)} />
            <div className="min-w-0 flex-1">
              <h2 className="font-display text-2xl font-semibold tracking-tight">{agent.name}</h2>
              <p className="text-base text-dim">{agent.role_title || "Agent"}</p>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-dim">
                <StatusPill status={status} />
                {team ? <span>{team.name}</span> : <span>No team</span>}
                {manager ? (
                  <span>
                    Reports to{" "}
                    <Link href={`/agents/${manager.id}`} className="text-accent-strong hover:underline">
                      {manager.name}
                    </Link>
                  </span>
                ) : null}
              </div>
              {purpose ? <p className="mt-3 max-w-2xl text-sm text-ink/90">{purpose}</p> : null}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2 sm:flex-col">
              <Link
                href={chatHref(agent.id)}
                className="inline-flex h-10 items-center justify-center gap-1.5 rounded-xl px-4 text-sm font-semibold text-white transition-transform hover:-translate-y-px"
                style={{ background: "linear-gradient(120deg, var(--accent), var(--accent-2, var(--accent)))" }}
              >
                <MessageSquare size={15} /> Chat
              </Link>
              {isAdmin ? (
                <>
                  <Button onClick={() => setEditing(true)}>
                    <Pencil size={14} /> Edit
                  </Button>
                  <Button onClick={() => setChangingAvatar(true)}>
                    <ImagePlus size={14} /> Change avatar
                  </Button>
                  {agent.status !== "disabled" ? (
                    <Button
                      onClick={() => toggle.mutate(agent.status === "paused" ? "resume" : "pause")}
                      disabled={toggle.isPending}
                    >
                      {agent.status === "paused" ? (
                        <>
                          <Play size={14} /> Resume
                        </>
                      ) : (
                        <>
                          <Pause size={14} /> Pause
                        </>
                      )}
                    </Button>
                  ) : null}
                </>
              ) : null}
            </div>
          </div>
          <ErrorNote message={actionError} />
        </section>

        <Tabs tabs={visibleTabs} value={tab} onChange={setTab} label="Profile sections" />

        {tab === "about" ? (
          <div className="grid gap-4 lg:grid-cols-3">
            <SectionCard title="Purpose" className="lg:col-span-2">
              {purpose ? (
                <p className="text-sm text-ink/90">{purpose}</p>
              ) : (
                <p className="text-sm text-dim">No purpose written yet.</p>
              )}
              {agent.description && agent.description !== purpose ? (
                <p className="mt-3 whitespace-pre-wrap text-sm text-dim">{agent.description}</p>
              ) : null}
            </SectionCard>
            <SectionCard title="Expertise">
              {expertise.length ? (
                <ul className="flex flex-wrap gap-1.5">
                  {expertise.map((tag) => (
                    <li key={tag}>
                      <Chip>{tag}</Chip>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-dim">No expertise tags yet.</p>
              )}
              <p className="mt-4 text-xs text-faint">Joined {formatDateTime(agent.created_at)}</p>
            </SectionCard>
            {isAdmin ? (
              <div className="lg:col-span-3">
                <Disclosure label="Show advanced settings" openLabel="Hide advanced settings">
                  <SectionCard title="Advanced" description="Operational details. Edit them from the Edit button above.">
                    <dl className="grid gap-3 text-sm sm:grid-cols-2">
                      <div>
                        <dt className="text-dim">Autonomy</dt>
                        <dd className="capitalize">{agent.autonomy_level}</dd>
                      </div>
                      <div>
                        <dt className="text-dim">Limits</dt>
                        <dd>
                          {agent.max_steps} steps · {agent.max_run_minutes} min · {agent.max_concurrent_runs} at once
                        </dd>
                      </div>
                      <div className="sm:col-span-2">
                        <dt className="text-dim">Instructions (system prompt)</dt>
                        <dd>
                          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-raised px-3 py-2 font-mono text-[12px] leading-relaxed text-dim">
                            {agent.system_prompt || "(empty)"}
                          </pre>
                        </dd>
                      </div>
                    </dl>
                  </SectionCard>
                </Disclosure>
              </div>
            ) : null}
          </div>
        ) : null}

        {tab === "colleagues" ? (
          <div className="grid gap-4 lg:grid-cols-2">
            <SectionCard title="Reporting line">
              {graph.isPending ? (
                <Spinner label="Loading colleagues…" />
              ) : (
                <ul className="space-y-2">
                  {manager ? (
                    <PersonRow id={manager.id} name={manager.name} role={manager.role_title} note="Manager" avatar={avatars[manager.id]} />
                  ) : (
                    <li className="text-sm text-dim">No manager — works independently.</li>
                  )}
                  {reports.map((report) => (
                    <PersonRow key={report.id} id={report.id} name={report.name} role={report.role_title} note="Reports here" avatar={avatars[report.id]} />
                  ))}
                </ul>
              )}
              <p className="mt-3 text-xs text-faint">
                Team: {team ? team.name : "none"}
                {team ? (
                  <>
                    {" · "}
                    <Link href="/company" className="text-accent-strong hover:underline">
                      See the company map
                    </Link>
                  </>
                ) : null}
              </p>
            </SectionCard>
            <SectionCard title="Works with" description="Relationships set up by an admin.">
              {relationships.length === 0 ? (
                <p className="text-sm text-dim">No working relationships yet.</p>
              ) : (
                <ul className="space-y-2">
                  {relationships.map((relationship) => {
                    const otherId = relationshipOtherId(relationship, agent.id);
                    const other = byId.get(otherId);
                    return (
                      <PersonRow
                        key={relationship.id}
                        id={otherId}
                        name={other?.name ?? "Another agent"}
                        role={relationship.purpose || other?.role_title || ""}
                        note={relationshipLabel(relationship, agent.id)}
                        avatar={avatars[otherId]}
                      />
                    );
                  })}
                </ul>
              )}
            </SectionCard>
            <div className="lg:col-span-2">
              <HelpDirectory workspaceId={workspaceId} agentId={agent.id} agentName={agent.name} avatars={avatars} />
            </div>
          </div>
        ) : null}

        {tab === "team" && isManager ? (
          <TeamStatus workspaceId={workspaceId} agentId={agent.id} avatars={avatars} showAdvanced={isAdmin} />
        ) : null}

        {tab === "memory" ? (
          <MemoryPanel workspaceId={workspaceId} agent={agent} canWrite={can("member")} isAdmin={isAdmin} />
        ) : null}

        {tab === "skills" ? (
          <SkillsPanel workspaceId={workspaceId} agentId={agent.id} isAdmin={isAdmin} />
        ) : null}

        {tab === "access" ? <AccessSection workspaceId={workspaceId} agentId={agent.id} isAdmin={isAdmin} /> : null}

        {tab === "activity" ? (
          <SectionCard title="Recent activity" description="What this agent has been doing and who it worked with.">
            <ActivityFeed
              workspaceId={workspaceId}
              fixedAgentId={agent.id}
              emptyTitle="Nothing yet"
              emptyDescription="Once this agent starts working, its updates and handoffs show up here."
            />
          </SectionCard>
        ) : null}

        {tab === "chats" ? <ChatsSection workspaceId={workspaceId} agent={agent} /> : null}
      </div>

      <AvatarDialog workspaceId={workspaceId} agent={agent} open={changingAvatar} onClose={() => setChangingAvatar(false)} />

      {editing && graph.data ? (
        <AgentDrawer
          key={agent.id}
          agentId={agent.id}
          onClose={() => setEditing(false)}
          teams={graph.data.teams}
          agents={graph.data.agents}
        />
      ) : null}
    </>
  );
}

function AccessSection({ workspaceId, agentId, isAdmin }: { workspaceId: string; agentId: string; isAdmin: boolean }) {
  const grants = useAgentGrants(workspaceId, agentId);
  const policy = useAgentPolicy(workspaceId, agentId);
  const tools = useTools(workspaceId);
  const connections = useConnections(workspaceId, isAdmin);
  const connectionNames = useMemo(
    () => Object.fromEntries((connections.data ?? []).map((connection) => [connection.id, connection.name])),
    [connections.data],
  );

  if (grants.isPending || policy.isPending || tools.isPending) {
    return <Spinner label="Loading access…" />;
  }
  if (grants.isError || policy.isError || tools.isError) {
    return (
      <LoadError
        what="this agent’s access"
        onRetry={() => {
          void grants.refetch();
          void policy.refetch();
          void tools.refetch();
        }}
      />
    );
  }
  const allowed = (grants.data ?? []).filter((grant) => grant.effect === "allow");
  const denied = (grants.data ?? []).filter((grant) => grant.effect === "deny");

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <SectionCard title="Apps and tools" className="lg:col-span-2">
        {allowed.length === 0 ? (
          <p className="text-sm text-dim">
            This agent can’t use any apps yet. {isAdmin ? "Use Edit → Tools & Access to give it some." : "Ask an admin to give it access."}
          </p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {allowed.map((grant) => (
              <li key={grant.id} className="flex items-start gap-2">
                <span aria-hidden className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-ok" />
                {describeGrant(grant, tools.data ?? [], connectionNames)}
              </li>
            ))}
            {denied.map((grant) => (
              <li key={grant.id} className="flex items-start gap-2 text-dim">
                <span aria-hidden className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-danger" />
                {describeGrant(grant, tools.data ?? [], connectionNames)}
              </li>
            ))}
          </ul>
        )}
        {isAdmin && (grants.data ?? []).length > 0 ? (
          <div className="mt-4">
            <Disclosure label="Show advanced details" openLabel="Hide advanced details">
              <ul className="space-y-1 font-mono text-[12px] text-dim">
                {(grants.data ?? []).map((grant) => (
                  <li key={grant.id}>
                    {grant.effect} {grant.capability} ({formatScope(grant.scope_json)})
                  </li>
                ))}
              </ul>
            </Disclosure>
          </div>
        ) : null}
      </SectionCard>
      <SectionCard title="Before acting">
        <p className="text-sm text-ink/90">{policySummary(policy.data)}</p>
        <p className="mt-2 text-xs text-faint">Risky actions wait in Attention until someone approves them.</p>
      </SectionCard>
    </div>
  );
}

function ChatsSection({ workspaceId, agent }: { workspaceId: string; agent: Agent }) {
  const conversations = useConversations(workspaceId, { agent_id: agent.id, limit: 20 });
  return (
    <SectionCard
      title={`Chats with ${agent.name}`}
      action={
        <Link href={chatHref(agent.id)} className="text-sm text-accent-strong hover:underline">
          Start a new chat
        </Link>
      }
    >
      {conversations.isPending ? (
        <Spinner label="Loading chats…" />
      ) : conversations.isError || !conversations.data ? (
        <LoadError what="chats with this agent" onRetry={() => void conversations.refetch()} />
      ) : conversations.data.items.length === 0 ? (
        <p className="text-sm text-dim">No chats yet. Say hello — it’s the fastest way to hand over work.</p>
      ) : (
        <ul className="space-y-2">
          {conversations.data.items.map((conversation) => (
            <li key={conversation.id}>
              <Link
                href={`/chats/${conversation.id}`}
                className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2.5 transition-colors hover:border-line-strong"
              >
                <MessageSquare size={15} className="shrink-0 text-dim" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{conversation.title}</span>
                  {conversation.last_message_preview ? (
                    <span className="block truncate text-xs text-dim">{conversation.last_message_preview}</span>
                  ) : null}
                </span>
                <time dateTime={conversation.last_activity_at} className="shrink-0 text-xs text-faint">
                  {timeAgo(conversation.last_activity_at)}
                </time>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}
