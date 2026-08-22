"use client";

/** Company activity: the agent-to-agent feed in plain language. */

import { ActivityFeed } from "@/components/activity/activity-feed";
import { PageHeader } from "@/components/app-shell";
import { useWorkspace } from "@/lib/workspace-context";

export default function ActivityPage() {
  const { workspace } = useWorkspace();
  return (
    <>
      <PageHeader title="Activity" description="Who asked whom for what, and how it went" />
      <div className="mx-auto max-w-3xl space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        <ActivityFeed workspaceId={workspace.workspace_id} />
      </div>
    </>
  );
}
