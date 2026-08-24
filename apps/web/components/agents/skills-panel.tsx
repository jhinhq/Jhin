"use client";

/** Agent profile "Skills" tab: which library skills this agent carries in
 * its prompt, plus a hint when it cannot read skill bodies (no skills.read
 * grant). Admin-only editing; everyone can look. */

import { useMutation } from "@tanstack/react-query";
import { BookOpen } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { LoadError, SectionCard } from "@/components/company/bits";
import { Badge, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useAgentGrants, useAgentSkills, useInvalidateSkills } from "@/lib/hooks";
import { canReadSkills, SOURCE_LABELS } from "@/lib/skills";
import type { AgentSkillInfo } from "@/lib/types";

export function SkillsPanel({
  workspaceId,
  agentId,
  isAdmin,
}: {
  workspaceId: string;
  agentId: string;
  isAdmin: boolean;
}) {
  const skills = useAgentSkills(workspaceId, agentId);
  const grants = useAgentGrants(workspaceId, agentId);
  const invalidate = useInvalidateSkills(workspaceId);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (skillIds: string[]) =>
      api<AgentSkillInfo[]>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/skills`, {
        method: "PUT",
        body: { skill_ids: skillIds },
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (mutationError) =>
      setError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Saving the skill selection failed.",
      ),
  });

  if (skills.isPending || grants.isPending) return <Spinner label="Loading skills…" />;
  if (skills.isError || !skills.data) {
    return <LoadError what="this agent’s skills" onRetry={() => void skills.refetch()} />;
  }

  const items = skills.data;
  const enabledIds = items.filter((item) => item.enabled_for_agent).map((item) => item.skill_id);
  const readable = canReadSkills(grants.data ?? []);

  const toggle = (item: AgentSkillInfo) => {
    const next = item.enabled_for_agent
      ? enabledIds.filter((id) => id !== item.skill_id)
      : [...enabledIds, item.skill_id];
    save.mutate(next);
  };

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <SectionCard
        title="Skills this agent uses"
        description="Skills show up in the agent’s instructions as a short list; it reads the full playbook only when it needs one."
        className="lg:col-span-2"
      >
        {items.length === 0 ? (
          <EmptyState
            icon={<BookOpen size={20} aria-hidden />}
            title="No skills in the library yet"
            description="Add skills on the Skills page first — install the starters or write your own."
            action={
              <Link href="/skills" className="text-sm text-accent-strong hover:underline">
                Open the Skills page
              </Link>
            }
          />
        ) : (
          <ul className="space-y-2">
            {items.map((item) => (
              <li key={item.skill_id}>
                <label
                  className={`flex items-start gap-3 rounded-xl border px-3 py-2.5 ${
                    item.enabled_for_agent ? "border-accent bg-accent-soft" : "border-line bg-raised"
                  } ${isAdmin ? "cursor-pointer" : ""}`}
                >
                  <input
                    type="checkbox"
                    checked={item.enabled_for_agent}
                    disabled={!isAdmin || save.isPending}
                    onChange={() => toggle(item)}
                    aria-label={`Use ${item.name}`}
                    className="mt-0.5 accent-[var(--accent)]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-[13px] font-medium">{item.name}</code>
                      <Badge>{SOURCE_LABELS[item.source]}</Badge>
                      {!item.enabled ? <Badge tone="warn">Off in library</Badge> : null}
                    </span>
                    <span className="mt-0.5 block text-xs text-dim">{item.description}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <ErrorNote message={error} />
        {!isAdmin && items.length > 0 ? (
          <p className="mt-3 text-xs text-faint">Only admins can change which skills an agent uses.</p>
        ) : null}
      </SectionCard>
      <SectionCard title="Reading skills">
        {readable ? (
          <p className="text-sm text-ink/90">
            This agent can read its skills’ full instructions with the <code>skills.read</code>{" "}
            tool.
          </p>
        ) : (
          <p className="text-sm text-ink/90" data-testid="skills-grant-hint">
            This agent will see its skills by name, but it can’t read the full instructions yet.
            An admin can grant the <code>skills.read</code> tool from Edit → Tools &amp; Access,
            or with the wizard’s “Skills” preset.
          </p>
        )}
        <p className="mt-2 text-xs text-faint">
          Skills are curated by workspace admins on the{" "}
          <Link href="/skills" className="text-accent-strong hover:underline">
            Skills page
          </Link>
          .
        </p>
      </SectionCard>
    </div>
  );
}
