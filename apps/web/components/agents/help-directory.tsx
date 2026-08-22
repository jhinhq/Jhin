"use client";

/** "Who they can ask for help": a directory search showing the colleagues an
 * agent can discover. Routing context only — grants still decide. */

import { Search } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useState } from "react";
import { Avatar } from "@/components/avatar";
import { Chip, LoadError, SectionCard } from "@/components/company/bits";
import { Input, Spinner } from "@/components/ui";
import { useDirectory } from "@/lib/hooks";

export function HelpDirectory({
  workspaceId,
  agentId,
  agentName,
  avatars,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  avatars?: Record<string, string | null>;
}) {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim());
  const directory = useDirectory(workspaceId, { q: deferred || undefined, limit: 12 });
  const entries = (directory.data ?? []).filter((entry) => entry.id !== agentId);

  return (
    <SectionCard
      title="Who they can ask for help"
      description={`${agentName} can see these colleagues in the company directory and ask them for help when an admin has allowed it.`}
    >
      <label className="relative mb-3 block">
        <span className="sr-only">Search colleagues</span>
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
        <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by name, role, or skill" className="pl-9" />
      </label>
      {directory.isPending ? (
        <Spinner label="Looking up colleagues…" />
      ) : directory.isError ? (
        <LoadError what="the directory" onRetry={() => void directory.refetch()} />
      ) : entries.length === 0 ? (
        <p className="text-sm text-dim">{deferred ? `No one matches “${deferred}”.` : "No other agents are discoverable yet."}</p>
      ) : (
        <ul className="space-y-2">
          {entries.map((entry) => (
            <li key={entry.id}>
              <Link
                href={`/agents/${entry.id}`}
                className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2.5 transition-colors hover:border-line-strong"
              >
                <Avatar name={entry.name} size="sm" src={avatars?.[entry.id]} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{entry.name}</span>
                  <span className="block truncate text-xs text-dim">
                    {entry.role_title || "Agent"}
                    {entry.primary_team_name ? ` · ${entry.primary_team_name}` : ""}
                    {entry.availability === "unavailable" ? " · away" : ""}
                  </span>
                </span>
                {entry.expertise.slice(0, 2).map((tag) => (
                  <Chip key={tag}>{tag}</Chip>
                ))}
              </Link>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-xs text-faint">Asking for help never grants access: the colleague still works within its own permissions.</p>
    </SectionCard>
  );
}
