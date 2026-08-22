"use client";

/** Ids of agents with a running task right now (polls through useTasks). */

import { useMemo } from "react";
import { useTasks } from "@/lib/hooks";

export function useWorkingAgentIds(workspaceId: string): Set<string> {
  const running = useTasks(workspaceId, { state: "running", limit: 100 });
  return useMemo(() => {
    const ids = new Set<string>();
    for (const task of running.data?.items ?? []) {
      if (task.assigned_agent_id) ids.add(task.assigned_agent_id);
    }
    return ids;
  }, [running.data]);
}
