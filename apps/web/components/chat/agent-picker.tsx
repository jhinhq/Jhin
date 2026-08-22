"use client";

/** Chip-style picker for choosing which agent to talk to. */

import { Check } from "lucide-react";
import { Avatar } from "@/components/avatar";
import type { Agent } from "@/lib/types";

export function AgentPicker({
  agents,
  selectedId,
  onSelect,
}: {
  agents: Pick<Agent, "id" | "name" | "role_title" | "status">[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div role="radiogroup" aria-label="Choose an agent" className="flex flex-wrap gap-2">
      {agents.map((agent) => {
        const selected = agent.id === selectedId;
        return (
          <button
            key={agent.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onSelect(agent.id)}
            className={`flex min-h-[44px] items-center gap-2.5 rounded-full border py-1.5 pl-1.5 pr-4 text-left transition-colors ${
              selected
                ? "border-accent bg-accent-soft text-ink"
                : "border-line bg-surface text-dim hover:border-line-strong hover:bg-hover"
            }`}
          >
            <Avatar name={agent.name} size="sm" />
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-ink">{agent.name}</span>
              {agent.role_title ? (
                <span className="block truncate text-xs text-dim">{agent.role_title}</span>
              ) : null}
            </span>
            {selected ? <Check size={14} className="ml-1 text-accent-strong" aria-hidden /> : null}
          </button>
        );
      })}
    </div>
  );
}
