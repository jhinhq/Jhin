"use client";

/** Placeholder page for navigation items delivered by a later phase. */

import { Hourglass } from "lucide-react";
import { PageHeader } from "@/components/app-shell";
import { Badge } from "@/components/ui";

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
      <div className="flex flex-col items-center justify-center gap-4 px-8 py-32 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-line-strong bg-surface text-dim">
          <Hourglass size={20} strokeWidth={1.6} />
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-center gap-2">
            <h2 className="text-sm font-semibold">{title}</h2>
            <Badge tone="accent">{phase}</Badge>
          </div>
          <p className="mx-auto max-w-md text-sm text-dim">{description}</p>
        </div>
      </div>
    </>
  );
}
