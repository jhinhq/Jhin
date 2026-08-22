"use client";

/** Placeholder page for navigation items delivered by a later phase. */

import { Hourglass } from "lucide-react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Badge, EmptyState } from "@/components/ui";

export function PhaseStub({
  title,
  phase,
  description,
}: {
  title: string;
  phase: string;
  description: string;
}) {
  return (
    <>
      <PageHeader title={title} />
      <PageBody>
        <EmptyState
          icon={<Hourglass size={20} strokeWidth={1.6} />}
          title={title}
          description={description}
          action={<Badge tone="accent">{phase}</Badge>}
        />
      </PageBody>
    </>
  );
}
