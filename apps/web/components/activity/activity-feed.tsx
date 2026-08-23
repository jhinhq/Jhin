"use client";

/** Company activity feed with agent / kind-group filters and cursor
 * pagination ("Load more" → `before = next_before`). Pages are accumulated
 * locally; the first page stays live through the hook's polling. */

import { useMutation } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ActivityCard } from "@/components/activity/activity-card";
import { LoadError, Segmented } from "@/components/company/bits";
import { Button, EmptyState, Field, Select, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { ACTIVITY_GROUP_LABELS, kindsParam, type ActivityGroup } from "@/lib/activity";
import { useActivity, useAgents } from "@/lib/hooks";
import type { ActivityCard as ActivityCardData, ActivityList, AgentAvatar } from "@/lib/types";

const PAGE_SIZE = 30;

const GROUP_OPTIONS: { id: ActivityGroup | "all"; label: string }[] = [
  { id: "all", label: "All" },
  { id: "handoffs", label: ACTIVITY_GROUP_LABELS.handoffs },
  { id: "progress", label: ACTIVITY_GROUP_LABELS.progress },
  { id: "review", label: ACTIVITY_GROUP_LABELS.review },
];

interface FeedFilters {
  agentId: string;
  group: ActivityGroup | "all";
}

export function ActivityFeed({
  workspaceId,
  fixedAgentId,
  compact = false,
  emptyTitle = "Nothing here yet",
  emptyDescription = "When your agents talk to each other, it shows up here.",
}: {
  workspaceId: string;
  /** When set, the agent picker is hidden and the feed is scoped to this agent. */
  fixedAgentId?: string;
  compact?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
}) {
  const [filters, setFilters] = useState<FeedFilters>({ agentId: "", group: "all" });
  const agentId = fixedAgentId ?? filters.agentId;
  const baseParams = {
    agent_id: agentId || undefined,
    kinds: kindsParam(filters.group),
    limit: compact ? 10 : PAGE_SIZE,
  };
  const agents = useAgents(workspaceId);
  const avatarMap = useMemo(
    (): Record<string, AgentAvatar> =>
      Object.fromEntries(
        (agents.data ?? []).map((agent) => [
          agent.id,
          {
            url: agent.avatar_url ?? null,
            shape: agent.avatar_shape ?? null,
            color: agent.avatar_color ?? null,
          },
        ]),
      ),
    [agents.data],
  );
  const first = useActivity(workspaceId, baseParams);

  return (
    <div className="space-y-4">
      {!compact ? (
        <div className="flex flex-wrap items-end gap-3">
          <Segmented
            label="Kind of activity"
            options={GROUP_OPTIONS}
            value={filters.group}
            onChange={(group) => setFilters((current) => ({ ...current, group }))}
          />
          {!fixedAgentId ? (
            <div className="w-full sm:w-64">
              <Field label="Agent">
                <Select
                  value={filters.agentId}
                  onChange={(event) => setFilters((current) => ({ ...current, agentId: event.target.value }))}
                >
                  <option value="">Everyone</option>
                  {(agents.data ?? []).map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          ) : null}
        </div>
      ) : null}

      {first.isPending ? (
        <Spinner label="Loading activity…" />
      ) : first.isError || !first.data ? (
        <LoadError what="the activity feed" onRetry={() => void first.refetch()} />
      ) : first.data.items.length === 0 ? (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      ) : (
        <PagedList
          key={`${agentId}|${filters.group}`}
          workspaceId={workspaceId}
          firstPage={first.data}
          params={baseParams}
          compact={compact}
          avatars={avatarMap}
        />
      )}
    </div>
  );
}

/** Accumulates later pages; remounted (via key) whenever filters change. */
function PagedList({
  workspaceId,
  firstPage,
  params,
  compact,
  avatars,
}: {
  workspaceId: string;
  firstPage: ActivityList;
  params: Record<string, string | number | undefined>;
  compact: boolean;
  avatars: Record<string, AgentAvatar>;
}) {
  const [extra, setExtra] = useState<ActivityCardData[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null | undefined>(undefined);
  const loadMore = useMutation({
    mutationFn: (before: string) =>
      api<ActivityList>(`/api/v1/workspaces/${workspaceId}/activity`, {
        params: { ...params, before },
      }),
    onSuccess: (page) => {
      setExtra((current) => [...current, ...page.items]);
      setNextBefore(page.next_before);
    },
  });
  const cursor = nextBefore === undefined ? firstPage.next_before : nextBefore;

  const seen = new Set<string>();
  const items = [...firstPage.items, ...extra].filter((card) => {
    if (seen.has(card.id)) return false;
    seen.add(card.id);
    return true;
  });

  return (
    <>
      <ul className="space-y-3">
        {items.map((card) => (
          <ActivityCard key={card.id} card={card} avatars={avatars} />
        ))}
      </ul>
      {loadMore.isError ? (
        <LoadError what="older activity" onRetry={() => cursor && loadMore.mutate(cursor)} />
      ) : null}
      {!compact && cursor ? (
        <div className="flex justify-center pt-1">
          <Button onClick={() => loadMore.mutate(cursor)} disabled={loadMore.isPending}>
            {loadMore.isPending ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
    </>
  );
}
