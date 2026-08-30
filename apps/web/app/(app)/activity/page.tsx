"use client";

/** Company activity: the agent-to-agent feed in plain language. */

import { ActivityFeed } from "@/components/activity/activity-feed";
import { PageBody, PageHeader } from "@/components/app-shell";
import { useWorkspace } from "@/lib/workspace-context";

export default function ActivityPage() {
  const { workspace } = useWorkspace();
  return (
    <>
      <PageHeader title="Activity" description="Who asked whom for what, and how it went" />
      <PageBody narrow>
        <ActivityFeed workspaceId={workspace.workspace_id} />
      </PageBody>
    </>
  );
}
