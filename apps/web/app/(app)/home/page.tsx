"use client";

/** Home — where signing in lands you. Everything here is derived from the
 * endpoints the rest of the app already uses (attention, conversations,
 * tasks, activity, org graph, spend); no section invents its own data. The
 * getting-started checklist only appears while the workspace is incomplete. */

import { Plus } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import { PageHeader } from "@/components/app-shell";
import { SectionCard } from "@/components/company/bits";
import { FirstRunSteps, useSetupStatus } from "@/components/first-run-steps";
import {
  NeedsYouPanel,
  RecentChatsPanel,
  RightNowPanel,
  SpendPanelFrame,
  TeamGlancePanel,
} from "@/components/home/panels";
import { SpendTile } from "@/components/spend-tile";
import { Button } from "@/components/ui";
import { sortByActivity } from "@/lib/chat";
import {
  useActivity,
  useAgentAvatarMap,
  useAttention,
  useConversations,
  useOrgGraph,
  useTasks,
  useWorkspaceSpend,
} from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const RECENT_CHATS = 4;
const RUNNING_SHOWN = 5;
const ACTIVITY_SHOWN = 5;

export default function HomePage() {
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");

  const attention = useAttention(workspaceId);
  const conversations = useConversations(workspaceId, { limit: 20 });
  const running = useTasks(workspaceId, { state: "running", limit: 50 });
  const queued = useTasks(workspaceId, { state: "queued", limit: 50 });
  // The whole feed, not just handoffs: a company with one agent per chat has
  // no agent↔agent traffic, and an always-empty strip helps nobody. Handoffs
  // are part of this feed and read the same way.
  const activity = useActivity(workspaceId, { limit: ACTIVITY_SHOWN });
  const graph = useOrgGraph(workspaceId);
  const spend = useWorkspaceSpend(workspaceId);
  const avatars = useAgentAvatarMap(workspaceId);
  const setup = useSetupStatus(workspaceId, isAdmin);

  const runningTasks = running.data?.items ?? [];
  const queuedTasks = queued.data?.items ?? [];
  const workingIds = useMemo(
    () =>
      new Set(
        (running.data?.items ?? [])
          .map((task) => task.assigned_agent_id)
          .filter((id): id is string => id !== null),
      ),
    [running.data],
  );

  const agentNameById = useMemo(() => {
    const names = new Map((graph.data?.agents ?? []).map((agent) => [agent.id, agent.name]));
    return (id: string | null) => (id ? names.get(id) ?? "An agent" : "Unassigned");
  }, [graph.data]);

  const recentChats = useMemo(
    () => sortByActivity(conversations.data?.items ?? []).slice(0, RECENT_CHATS),
    [conversations.data],
  );

  const firstName = user.display_name.split(/\s+/)[0] || "there";
  const showGettingStarted = isAdmin && !setup.isPending && !setup.complete;

  return (
    <>
      <PageHeader
        eyebrow={workspace.workspace_name}
        title={`Hi ${firstName}`}
        description="What needs you, and what your company is working on right now."
        actions={
          can("member") ? (
            <Link href="/chats">
              <Button variant="primary">
                <Plus size={14} /> New chat
              </Button>
            </Link>
          ) : null
        }
      />
      <div className="mx-auto w-full max-w-6xl space-y-4 px-4 py-5 sm:px-8 sm:py-6">
        {showGettingStarted ? (
          <SectionCard
            title="Getting started"
            description="A couple of steps and your company is ready to work."
          >
            <div className="flex justify-center">
              <FirstRunSteps workspaceId={workspaceId} isAdmin={isAdmin} includeApps />
            </div>
          </SectionCard>
        ) : null}

        <NeedsYouPanel
          attention={attention.data}
          isPending={attention.isPending}
          isError={attention.isError}
          onRetry={() => void attention.refetch()}
        />

        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-2">
            <RightNowPanel
              running={runningTasks.slice(0, RUNNING_SHOWN)}
              queued={queuedTasks}
              activity={activity.data?.items ?? []}
              // The feed hook retries on a timer, so a persistent failure can
              // sit in `pending` forever; `failureCount` is what actually tells
              // us the last attempt did not come back.
              activityFailed={activity.isError || activity.failureCount > 0}
              agentNameById={agentNameById}
              avatars={avatars}
              isPending={running.isPending || queued.isPending}
              isError={running.isError || queued.isError}
              onRetry={() => {
                void running.refetch();
                void queued.refetch();
              }}
            />
            <RecentChatsPanel
              conversations={recentChats}
              avatars={avatars}
              isPending={conversations.isPending}
              isError={conversations.isError}
              onRetry={() => void conversations.refetch()}
            />
          </div>

          <div className="space-y-4">
            <TeamGlancePanel
              agents={graph.data?.agents ?? []}
              teamCount={graph.data?.teams.length ?? 0}
              workingIds={workingIds}
              avatars={avatars}
              isPending={graph.isPending}
              isError={graph.isError}
              onRetry={() => void graph.refetch()}
            />
            <SpendPanelFrame
              isPending={spend.isPending}
              isError={spend.isError || !spend.data}
              onRetry={() => void spend.refetch()}
            >
              {spend.data ? <SpendTile spend={spend.data} bare /> : null}
            </SpendPanelFrame>
          </div>
        </div>
      </div>
    </>
  );
}
